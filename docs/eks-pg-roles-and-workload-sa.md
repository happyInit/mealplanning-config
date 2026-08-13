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

**2차(같은 날) 추가 4건 — 상세는 §6.** 전부 *"미결처럼 보였지만 이미 확정된 결정이 있던"* 것들이다.

| 항목 | 판정 | 한 것 |
|---|---|---|
| **0-32** bootstrap | 🟢 **C-78 로 확정돼 있었다** | 죽은 VM(`pg_basebackup ← 192.168.0.8`) → **`initdb` 빈 클러스터**. 🔴 1차 보고의 `bootstrap.recovery` 제안은 **틀렸다** |
| **0-14c** 잔여 | 🟢 완료 | `mp-pg-onsite-dump` 전용 SA + IRSA(`mp-pg-dump`) |
| **0-13** 부수 | 🟢 완료 | `app-common` 의 `PGUSER: fbapp` 을 eks 에서 제거(사문화 + 조용한 권한 상승 경로) |
| 🆕 **0-1 누락** | 🟢 완료 | `ocr`·`ranking-serving` 의 **Pooler 우회가 eks 에 없었다** — 온프렘과 같은 내용으로 복구 |
| A-41 chat Bedrock | ⏸ **안 한다** | eks 도 `GENERATOR_BACKEND: template`(실측). 안 쓰는 권한을 미리 주면 0-14c 역행 |

---

## 1. 0-13 — AWS PG 는 **처음부터** 롤이 갈린 상태로 짓는다

### 왜 온프렘을 안 건드리나

`schema-roles.md §5` 의 전환은 **Phase 4 = 서비스 1개씩 전환**이 "유일한 위험 구간"이라고
스스로 적고 있다. 라이브 11종의 접속 신원을 바꾸는 일이고, 그건 C-83 이 말하는 *구조·권한* 변경 그 자체다.
반대로 **아직 없는 클러스터**는 옳은 모양으로 처음부터 지으면 전환 구간이 아예 생기지 않는다.

🔴 그러므로 **온프렘 `fbapp` 의 권한을 회수하지 않는다.** 회수하면 라이브 앱이 즉사한다.

### 배선 (전부 `overlays/eks`)

```
Secrets Manager mp/prod/pg-roles  (12 property · 32자 랜덤)          ← 🔴 값 적재는 이 레포 밖
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

1. **Secrets Manager `mp/prod/pg-roles` 적재** — 없으면 ExternalSecret 이 `SecretSyncedError` 로 앉고
   **CNPG 가 롤을 못 만든다.** 32자 랜덤 12개(온프렘 `fbapp` 은 8바이트 — 자연스러운 회전 기회).
   🔴 `app-secrets` 번들에 넣지 말 것 — ⟳ **2026-08-13 정정**: 원래 근거(4,096 B 여유 711 B)는
   **C-36**(Secrets Manager · 64KB)으로 소멸했다. 유지 근거는 **폭발 반경 분리**다 —
   `app-secrets` 는 3 ns · 16객체를 받치고 IAM 경로 최소권한은 번들 단위까지만 간다.
2. **`schema-roles.sql` 실행** — GRANT 는 CNPG 가 관리하지 않는다.
   ⟳ **§6① 로 정정** — AWS 는 `initdb` 로 뜨므로 `postInitApplicationSQL` 이 *기술적으로는* 돈다.
   그래도 **안 쓴다**: GRANT 정본이 앱 레포 SQL 하나여야 하고 복사하면 갈린다.
   ⇒ **psql 1회는 사람 몫**이다(C-78 의 A1 "스키마 DDL").
3. **`ALTER ROLE fbapp NOLOGIN`(5단계)** — `initdb` 가 `fbapp` 을 **객체 소유자로 의도적으로 만든다**
   (schema-roles.sql 이 전제하는 그대로). 위 배선은 롤을 *더할* 뿐이라, 적재·검증 후 사람이 닫는다.

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

### `data/mp-pg-onsite-dump` — ✅ §6② 에서 닫았다

명세 §A 의 집계는 app·pipeline 만이라 1차에서 빠져 있었다. 2차에서 전용 SA + IRSA 를 붙였다.
🔴 **업로드 컨테이너의 MinIO → S3 재작성은 여전히 안 했다** — A4(C-78) · `2-8`·`1-20` 소관이고,
그 전에 보존 기간 정본이 갈린다(7일 vs C-79 의 190일). §6② 참조.

---

## 3. 🔴 IAM — Terraform 이 만들어야 하는 것

계정 ID 미정(C-57 로 계정 1개)이라 전부 `PLACEHOLDER` 다. `services/*` 의 ECR 레지스트리와 같은 규약.

| 롤 | 신뢰(OIDC `sub`) | 권한 | 근거 |
|---|---|---|---|
| `mp-pipeline-bedrock` | `system:serviceaccount:pipeline:mp-score-review-sentiment`<br>`system:serviceaccount:pipeline:mp-summarize-reviews` | `bedrock:InvokeModel` — **모델 ARN 2개 한정** | 0-16 · C-30 |
| `mp-pg-barman` | `system:serviceaccount:data:pg` (CNPG `serviceAccountTemplate`) | `s3:PutObject`·`GetObject`·`ListBucket`·`DeleteObject` on `mp-backup-ap2/pg-eks/*`<br>+ **`mp-backup-ap2/pg/*` 는 읽기만**(C-51 페일백 원본) | 0-16 · 0-23 |
| `mp-pg-dump` | `system:serviceaccount:data:mp-pg-onsite-dump` | `s3:PutObject`·`ListBucket`(+ 보존을 CronJob 이 맡으면 `DeleteObject`) on **`mp-pg-dump-ap2/aws/*`**<br>🔴 `mp-backup-ap2` 는 **주지 않는다** — 두 트랙의 장애 도메인 분리가 존재 이유다(C-18) | 0-14c · C-68 · C-69 |

- 🔴 신뢰정책은 **`StringEquals` 에 sub 두 개를 리스트로** 준다. `StringLike` + `mp-*` 로 뭉뚱그리면
  pipeline ns 의 SA 22개가 전부 Bedrock 롤을 맡을 수 있게 되어 0-14c 를 되돌리는 셈이 된다.
- 🔴 `sts:AssumeRoleWithWebIdentity` 는 **STS 리전 엔드포인트**로 나간다(`AWS_STS_REGIONAL_ENDPOINTS=regional`,
  C-56 함정). netpol 은 이미 열려 있다(1-2).
- 나머지 SA **33개는 롤을 붙이지 않는다.** 붙일 이유가 없고, 안 붙이는 것이 0-14c 의 결과물이다.

### 아직 롤이 필요 없지만 곧 필요해질 것 (보고용 · 이 브랜치는 안 건드림)

- `services/chat` — `GENERATOR_BACKEND=bedrock` 경로가 코드에 있다(기본값 `template`). 켜는 순간
  `mp-chat` SA 에 Bedrock 롤이 필요하다.
- `services/operations` — `rca_contract.py` 에 `provider: "bedrock"` 선택지가 있다(기본 `mock`).
- 둘 다 **지금 붙이지 않는다** — 안 쓰는 권한이라 0-14c 역행이고, 체크리스트가 **A-41** 로 추적 중이다.

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

🔴 **남은 진짜 선행은 0-23 이 아니라 `bootstrap` 이었다 — 아래 §6 에서 고쳤다.**

---

## 6. 🔴 추가로 고친 것 4건 (2026-08-13 2차)

1차 보고에서 "다른 레인" 으로 넘겼던 발견들인데, **전부 이미 확정된 결정이 있어서** 여기서 닫았다.
(찾아보지 않고 넘기면 "미결처럼 보이는 기결" 이 그대로 남는다.)

### ① 0-32 bootstrap — 죽은 VM → **빈 클러스터**(C-78)

base `cluster.yaml` 의 `bootstrap.pg_basebackup` 이 `externalClusters: vm-pg → 192.168.0.8` 을 가리키는데
그 VM 은 **P4(2026-07-31)에서 파괴**됐다. eks 렌더가 그대로 들고 있어 **AWS 클러스터가 기동을 못 한다.**

🔴 **1차 보고에서 내가 `bootstrap.recovery`(barman) 를 제안한 것은 틀렸다.** 그건 0-32 의 스케치
(C-72, 2026-08-12)였고 **C-78(2026-08-13, 사용자 확정)이 그것을 뒤집었다**:

> barman `bootstrap.recovery` 를 쓰지 않는다 — *생성 시점 1회만 유효*라 틀리면 Cluster 재생성인데,
> **빈 클러스터 + `pg_restore` 는 그 경로를 안 밟는다**

⇒ `bootstrap.initdb`(`database: foodbudget` · `owner: fbapp`) + `replica`·`externalClusters` 제거.
A1 에서 빈 클러스터로 띄우고 A3 컷오버 창에 온프렘 `pg_dump -Fc -Z6`(318MiB) → `pg_restore`.

**따라오는 정정 2건**
- 🔴 **0-23 은 0-32 의 선행이 아니다.** 읽는 경로(recovery)가 아예 안 쓰인다.
  0-23 이 막는 것(양 사이트 WAL 이 같은 프리픽스에서 섞이는 것)은 여전히 유효하지만 **별개 축**이다.
- 🟢 `initdb` 라서 `postInitApplicationSQL(Refs)` 가 **동작한다**(recovery 였다면 안 돈다).
  **그래도 안 쓴다** — GRANT 정본은 앱 레포 `schema-roles.sql` 하나여야 하고, 여기 복사하면
  정본이 둘이 되어 갈리는 순간 권한이 조용히 어긋난다.
- 🟢 `initdb` 는 `pg-app` 시크릿(username=fbapp)을 **자동 생성**한다. 온프렘은 pg_basebackup 이라
  안 만들어졌던 것이고, `mp-pg-onsite-dump` 가 그 시크릿을 읽으므로 EKS 에선 배선이 자연스러워진다.
- ⇒ §1 의 *"recovery 면 fbapp 이 물리 복제로 따라온다"* 는 이제 해당 없음.
  `fbapp` 은 initdb 가 **소유자로 의도적으로** 만든다(schema-roles.sql 이 전제하는 그대로).

### ② `mp-pg-onsite-dump` 의 신원 (0-14c 잔여분)

전용 SA + IRSA 어노테이션(`mp-pg-dump` → `mp-pg-dump-ap2`, C-68·C-69)을 붙였다.
**없으면 이상한 상태**였다 — `netpol-pg-onsite-s3.yaml` 이 이미 STS 를 열어 IRSA 를 전제하는데
붙일 신원이 없었다(네트워크는 뚫렸는데 주체가 없음).

🔴 **나머지 반쪽(업로드 컨테이너 `mc`+MinIO → S3 재작성)은 안 했다.** C-78 의 **A4** 이고 `2-8`·`1-20` 소관인데,
그 전에 **정본이 갈린다**:

| | 누가 지우나 | 보존 |
|---|---|---|
| 보존표(§lifecycle) | CronJob (`mc rm --older-than 7d` 승계) | **7일** |
| **C-79** (2026-08-13) | S3 라이프사이클 | Std → IA 30-90d → Glacier IR 90-180d → **만료 190일** |

**사람이 정할 것.** 정하기 전에 재작성하면 둘 다 지우거나 아무도 안 지우게 된다.

### ③ `app-common` 의 `PGUSER: fbapp` — EKS 에서 제거

0-13 으로 app ns 11종이 전부 자기 `env: PGUSER` 를 갖게 돼 이 값은 사문화된다. 남겨 두면 해롭다:
새 서비스가 PGUSER 를 안 적으면 **조용히 `fbapp`(앱 스키마 8개의 소유자)로 붙어** 롤 분리가 그 서비스에서만 사라진다.
지우면 기동 시점에 인증 실패로 드러난다 — **조용한 권한 상승보다 시끄러운 실패가 낫다.**

지워도 되는 근거(eks 렌더 실측): app-common 을 읽는 13개 중 PGUSER 가 필요한 11개는 전부 자기 env 보유 ·
`mp-operations` 는 자기 값(외부 교육용 DB) 보유 · `mp-frontend` 는 envFrom 자체가 없다 ·
`mp-ocr-config-canary` 는 app-common 을 읽지만 **PG 를 안 쓴다**(`config_canary.py` 에 psycopg·PGHOST·PGUSER **0건**).
🔴 온프렘은 그대로 — 거기는 11종이 실제로 `fbapp` 으로 붙어 있다(C-83).

### ④ 🆕 ocr · ranking-serving 의 **Pooler 우회가 eks 에 없었다** (0-1 누락분)

③을 확인하다 발견했다. `pg-direct.yaml`(PGHOST → `pg-rw`)이 **`overlays/onprem` 에만** 있어서
eks 렌더는 app-common 의 `pg-pooler` 를 그대로 물고 있었다 — **AWS 에서만 Pooler 를 탄다.**

| 서비스 | Pooler 를 타면 | 증상 |
|---|---|---|
| `ocr` | `SET statement_timeout`·`conn.read_only=True` 가 **세션 스코프**라 다음 문이 다른 백엔드로 간다 | 에러가 아니라 **쓰기 차단 가드가 조용히 무효화** |
| `ranking-serving` | `psycopg.connect()` 직접 호출이라 `prepare_threshold` 기본값(5) | `prepared statement "..." does not exist` |

해제 조건(ocr = `SET LOCAL` 전환 / ranking-serving = `prepare_threshold=None`)이 **아직 하나도 충족되지 않았다**
⇒ 사이트에 따라 갈릴 이유가 없다. 온프렘 파일과 같은 내용을 eks 에도 뒀다.

### ⑤ 안 한 것 — chat · operations 의 잠자는 Bedrock 경로

`services/chat` 은 eks 렌더도 `GENERATOR_BACKEND: template` 이고(실측), `services/operations` 의
`provider: bedrock` 도 기본 `mock` 이다. 지금 IRSA 롤을 붙이면 **안 쓰는 권한을 주는 것**이라
0-14c 의 취지에 역행한다. 체크리스트가 이미 **A-41**(chat Bedrock netpol · *"이관 항목이 아니라 새 기능"*)로
추적 중이므로 **그대로 둔다.**

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
