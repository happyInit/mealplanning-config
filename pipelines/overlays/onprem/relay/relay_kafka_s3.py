# 🔴🔴 이 파일은 **앱 레포의 사본**이다 — 정본 = happyInit/food-budget-app
#      `pipelines/transport/relay_kafka_s3.py` (MR !22·!24 · 사본 시점 eb4fae1)
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
"""crawl-s3-relay — 온프렘 Kafka 3토픽 → S3 `incoming/` 객체 (A5-b · C-44 이관 브리지).

이관 후 배치가 이렇게 갈린다(C-3): **크롤은 온프렘 상시 프로덕션 / 적재는 AWS**. 그런데 크롤러는
Kafka 로 produce 하고 AWS 리파이너는 SQS 를 본다 — 그 사이를 잇는 것이 이 스크립트다.

## 🔴 왜 크롤러에 `--s3` 를 다는 대신 이걸 만드나 = C-83 (온프렘 형상 동결 · 덧셈만)

크롤 CronJob 7종의 args 를 고치는 것은 **라이브 프로덕션 크롤의 형상 변경**이다. 이 브리지는
그 대신 **새 컨슈머 그룹 하나**를 더한다:

    크롤러 ──produce──> Kafka ──┬──> 온프렘 리파이너 3종 (group: retail-refiner …)   [무변경]
                                └──> 이 릴레이     (group: crawl-s3-relay)          [신설]

Kafka 는 컨슈머 그룹마다 오프셋이 독립이라, 이 릴레이가 읽어도 **기존 리파이너가 보는 것이
줄지 않는다.** 크롤러·리파이너·토픽 어느 것도 건드리지 않고, 되돌리기는 이 CronJob 하나를
지우는 것이다(그러면 온프렘은 정확히 이전 상태).

🔵 `crawl_raw`(PG) 를 읽어 올리는 안은 기각했다 — **레시피가 거기 없다.** `consume_recipe` 는
   `stage_record` 를 거치지 않고 `process_recipe` 로 바로 간다. 토픽을 읽으면 3스트림이 균일하다.

## 🔴 순서 계약: **업로드 확인 → 오프셋 커밋**

뒤집으면 커밋 후 업로드 전에 죽었을 때 그 크롤분이 **통째로 사라진다**(구 컨슈머의 "DB 먼저 →
오프셋" 과 같은 규칙). `_s3.upload_run` 이 ETag 를 본문 MD5 와 대조해 *"올린 셈 쳤다"* 를
배제하므로, 반환이 곧 전달 확인이다.

반대 방향(업로드 성공 → 커밋 전 사망)은 **재전달 → 같은 객체 재업로드**가 된다. 무해하다:
객체 키에 run-id 가 붙어 새 객체가 되고, AWS 리파이너의 적재가 전부 `on conflict do nothing`
이라 중복 적재가 되지 않는다. 유실보다 중복을 택하는 쪽이 at-least-once 의 정의다.

## 스트림 매핑 (`_s3.py` 키 규약과 1:1)
    retail.crawl.raw → retail   ·   retail.deal.raw → deal   ·   recipe.crawl.raw → recipe
`source`(kurly·oasis·10K)는 Kafka 메시지 헤더에서 꺼낸다 — 구 컨슈머와 **같은 출처**라
`incoming/<stream>/<source>/…` 키가 리파이너의 `parse_source()` 와 어긋나지 않는다.

env: `MP_CRAWL_BUCKET` · `AWS_REGION` · `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`
     (🔴 온프렘은 EKS 밖이라 IRSA 를 못 쓴다 — 이 정적 키가 이 설계의 유일한 보안 후퇴이고,
      그래서 IAM 은 `s3:PutObject` **하나 · `incoming/*` 한정**이다. `mp-crawl-uploader`)
     `RELAY_IDLE_EXIT`(초, 기본 30) · `RELAY_MAX_RECORDS`(객체당 상한, 기본 5000)
"""
import argparse
import json
import os
import signal
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "stream"))
from _observability import get_pipeline_logger                         # noqa: E402
from _s3 import run_id, upload_run                                     # noqa: E402
# ⚠️ `_topics` 는 **드라이버 없는** 상수 모듈이다(`_kafka.py` 머리말) — confluent_kafka 없이
#    임포트된다. `_kafka.consumer` 는 드라이버를 끌고 오므로 `run()` 안에서 **지연 임포트**한다.
#    `_s3` 가 boto3 를 지연 임포트하는 것과 같은 취지: 이 레포의 운반 테스트는 **의존성 0** 이다.
from _topics import TOPIC_DEAL_RAW, TOPIC_RECIPE_RAW, TOPIC_RETAIL_RAW  # noqa: E402

GROUP = "crawl-s3-relay"

# 🔴 토픽 → 스트림. `_s3.py` 의 키 규약(`incoming/<stream>/…`)과 SQS 큐 3개가 여기에 물려 있다.
#    이름을 바꾸면 S3 이벤트가 큐를 못 고르고 **객체가 조용히 아무 큐에도 안 간다.**
STREAMS = {
    TOPIC_RETAIL_RAW: "retail",
    TOPIC_DEAL_RAW: "deal",
    TOPIC_RECIPE_RAW: "recipe",
}
IDLE_EXIT = float(os.environ.get("RELAY_IDLE_EXIT") or 30)
MAX_RECORDS = int(os.environ.get("RELAY_MAX_RECORDS") or 5000)

log = get_pipeline_logger(GROUP)


def _source(msg) -> str:
    """구 컨슈머와 **같은 방식**으로 꺼낸다(`consume_retail.py` 실측).

    🔴 기본값이 `oasis` 인 것도 그대로 맞춘다 — 여기서만 다른 기본값을 쓰면 헤더 없는 메시지가
       온프렘과 AWS 에서 **다른 source** 로 적재되고, `crawl_raw` 의 유니크 키가
       `(source,kind,src_key,crawled_at)` 이라 같은 레코드가 중복 적재된다.
    """
    return dict(msg.headers() or {}).get("source", b"oasis").decode()


def _flush(batches, uploader, bucket_hint, run_rid, flush_seq):
    """모아 둔 배치를 S3 객체로 올린다. 반환 = (올린 객체 수, 올린 레코드 수).

    🔴 배치 하나가 실패하면 예외를 그대로 올린다 — 호출부가 **커밋을 건너뛰게** 하기 위해서다.
       여기서 삼키면 오프셋이 전진해 그 크롤분이 사라진다.

    🔴 **`flush_seq` 가 키 충돌을 막는다 — 없으면 조용히 덮어쓴다.**
       `run_id()` 는 초 단위(`%Y%m%dT%H%M%SZ`)라, 백로그를 빠르게 드레인할 때 같은 초에 여러 번
       flush 되면 **같은 (stream,source) 가 같은 키를 만든다.** S3 PutObject 는 덮어쓰기가
       기본이고 이 버킷은 버전관리가 꺼져 있어, 앞 객체가 **에러 없이 사라진다.**
       게다가 flush 마다 오프셋을 커밋하므로 Kafka 재전달도 안 온다 = 진짜 유실이다.
       🔴 2026-08-16 첫 실전 실행에서 실제로 밟았다 — 37회 업로드 중 고유 키 22개,
          **35,000건(45.6%) 유실**. 로그는 37건 전부 "success" 였다(그래서 조용하다).
    """
    objects = records = 0
    for (stream, source), rows in sorted(batches.items()):
        if not rows:
            continue
        # 임시파일로 쓰는 이유 = `upload_run` 이 경로를 받아 본문 MD5 를 계산해 ETag 와 대조한다
        # (전달 판정). 메모리 문자열을 받게 고치면 그 판정 지점을 건드리게 된다.
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", encoding="utf-8", delete=False) as fh:
            for rec in rows:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            path = fh.name
        try:
            key = uploader(path, stream, source,
                           rid=f"{run_rid}-{flush_seq:04d}-{stream}-{source}")
        finally:
            os.unlink(path)
        objects += 1
        records += len(rows)
        log.info(f"relayed → s3://{bucket_hint}/{key}", extra={
            "event": "object_uploaded", "component": GROUP, "stream": stream,
            "source": source, "object_key": key, "record_count": len(rows), "result": "success"})
    return objects, records


def run(*, consumer_factory=None, uploader=upload_run, idle_exit=IDLE_EXIT,
        max_records=MAX_RECORDS, bucket_hint=None):
    if consumer_factory is None:
        from _kafka import consumer as consumer_factory  # noqa: PLC0415 (위 머리말 참조)
    if bucket_hint is None:
        from _s3 import bucket  # noqa: PLC0415
        bucket_hint = bucket()
    c = consumer_factory(GROUP)
    c.subscribe(list(STREAMS))
    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    log.info("crawl s3 relay started", extra={
        "event": "service_started", "component": GROUP, "consumer_group": GROUP,
        "topic": ",".join(STREAMS)})

    batches = {}
    pending = idle = 0
    objects = records = 0
    # 🔴 run_rid 는 **실행당 한 번**만 뽑는다. flush 마다 다시 뽑으면 같은 초에 걸린 flush 들이
    #    같은 값을 받아 키가 겹친다(위 `_flush` 머리말의 유실 사고). 유일성은
    #    run_rid(타임스탬프+파드이름) × flush_seq(단조증가) × (stream,source) 로 보장한다.
    run_rid = run_id()
    flush_seq = 0

    def flush_and_commit():
        """🔴 업로드가 **전부** 성공한 뒤에만 커밋한다. 순서를 지키는 유일한 지점이다."""
        nonlocal batches, pending, objects, records, flush_seq
        if not pending:
            return
        o, r = _flush(batches, uploader, bucket_hint, run_rid, flush_seq)
        c.commit()  # ← 여기까지 왔다 = 전 객체가 ETag 로 전달 확인됨
        flush_seq += 1
        objects += o
        records += r
        batches = {}
        pending = 0

    try:
        while running:
            msg = c.poll(1.0)
            if msg is None:
                idle += 1.0
                if pending:
                    flush_and_commit()  # 유휴 = 크롤 한 판이 끝났다는 신호
                if idle >= idle_exit:
                    break
                continue
            if msg.error():
                log.error("kafka consume error", extra={
                    "event": "pipeline_record_rejected", "component": GROUP,
                    "error_type": "KafkaError", "error_code": msg.error().code(), "retryable": True})
                continue

            idle = 0.0
            stream = STREAMS.get(msg.topic())
            if stream is None:  # 구독 목록 밖 — 도달 불가지만 조용히 버리지는 않는다
                log.warning("unknown topic", extra={
                    "event": "pipeline_record_rejected", "component": GROUP,
                    "topic": msg.topic(), "retryable": False})
                continue
            try:
                payload = json.loads(msg.value())
            except json.JSONDecodeError:
                # 🔴 재시도해도 같은 결과다. 여기서 멈추면 릴레이가 그 파티션에 영원히 갇힌다.
                log.exception("kafka message body is not json", extra={
                    "event": "pipeline_record_rejected", "component": GROUP,
                    "stream": stream, "error_type": "JSONDecodeError", "retryable": False})
                continue
            batches.setdefault((stream, _source(msg)), []).append(payload)
            pending += 1
            if pending >= max_records:
                flush_and_commit()

        flush_and_commit()  # 종료 신호(SIGTERM)에도 손에 든 것은 올리고 나간다
    finally:
        c.close()

    log.info("crawl s3 relay stopped", extra={
        "event": "service_stopped", "component": GROUP, "consumer_group": GROUP,
        "result": "completed", "record_count": records, "object_count": objects})
    return objects, records


def main():
    ap = argparse.ArgumentParser(description="Kafka 3토픽 → S3 incoming/ 릴레이 (A5-b)")
    ap.add_argument("--idle-exit", type=float, default=IDLE_EXIT,
                    help="빈 poll 이 이 초 수만큼 이어지면 종료(기본 %(default)s)")
    ap.add_argument("--max-records", type=int, default=MAX_RECORDS,
                    help="객체 하나에 담는 레코드 상한(기본 %(default)s)")
    args = ap.parse_args()
    run(idle_exit=args.idle_exit, max_records=args.max_records)


if __name__ == "__main__":
    main()
