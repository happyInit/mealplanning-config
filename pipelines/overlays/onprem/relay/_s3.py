# 🔴🔴 이 파일은 **앱 레포의 사본**이다 — 정본 = happyInit/food-budget-app
#      `pipelines/transport/_s3.py` (MR !22 · 사본 시점 dfd23c5)
#
# 왜 사본이 여기 있나 = **온프렘 이미지를 다시 구울 수 없어서**다(2026-08-16).
#   온프렘 라이브 이미지는 `mp-data-pipeline:4f03052e`(2026-08-10)에 멈춰 있다 —
#   Jenkins 는 GitHub 웹훅으로 뜨는데 08-14 GitLab 정본화로 동기화가 끊겼고,
#   GitLab CI 는 arm64·ECR 전용이라 Harbor 로 push 하지 않는다.
#   🟢 그런데 그 이미지에 **boto3·confluent-kafka 가 이미 들어 있다**(실측: boto3 1.43.67 ·
#      confluent_kafka 2.15.0). 즉 모자란 건 라이브러리가 아니라 **이 코드 두 파일**뿐이라,
#      ConfigMap 으로 넣으면 이미지를 안 굽고도 돈다. `imagePullPolicy: IfNotPresent` 라
#      Harbor 가 꺼져 있어도 노드 캐시로 뜬다.
#
# 🔴 **한시적이다.** 이미지를 다시 구울 수 있게 되면 이 사본과 ConfigMap 마운트를 **지우고**
#    이미지 안의 `pipelines/transport/` 를 쓴다. 그때까지 앱 레포 쪽을 고치면 여기도 같이 고칠 것
#    — 이 레포가 싫어하는 '진실이 둘' 상태이고, 그걸 알고 감수하는 중이다.
# ─────────────────────────────────────────────────────────────────────────────
"""S3 운반 — 크롤 산출물 업로드 · 컨슈머 다운로드 · 레코드 격리 (C-44).

구 `pipelines/stream/_kafka.py` 의 대체물. 왜 스트림이 아니라 객체인가:
  우리 크롤은 애초에 스트리밍이 아니라 **배치**였다 — 하루 5회 + 주 2회 · 4.7MB/일.
  Kafka 3브로커(950m CPU · 3,456MiB)는 그 배치를 나르려고 상시 떠 있었다.

객체 키 규약:
    incoming/<stream>/<source>/<yyyy-mm-dd>/<run-id>.jsonl
    failed/<stream>/<source>/<yyyy-mm-dd>/<run-id>/<seq>.json

    stream ∈ retail | deal | recipe   SQS 큐 3개와 1:1 (= 구 Kafka 토픽 3종)
    source ∈ kurly | oasis | 10K      구 Kafka 메시지 헤더 `source` 와 1:1 (컨슈머가 그대로 쓴다)
    run-id = <UTC타임스탬프>-<파드이름>  CronJob Job 까지 역추적된다

🔴 stream 층을 넣은 이유 = 소스와 스트림이 1:1 이 아니다. `oasis` 한 크롤러가 `--categories` 면
   retail, `--deal` 이면 deal 로 간다(Kafka 도 레코드별로 토픽을 갈랐다). 소스만으로 prefix 를
   만들면 S3 이벤트가 큐를 못 고른다.

🔴 `failed/` 에는 S3 이벤트 알림이 걸려 있지 않다. 걸면 격리 레코드를 쓸 때마다 새 메시지가 나서
   무한 루프가 된다 (infra/terraform/aws/locals.tf).
"""
import hashlib
import json
import os
from datetime import datetime, timezone

DEFAULT_BUCKET = "mp-crawl-ap2"
DEFAULT_REGION = "ap-northeast-2"
INCOMING = "incoming"
FAILED = "failed"


class UploadFailed(RuntimeError):
    """업로드가 전달로 확인되지 않았다. 🔴 예외로 올린다 — 조용한 성공이 #558 의 실패 양식이었다."""


def bucket() -> str:
    return os.environ.get("MP_CRAWL_BUCKET") or DEFAULT_BUCKET


def region() -> str:
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or DEFAULT_REGION


def client(service: str = "s3"):
    """자격증명은 env(AWS_ACCESS_KEY_ID/SECRET) — 온프렘은 EKS 밖이라 IRSA 를 못 쓴다.

    그 정적 키가 체크리스트 '열린 항목 ③'(이 설계의 유일한 보안 후퇴)이고, 그래서 IAM 정책은
    이 버킷의 해당 prefix 로만 묶여 있다(infra/terraform/aws/iam.tf).

    ⚠️ boto3 는 **여기서 지연 임포트**한다 — 키 규약 함수들은 의존성 없이 임포트돼야
       파일 전용 모드와 테스트가 boto3 없이 돈다(구 `_kafka` 지연 로드와 같은 취지).
    """
    import boto3  # noqa: PLC0415

    return boto3.client(service, region_name=region())


def run_id() -> str:
    """실행 신원. K8s 에서 HOSTNAME 은 파드 이름이라 Job → 로그까지 그대로 이어진다."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{ts}-{os.environ.get('HOSTNAME') or 'local'}"


def _day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def incoming_key(stream: str, source: str, rid: str, day: str | None = None) -> str:
    return f"{INCOMING}/{stream}/{source}/{day or _day()}/{rid}.jsonl"


def failed_key(stream: str, source: str, rid: str, seq: int, day: str | None = None) -> str:
    return f"{FAILED}/{stream}/{source}/{day or _day()}/{rid}/{seq:06d}.json"


def parse_source(key: str) -> str:
    """incoming/<stream>/<source>/... 에서 source 를 꺼낸다.

    구 Kafka 경로에서 `source` 는 메시지 헤더였다(`dict(msg.headers())["source"]`).
    적재 SQL 의 유니크 키(`crawl_raw(source,kind,src_key,crawled_at)`)에 그대로 들어가므로
    값이 달라지면 **같은 레코드가 중복 적재된다** — 키에서 뽑는 이 함수가 그 계약의 유일한 지점이다.
    """
    parts = key.split("/")
    if len(parts) < 3 or parts[0] != INCOMING:
        raise ValueError(f"운반 규약에 맞지 않는 키: {key}")
    return parts[2]


def upload_run(path, stream: str, source: str, rid: str | None = None, s3=None) -> str:
    """크롤 산출 파일 1개 → S3 객체 1개. 반환 = 객체 키.

    🔴 **전달을 판정한다.** PutObject 응답의 ETag 는 단일 PUT 에서 본문 MD5 와 같다 —
       로컬에서 계산한 MD5 와 대조해 "올린 셈 쳤다"를 배제한다. HeadObject 를 쓰지 않는 이유는
       업로더 IAM 에 GetObject 가 없기 때문이다(권한을 넓히지 않으려고 ETag 로 판정한다).
    """
    rid = rid or run_id()
    key = incoming_key(stream, source, rid)
    s3 = s3 or client()

    with open(path, "rb") as fh:
        body = fh.read()
    digest = hashlib.md5(body).hexdigest()  # noqa: S324 — 무결성 대조용, 암호 용도 아님

    resp = s3.put_object(Bucket=bucket(), Key=key, Body=body, ContentType="application/x-ndjson")
    etag = (resp.get("ETag") or "").strip('"')
    if etag != digest:
        raise UploadFailed(f"s3://{bucket()}/{key} ETag {etag!r} != 본문 MD5 {digest!r}")
    return key


def iter_records(bucket_name: str, key: str, s3=None):
    """객체 → 레코드 스트림. JSONL 한 줄 = 레코드 1건. 빈 줄은 건너뛴다.

    ⚠️ 전체를 메모리에 올리지 않는다 — 지금은 객체가 5MB 미만이지만 롱테일 수집이 늘면 커진다.
    """
    s3 = s3 or client()
    body = s3.get_object(Bucket=bucket_name, Key=key)["Body"]
    for seq, line in enumerate(body.iter_lines()):
        if not line.strip():
            continue
        yield seq, json.loads(line)


def quarantine_record(stream: str, source: str, rid: str, seq: int, payload, exc, component: str, s3=None) -> str:
    """영구 실패 레코드 1건 → `failed/` 격리. 구 Kafka DLQ 토픽의 대체물(#252).

    원문 + 실패 사유를 같이 남긴다 — DLQ 토픽은 헤더에 사유를 실었는데 객체엔 헤더가 없다.
    """
    s3 = s3 or client()
    key = failed_key(stream, source, rid, seq)
    doc = {
        "component": component,
        "stream": stream,
        "source": source,
        "run_id": rid,
        "seq": seq,
        "error_type": type(exc).__name__,
        "error": str(exc),
        "quarantined_at": datetime.now(timezone.utc).isoformat(),
        "payload": payload,
    }
    s3.put_object(
        Bucket=bucket(),
        Key=key,
        Body=json.dumps(doc, ensure_ascii=False).encode(),
        ContentType="application/json",
    )
    return key
