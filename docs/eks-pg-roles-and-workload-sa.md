# EKS 전용 — PG 서비스 롤(0-13) · 워크로드별 SA(0-14c) · 그 밖 3건의 판정

> 신설 2026-08-13. 브랜치 `feat/eks-pg-roles-workload-sa`.
> 근거 = 앱 레포 `docs/mp_aws_prep_checklist.md` **0-13 · 0-14c · 0-14d · 1-2 · 0-23 · 0-30**,
> 인계 명세 `docs/mp_config_repo_security_specs.md §A·§B`(#571),
> 롤 설계 `docs/prd/schema-roles.md` + 멱등 DDL `docs/prd/schema-roles.sql`(#566).
>
> 🔴 **최상위 제약 = C-83**(2026-08-13, 앱 #623) — 온프렘은 *형상 동결*, AWS 는 *덧셈만*.
> 그래서 이 브랜치는 `base/` 와 `overlays/onprem/` 을 **한 글자도** 고치지 않는다.
> 온프렘 렌더 37개가 **바이트 단위로 동일**함을 증명했다(§검증).

---

## 0. 한 장 요약

| 항목 | 판정 | 이 브랜치가 한 것 |
|---|---|---|
| **0-13** PG 스키마별 롤 | 🟢 EKS 쪽 배선 완료 | `Cluster.spec.managed.roles` 12 + ExternalSecret 12 + Pooler 산수 + 서비스 11 · 파이프라인 1 의 `PGUSER`/`PGPASSWORD` |
| **0-14c** 워크로드별 SA | 🟢 완료 | SA **35개** 신설(app 13 · pipeline 22) · 워크로드 **36/36** 연결 |
| **0-14d** 시크릿 분리 | ⛔ **EKS 에 할 일 없음** | 0-16 이 이미 AWS 키를 지웠다(렌더 실측 eks 4키 / 온프렘 6키). 남는 건 온프렘 항목이고 C-83 동결 대상 |
| **1-2** CNPG egress STS | ✅ **이미 라이브 아님·이미 머지됨** | main 에 있다(`sts.ap-northeast-2` + `sts.amazonaws.com`). 렌더로 확인만 |
| **0-23 · 0-30** barman 경로 | ✅ **이미 AWS-only 형태다** | config #148 이 이미 `overlays/eks` 에만 `pg-eks` 를 넣었다. 온프렘 경로는 안 움직인다 |

---

## 1. 0-13 — AWS PG 는 **처음부터** 롤이 갈린 상태로 짓는다

### 왜 온프렘을 안 건드리나

`schema-roles.md §5` 의 전환은 **Phase 4 = 서비스 1개씩 전환**이 "유일한 위험 구간"이라고
스스로 적고 있다. 라이브 11종의 접속 신원을 바꾸는 일이고, 그건 C-83 이 말하는 *구조·권한* 변경 그 자체다.
반대로 **아직 없는 클러스터**는 옳은 모양으로 처음부터 지으면 전환 구간이 아예 생기지 않는다.

🔴 그러므로 **온프렘 `fbapp` 의 권한을 회수하지 않는다.** 회수하면 라이브 앱이 즉사한다.

### 배선 (전부 `overlays/eks`)

```
SSM /mp/prod/pg-roles  (12 property · 32자 랜덤)          ← 🔴 값 적재는 이 레포 밖
   │  ExternalSecret ×12  (platform/pg/overlays/eks/pg-roles.yaml)
   │     type: kubernetes.io/basic-auth   ← CNPG 요구 타입
   ▼
data/mp-pg-role-<svc>  Secret
   │  Cluster.spec.managed.roles[].passwordSecret
   ▼
PG 롤 12종  (LOGIN · connectionLimit · inRoles)
   ▲
   │  GRANT (스키마·테이블)  ← 🔴 여기는 CNPG 밖이다. 사람이 psql 로 `schema-roles.sql` 1회.
```

| 무엇 | 정본 |
|---|---|
| 롤 존재 · LOGIN · 비밀번호 · connectionLimit · **그룹 멤버십** | CNPG `Cluster.spec.managed.roles` (이 레포) |
| 스키마·테이블 **GRANT** | 앱 레포 `docs/prd/schema-roles.sql` (사람이 psql) |

🔴🔴 **`inRoles` 는 배타적이다.** CNPG 는 spec 에 없는 멤버십을 **REVOKE 한다.**
`schema-roles.sql` 의 `GRANT mp_data_reader TO svc_*` 는 전부 멤버십이므로 두 곳이 어긋나면
다음 reconcile 에서 조용히 회수돼 **읽기가 죽는다.** 대조표는 `schema-roles.md §4.2` 하나다 —
이 레포의 patch 를 고칠 때 그 표를 같이 고칠 것.

### 🔴 Pooler 를 같이 줄인 이유 — 이건 튜닝이 아니라 0-13 의 일부다

PgBouncer 는 **(유저, DB) 쌍마다 별도 풀**을 만든다.

```
지금   : 풀 1개(fbapp)  × default_pool_size 20 × 인스턴스 2 =  40  ≤ 97  ✅
롤 분리: 풀 9개(svc_*)  × 20                  × 2          = 360  ≫ 97  ❌ 커넥션 고갈
```

가용은 `max_connections 100 − superuser_reserved 3 = 97`(온프렘 실측 §1.5).
→ `platform/pooler/overlays/eks` 에서 `default_pool_size: 5` · `max_db_connections: 25`.
실효 ≈ pooler 50 + pgsync 8 + 직결 14 + replica 1 + exporter 1 = **74 ≤ 97**(여유 23).

- 인증은 **안 바뀐다** — `auth_query`(`public.user_search`, SECURITY DEFINER)가 임의 롤을 자동 해석한다.
- PGSync 는 이 풀을 안 탄다(LISTEN/NOTIFY = 세션 기능). 위 산수의 `pgsync 8` 이 직결분이다.
- ⚠️ CNPG 가 이 두 키를 `spec.pgbouncer.parameters` 로 허용하는지는 **적용 전 dry-run 으로 확인**할 것.

### 🔴 이 레포가 못 하는 것 (사람·Terraform)

1. **SSM `/mp/prod/pg-roles` 적재** — 없으면 ExternalSecret 이 `SecretSyncedError` 로 앉고
   **CNPG 가 롤을 못 만든다.** 32자 랜덤 12개(온프렘 `fbapp` 은 8바이트 — 자연스러운 회전 기회).
   🔴 `app-secrets` 번들에 넣지 말 것 — 0-11(SSM standard 4,096 B) 여유가 711 B 뿐이고 12롤이 579 B 다.
2. **`schema-roles.sql` 실행** — GRANT 는 CNPG 가 관리하지 않는다.
   `postInitApplicationSQL` 은 **initdb 부트스트랩 1회만** 유효한데 AWS 는 `bootstrap.recovery` 로 뜰 예정이라(0-32)
   그 훅이 안 돈다 ⇒ **어느 경로로 짓든 psql 1회는 사람 몫**이다.
3. **`ALTER ROLE fbapp NOLOGIN`(5단계)** — recovery 부트스트랩이면 `fbapp` 과 그 권한이
   물리 복제로 **따라온다.** 위 배선은 롤을 *더할* 뿐이라, 적재 검증 후 사람이 닫는다.

---

## 2. 0-14c — 워크로드별 ServiceAccount

### 실측 대비 결과

| ns | 워크로드 | 전 | 후 |
|---|---:|---|---|
| app | 14 (Deploy 11 + Rollout 2 + CronJob 1) | `default` 14/14 | 전용 SA 13개 · 14/14 연결 |
| pipeline | 22 (Deploy 5 + CronJob 17) | `default` 22/22 | 전용 SA 22개 · 22/22 연결 |

app 이 SA 13개인 이유 = `mp-ocr-config-canary` 가 `mp-ocr` 를 공유한다(같은 서비스·같은 시크릿·같은 이미지).

### 🔴 명세(§A)와 **일부러 다르게** 한 것 2가지

**① `imagePullSecrets: [harbor]` 를 복사하지 않았다.**
§A 의 함정 서술("빠뜨리면 40개가 ImagePullBackOff")은 **온프렘 전제**다.
EKS 는 ECR 이고 **kubelet 이 노드 인스턴스 롤로 당긴다**(`AmazonEC2ContainerRegistryReadOnly`) —
IRSA 는 파드용이라 이미지 pull 에 관여하지 않는다. 게다가 eks 오버레이는
`ExternalSecret/mp-harbor-pull` 자체를 지운다(1-31). 없는 시크릿을 SA 에 적으면
kubelet 이 pull 마다 경고를 남긴다.
🔴 **이 판단은 "eks 오버레이라서" 성립한다.** 만약 누군가 이 SA 들을 `base/` 로 올리면
그 순간 §A 의 함정이 **온프렘 라이브에서** 그대로 터진다 — C-83 이 금지하는 것이 정확히 그 이동이다.

**② 공용 `mp-review-ai` 를 안 쓰고 워크로드 이름 그대로 갔다.**
종전 eks 오버레이가 Bedrock CronJob 2개에 공용 SA 를 제안했는데, "SA 하나에 워크로드 여럿"이
지금 상태(`default` 하나에 22개)이고 그게 0-14c 가 고치려는 것이다.
대신 **IAM 롤 하나를 SA 둘이 신뢰**하게 한다(아래 §IAM).

### `automountServiceAccountToken: false`

- app 은 이미 파드 레벨 14/14 false 였다 — SA 레벨에도 false 로 이중.
- 🔴 pipeline 은 22/22 **미설정**이라 파드 37개 전부에 K8s API 토큰이 붙어 있었다.
  SA 레벨 false 면 파드가 명시로 뒤집지 않는 한 안 붙는다.
- IRSA 와 충돌하지 않는다 — IRSA 토큰은 EKS 웹훅이 **별도 projected 볼륨(`aws-iam-token`)** 으로 넣는다.

### 손대지 않은 것

`data/mp-pg-onsite-dump` 는 여전히 `default` SA 다. C-18 미결(MinIO → `mp-pg-dump-ap2` 로 살릴지)이
사람 결정으로 남아 있어 이 브랜치가 정하지 않았다. 🔴 다만 지금은
`platform/policies-data/overlays/eks/netpol-pg-onsite-s3.yaml` 이 **STS 를 열어 IRSA 를 전제하는데
신원이 없는** 상태다 — 살리기로 하면 SA + 롤이 같이 와야 한다.

---

## 3. 🔴 IAM — Terraform 이 만들어야 하는 것

계정 ID 미정(C-57 로 계정 1개)이라 전부 `PLACEHOLDER` 다. `services/*` 의 ECR 레지스트리와 같은 규약.

| 롤 | 신뢰(OIDC `sub`) | 권한 | 근거 |
|---|---|---|---|
| `mp-pipeline-bedrock` | `system:serviceaccount:pipeline:mp-score-review-sentiment`<br>`system:serviceaccount:pipeline:mp-summarize-reviews` | `bedrock:InvokeModel` — **모델 ARN 2개 한정** | 0-16 · C-30 |
| `mp-pg-barman` | `system:serviceaccount:data:pg` (CNPG `serviceAccountTemplate`) | `s3:PutObject`·`GetObject`·`ListBucket`·`DeleteObject` on `mp-backup-ap2/pg-eks/*`<br>+ **`mp-backup-ap2/pg/*` 는 읽기만**(C-51 페일백 원본) | 0-16 · 0-23 |

- 🔴 신뢰정책은 **`StringEquals` 에 sub 두 개를 리스트로** 준다. `StringLike` + `mp-*` 로 뭉뚱그리면
  pipeline ns 의 SA 22개가 전부 Bedrock 롤을 맡을 수 있게 되어 0-14c 를 되돌리는 셈이 된다.
- 🔴 `sts:AssumeRoleWithWebIdentity` 는 **STS 리전 엔드포인트**로 나간다(`AWS_STS_REGIONAL_ENDPOINTS=regional`,
  C-56 함정). netpol 은 이미 열려 있다(1-2).
- 나머지 SA **33개는 롤을 붙이지 않는다.** 붙일 이유가 없고, 안 붙이는 것이 0-14c 의 결과물이다.

### 아직 롤이 필요 없지만 곧 필요해질 것 (보고용 · 이 브랜치는 안 건드림)

- `services/chat` — `GENERATOR_BACKEND=bedrock` 경로가 코드에 있다(기본값 `template`). 켜는 순간
  `mp-chat` SA 에 Bedrock 롤이 필요하다.
- `services/operations` — `rca_contract.py` 에 `provider: "bedrock"` 선택지가 있다(기본 `mock`).
- `data/mp-pg-onsite-dump` — 위 §2 참조(C-18 미결).

---

## 4. 0-14d · 1-2 · 0-23/0-30 — 판정과 근거

### 0-14d ⛔ EKS 에 할 일이 없다

명세 §B 가 쪼개라고 한 이유는 *"20개는 AWS 키가 필요 없는데 `envFrom.secretRef` 가 통째 주입이라
뺄 수가 없다"* 였다. **EKS 에서는 뺄 것이 이미 없다** — 0-16 패치(2026-08-10 머지)가
`AWS_ACCESS_KEY_ID`·`AWS_SECRET_ACCESS_KEY` 두 엔트리를 ExternalSecret 에서 지웠다.

```
렌더 실측 (2026-08-13)
  pipelines/overlays/eks    mp-pipeline-secrets = [PGPASSWORD, ES_PASSWORD, DATA_GO_KR_SERVICE_KEY, REPORT_GEMINI_API_KEY]
  pipelines/overlays/onprem mp-pipeline-secrets = [ … 위 4개 …, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY]
```

쪼개면 **키가 0개인 `mp-pipeline-aws-secrets`** 가 생긴다. 남는 것은 온프렘 항목인데
온프렘은 C-83 동결 대상이고(시크릿을 쪼개는 것 = 오브젝트 구조·권한 변경),
§B 의 "런타임 Job 36개 처리 절차"도 **라이브 온프렘 얘기**라 신규 클러스터엔 대상이 없다.

### 1-2 ✅ 이미 머지돼 있었다

`platform/policies-data/overlays/eks/kustomization.yaml` 이 `mp-pg-instance-egress` 의
`egress[1].toFQDNs` 에 `sts.ap-northeast-2.amazonaws.com` · `sts.amazonaws.com` 을 더한다.
렌더로 확인만 했고 **변경 없음**. (인덱스 기반 `op: add` 라 base 순서가 바뀌면 렌더가 죽어 드러난다.)

### 0-23 · 0-30 ✅ 이미 "AWS 쪽만" 형태다

config #148 이 **`overlays/eks` 에만** `destinationPath: s3://mp-backup-ap2/pg-eks` ·
`serverName: pg-eks` 를 넣었다. 온프렘은 `s3://mp-backup-ap2/pg` · `serverName: pg` 그대로 —
**옛 base backup + WAL 체인은 손대지 않았고 고아화도 없다.**

⚠️ 다만 #148 이 base 에도 `serverName: pg` 를 **글자로 명시**했다. 그 값은 CNPG 의 암묵 기본값
(= Cluster 이름 `pg`)과 같아 **S3 경로가 1바이트도 안 움직인다.** C-83 이후의 기준선은 이미 이
상태이므로 되돌리지 않았다 — 되돌리는 것 자체가 온프렘 렌더 변경이고, base 변경 면역
(overlays/onprem 의 재선언)까지 같이 사라진다.

체크리스트가 적은 `pg-prod`/`pg-dr` 이름은 **채택하지 않았다**(#148 판단 유지) — C-51 은 리전 장애 시
온프렘을 승격시켜 역할을 뒤바꾸므로 역할 기반 이름은 그때 거짓말이 된다. 사이트 이름은 페일오버해도 안 바뀐다.

🔴 **남은 진짜 선행은 0-23 이 아니라 `bootstrap` 이다.** base `cluster.yaml` 의
`bootstrap.pg_basebackup` → `externalClusters: vm-pg → 192.168.0.8` 은 **P4(2026-07-31)에서 파괴된 VM** 이다.
eks 렌더가 이걸 그대로 들고 있어 AWS 클러스터가 **부트스트랩 자체를 못 한다.** → 0-32 소관.
그때 `bootstrap.recovery` 가 읽을 곳은 **온프렘 값(`serverName: pg`)** 이다. `pg-eks` 로 적으면 빈 경로를 읽는다.

---

## 5. 검증

### ① 온프렘 렌더 불변 — **바이트 단위 동일**

```bash
for k in $(find . -name kustomization.yaml -path "*/overlays/onprem/*" | sed 's|/kustomization.yaml||' | sort); do
  kubectl kustomize "$k" > before/$(echo "$k" | tr / _).yaml     # 작업 전 (origin/main)
done
# … 변경 후 같은 것을 after/ 에 …
diff -r before after && echo "온프렘 diff 0"
```

결과: **트랙 37개 · sha256 37/37 일치 · diff 0.**
(SITES.md §검증 방법의 선례 — `platform/es` 이사 때와 같은 기준.)

### ② `python3 scripts/validate.py` — 통과

### ③ eks 렌더 내용 확인 (초록만으로는 안 되는 축)

```
ServiceAccount               35   (app 13 · pipeline 22)
serviceAccountName 미설정     app·pipeline 36/36 중 0
PGUSER=svc_*                 app 11 · pipeline 1(ConfigMap)
PGPASSWORD → pg-roles        11 + 1
managed.roles                12   (inRoles 대조표와 일치)
Pooler parameters            default_pool_size=5 · max_db_connections=25
```

🔴 `overlays/eks` 는 `validate.py` 의 정책 검사 대상이 아니다(`SKIP_KUSTOMIZE_RE`).
**초록 = 이관 준비 완료가 아니다.** 위 ③이 사람이 봐야 하는 자리다.
