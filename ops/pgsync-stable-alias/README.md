# PGSync stable alias lifecycle (manual-only)

이 디렉터리는 T-3의 재현 가능한 운영 번들이다. `platform/argocd/**` 어디에서도 이 경로를
참조하지 않는다. 네 Job은 `suspend:true`, `backoffLimit:0`, 고정 이름이라 실수로 적용해도
실행되지 않고 자동 재시도도 하지 않는다. 실행은 반드시 `ops.sh`를 통한다.

## 불변조건

- 앱과 PGSync는 물리 인덱스가 아니라 `recipes_live`를 쓴다.
- `recipes_live`는 정확히 한 backing만 가지며 그 backing에 `is_write_index:true`가 명시된다.
- 새 generation은 이 디렉터리의 `recipes-index.json`으로 만든다. 이 파일이 nori/keyword/replica
  계약의 정본이다. 앱 레포의 과거 `recipes_pgsync.index.json`은 정본이 아니다.
- ES generation 교체에는 stock bootstrap을 다시 실행하지 않는다. slot 이름은 alias에서 파생된
  `foodbudget_recipes_live`로 계속 유지한다.
- Git desired state의 `mp-pgsync-bootstrap`은 항상 PARK다. `ops.sh`만 30분 `validUntil`을 둔
  임시 live patch를 만들며 EXIT/INT/TERM에서 PARK와 임시 리소스 삭제를 시도한다.
- bootstrap role은 superuser가 아니며 table owner도 바꾸지 않는다.
- 모든 mutation은 `data/mp-pgsync-stable-alias-lock` Lease를 먼저 원자적으로 획득하고 30초마다
  갱신한다. 다른 실행은 첫 mutation 전에 실패하며, 소유권을 잃은 프로세스는 다른 실행의
  Job/Secret/role을 정리하지 않는다.
- 일회성 Job은 uid/gid 10001, read-only root filesystem, RuntimeDefault seccomp, capability ALL drop으로
  실행하고 필요한 임시 쓰기 경로만 `emptyDir`로 제공한다.
- daemon과 네 Job의 공통 `app=pgsync` egress는 NetworkPolicy로 DNS·CNPG 5432·ES 9200·전용
  Redis 6379에만 제한한다. Job별로 주입하지 않은 credential 경로뿐 아니라 네트워크 우회도 막는다.

## 1. 새 ES generation — 생성 contract만 구현, promotion은 실행 금지

daemon이 정상일 때 canonical mapping으로 빈 generation을 만드는 단계만 구현돼 있다. 같은 이름의
index가 이미 있으면 mapping/settings뿐 아니라 `count=0`이고 alias 연결이 전혀 없는 경우에만
멱등 성공한다.

```bash
cd /path/to/mealplanning-config
ops/pgsync-stable-alias/ops.sh prepare-index recipes_v3
```

🔴 **reindex/final-sync/promotion/rollback은 이 번들에서 NOT IMPLEMENTED이며 실행 금지다.** 안전한
promotion에는 최소한 write barrier 확인 → daemon 0 → full reindex → count/mapping/search gate → atomic
alias swap → daemon 1 → stable slot catch-up → CRUD E2E와 각 실패 지점의 복구가 필요하다. 이를 자동화한
별도 변경이 리뷰되기 전에는 수동 `_aliases` 요청으로 다음 generation을 승격하거나 롤백하지 않는다.
이 T-3 close 번들의 범위는 현재 `recipes_v2` 정리와 stable lifecycle 검증이다.

현재 단일 backing alias에 write flag만 빠졌다면 다음 명령은 멱등이다. backing이 0개/2개 이상이면
아무것도 바꾸지 않고 실패한다. 이 경로는 daemon Pod를 `exec`하지 않고 ES credential만 가진 별도
일회성 Job을 쓰므로 live slot 부재로 daemon이 CrashLoop인 최초 bootstrap 상황에서도 동작한다.

```bash
PGSYNC_CONFIRM=SET_RECIPES_LIVE_WRITE_INDEX \
  ops/pgsync-stable-alias/ops.sh alias-write
```

## 2. stock bootstrap — 신규 cluster/schema에서만

PGSync 7.1 stock bootstrap은 target slot과 PGSync trigger를 drop/create한다. 이미
`foodbudget_recipes_live`가 있으면 `ops.sh`가 거부한다. ES generation 교체를 이유로 우회하지 말고
bootstrap을 다시 실행하지 않는다.

```bash
PGSYNC_CONFIRM=STOCK_BOOTSTRAP_DROPS_TARGET_SLOT_AND_TRIGGERS \
  ops/pgsync-stable-alias/ops.sh bootstrap
```

한 명령 안에서 다음 순서를 수행한다.

1. alias를 exact-one + explicit write로 확인한다.
2. 0600 `mktemp`에 난수 password를 만들고 `kubernetes.io/basic-auth` Secret
   (`username`, `password`)을 생성한다. 값은 argv/log에 출력하지 않는다.
3. DatabaseRole을 `LOGIN + REPLICATION + IN ROLE fbapp,pgsync + connectionLimit 10`으로
   임시 patch한다. `disablePassword`는 제거하고 `validUntil=현재+30분`을 설정한다.
4. daemon을 0으로 내리고 suspended Job을 create한 뒤 한 번만 unsuspend한다.
5. stock `bootstrap -c /app/schema.json` 완료 후 `_view` SELECT ACL을 복구한다.
6. tracked PARK manifest를 재적용하고 password reference/validUntil을 제거한다.
7. Job/ConfigMap/basic-auth Secret을 삭제하고 daemon을 1로 복원한 뒤 read-only gate를 실행한다.
8. read-only gate가 통과한 뒤 `fbapp` CRUD CDC gate를 실행하고 read-only gate를 다시 통과시킨다.

stock bootstrap이 성공해 slot/trigger/`_view`는 생겼지만 ACL 복구에서 중단됐다면 bootstrap을
재실행하지 않는다. 다음 명령은 live slot·view owner·정확한 trigger 계약을 먼저 확인하고
`GRANT SELECT` 단계만 멱등 재개한다.

```bash
PGSYNC_CONFIRM=RESUME_POST_BOOTSTRAP_ACL_ONLY \
  ops/pgsync-stable-alias/ops.sh resume-acl
```

프로세스가 SIGKILL되면 shell trap은 실행될 수 없다. 그래도 password는 최대 30분 뒤 만료되고,
renewal child는 parent 소멸을 감지해 멈추므로 Lease는 마지막 갱신 후 최대 35분 안에 만료된다.
Lease가 만료된 뒤 아래 명령이 ownership을 원자적으로 인수해 PARK/정리를 반복한다. active holder가
남아 있는 동안에는 다른 세션의 자원을 지우지 않고 실패한다.

```bash
ops/pgsync-stable-alias/ops.sh cleanup
```

## 3. bounded rollback 종료와 legacy retirement

`foodbudget_recipes_pgsync` slot은 소비자가 없으면 WAL을 무제한 붙잡는다. 롤백 종료 시각은
`platform/pgsync/schema-configmap.yaml`의
`operations.mealplanning.io/legacy-slot-retire-after` annotation에 리뷰 가능한 Git 상태로 먼저
기록한다. CLI 인자는 live annotation과 정확히 같아야 하고 그 시각 이전에는 명령이 실패한다.

```bash
PGSYNC_CONFIRM=RETIRE_RECIPES_PGSYNC_SLOT_AND_VIEW_ROUTE \
  ops/pgsync-stable-alias/ops.sh retire '2026-08-03T09:15:00Z'
```

명령은 role을 활성화하거나 legacy slot을 건드리기 전에 alias/mapping/analyzer/count/live-slot/
role/view/trigger/ACL 전체 read-only gate와 committed INSERT→UPDATE→DELETE CDC를 통과시킨 뒤 같은
read-only gate를 다시 실행한다. 따라서 live 경로가 증명되지 않은 상태에서 rollback slot을 먼저
없앨 수 없다.

그 뒤 retirement Job은 PGSync와 같은 순서로 `hashtext('foodbudget')` database **session lock** 뒤
`hashtext('foodbudget_recipes_pgsync')` slot **session lock**을 잡는다. 두 lock은 commit 경계를 넘어
유지된다. live slot 존재, legacy slot inactive, `_view` owner/두 recipe table/4개 trigger/indices가
정확한지도 확인한다.

retirement는 의도적으로 두 단계다. replication slot 삭제는 PostgreSQL transaction rollback 대상이
아니기 때문이다.

1. 기존 행에서 독립적인 VALUES 기반 `_view_next`를 만들고 한 transaction에서 old/new view와
   unique index를 교체한 뒤 commit한다. trigger는 완전한 old 또는 완전한 new view만 본다.
2. lock을 계속 잡은 채 새 view/ACL과 legacy slot `active=false`를 재확인하고 slot을 삭제한다.

삭제 뒤에는 legacy 부재를 요구하는 전체 read-only gate → CRUD CDC → 전체 read-only gate를 다시
통과해야 명령이 성공한다.

1단계 commit 뒤 프로세스가 죽으면 재실행은 `live-only view + legacy slot` 상태를 받아 2단계부터
재개한다. slot 삭제 뒤 죽거나 이미 완료된 상태도 `live-only view + slot absent`로 멱등 성공한다.
예상 밖 조합(`legacy route + slot absent`, 다른 index/table/owner/trigger)은 자동 수정하지 않고 실패한다.

## 4. 최종 read-only gate

```bash
ops/pgsync-stable-alias/ops.sh verify
```

검사 범위:

- Argo `pg`/`pgsync`/`mp-recipe` Synced+Healthy와 두 live env 배선
- PARK role, NULL password, membership 0, 임시 Secret/Job 없음
- live slot, legacy slot 부재, `_view.indices={recipes_live}`, table owner, trigger, ACL
- PG/ES count 동일
- ES green, exact-one alias, explicit write backing, canonical mapping, replica=1
- `김치찌개` nori mixed 토큰(`김치찌개`, `김치`, `찌개`)

승인된 rollback window 동안만 legacy slot을 임시 허용할 수 있다. 환경변수 값도 Git/live annotation과
정확히 같아야 하며 deadline이 지나면 gate가 실패한다.

```bash
ALLOW_LEGACY_SLOT_UNTIL='2026-08-03T09:15:00Z' \
  ops/pgsync-stable-alias/ops.sh verify
```

## 5. CRUD E2E 쓰기 검증

read-only gate와 별도로 다음 executable gate를 수행한다. 이 명령도 먼저 read-only gate를 통과하고,
쓰기 검증 뒤 동일 gate를 다시 실행한다.

```bash
PGSYNC_CONFIRM=RUN_RECIPES_LIVE_CDC_CRUD_E2E \
  ops/pgsync-stable-alias/ops.sh crud
```

DB 접속은 `data/pg-app`의 `kubernetes.io/basic-auth` `username/password`를 `secretKeyRef`로
주입한 suspended/backoff=0 일회성 Job을 사용한다. Secret을 decode하거나 password를 argv·로그에
넣지 않는다. stock bootstrap lifecycle은 daemon 복구 뒤 이 gate를 자동 호출한다.

Job은 UUID 기반 전용 `source/src_recipe_id`와 음수 bigint ID를 쓰고 다음을 순서대로 확인한다.

1. INSERT commit 후 `recipes_live/_doc/<id>`가 나타나고 이름이 일치한다.
2. UPDATE commit 후 같은 문서 이름이 바뀐다.
3. DELETE commit 후 문서가 404가 된다.
4. 실패 경로의 `finally`에서도 같은 PG 행을 DELETE하고 ES 404까지 확인한다.

실패 경로도 `finally`에서 테스트 행을 지우고 ES 404를 확인한다. 운영 데이터와 충돌 가능한 고정
ID를 재사용하거나 postgres/bootstrap identity로 CRUD를 수행하지 않는다. Job 로그의 테스트 ID와
INSERT/UPDATE/DELETE 성공을 변경 기록에 남긴다.
