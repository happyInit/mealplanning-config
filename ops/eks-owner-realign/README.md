# EKS 오브젝트 소유자 정합화 (manual-only)

EKS `foodbudget` DB 의 스키마·테이블·시퀀스·뷰 소유자를 **온프렘과 같은 `fbapp`** 으로 되돌린다.
`ops/pgsync-stable-alias` 와 같은 성격의 **수동 런북**이다 — `platform/argocd/**` 어디에서도 이 경로를
참조하지 않으므로 ArgoCD 가 자동 실행하지 않는다.

- 대상: **EKS 전용.** 🔴 온프렘에서 돌리지 말 것 — 이미 `fbapp` 소유이고 형상 동결 대상이다(C-83).
- 일회성: 정합화가 끝나면 다시 돌릴 일이 없다. 멱등이라 다시 돌려도 0건 처리로 끝난다.

---

## 1. 왜 필요한가

A1 초기 적재를 `postgres` 로 돌려서 **DB 의 모든 오브젝트 소유자가 `postgres`** 가 됐다.
온프렘은 전부 `fbapp` 이다. 실측(2026-08-14):

```
                  온프렘        EKS
스키마 8개        fbapp   →    postgres
테이블  41개      fbapp   →    postgres
시퀀스  26개      fbapp   →    postgres
뷰      2개       fbapp   →    postgres
matview 1개       fbapp   →    postgres     (pgsync 소유 2개는 양쪽 동일 · 대상 아님)
```

이 어긋남이 막고 있는 것 셋:

| # | 증상 | 왜 |
|---|---|---|
| ① | **PGSync bootstrap 이 죽는다** — `must be owner of relation recipe` | stock bootstrap 이 `DROP TRIGGER` 를 먼저 돌리는데, PG 에서 **트리거 삭제는 소유자 전용**이고 `GRANT` 로 줄 수 없다. `platform/pg/base/bootstrap-role.yaml` 이 설계한 경로(`inRoles: [fbapp, pgsync]` 로 **owner 권한을 상속**)가 EKS 에서는 fbapp 이 소유자가 아니라 성립하지 않는다 |
| ② | **스키마 마이그레이션 경로가 없다** | `schema-production.sql` 멱등 DDL 을 `fbapp` 으로 돌리면 `ALTER TABLE` 이 소유자가 아니라 실패한다 |
| ③ | **양 사이트 권한 구조가 다르다** | DR 페일오버 시 온프렘에서 되는 절차가 AWS 에서 안 된다. `schema-roles.sql` 5단계(`ALTER ROLE fbapp NOLOGIN`)도 "fbapp 이 소유자" 를 전제한다 |

🔴 **`ops/pgsync-stable-alias/README.md` 의 불변조건 *"table owner 도 바꾸지 않는다"* 와 충돌하지 않는다.**
그 문장은 *운영 중인 정상 상태에서 소유권을 흔들지 마라* 는 뜻이고, 여기서 하는 것은
**잘못 적재된 소유권을 정답지(온프렘)로 되돌리는 복구**다. 끝나고 나면 그 불변조건이 비로소 성립한다.

## 2. 왜 Job 이 아니라 런북인가

`ALTER ... OWNER TO` 는 **현재 소유자(`postgres`) 또는 superuser 만** 실행할 수 있다. 우회로가 없다.

그런데 이 클러스터는 `enableSuperuserAccess: false` 라 **superuser TCP 자격증명이 존재하지 않는다.**
Job 으로 만들면 그것을 새로 만들어야 하고(`enableSuperuserAccess: true` → `pg-superuser` Secret),
그건 이 레포가 금지하는 방향이다 — `bootstrap-role.yaml` 이 *"superuser 사용은 금지한다"* 이고,
지금 이 사고를 만든 것도 사람이 슈퍼유저로 손댄 결과다.

⇒ **`kubectl exec` 의 로컬 소켓 peer 인증**으로 실행한다. 이 경로는 **자격증명을 만들지도, 저장하지도
않는다.** 대신 SQL 을 이 디렉터리에 커밋해 *무엇을 했는지* 를 남긴다 — 손으로 친 DDL 은 흔적이 없다.

## 3. 실행

```bash
# ① primary 확인 — 🔴 replica 에 쓰면 read-only 에러가 난다
PRIMARY=$(kubectl -n data get pods -l cnpg.io/cluster=pg,role=primary -o name | head -1)
echo "$PRIMARY"

# ② 사전 상태 기록 (되돌릴 때 참조)
kubectl -n data exec "${PRIMARY#pod/}" -c postgres -- psql -U postgres -d foodbudget -c "
  SELECT relkind::text AS kind, pg_get_userbyid(relowner) AS owner, count(*)
    FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
   WHERE n.nspname IN ('account','activity','chat','mealplan','notify','pantry','price','public','recipebook')
     AND relkind IN ('r','S','v','m','p') GROUP BY 1,2 ORDER BY 1,2;"

# ③ 활성 트랜잭션 확인 — 있으면 끝나기를 기다린다(ALTER 가 ACCESS EXCLUSIVE 락을 잠깐 잡는다)
kubectl -n data exec "${PRIMARY#pod/}" -c postgres -- psql -U postgres -d foodbudget -c "
  SELECT pid, usename, state, now()-xact_start AS age, left(query,60)
    FROM pg_stat_activity WHERE datname='foodbudget' AND state<>'idle' AND pid<>pg_backend_pid();"

# ④ 적용
kubectl -n data exec -i "${PRIMARY#pod/}" -c postgres -- \
  psql -U postgres -d foodbudget -f - < ops/eks-owner-realign/reown.sql
```

기대 출력 — `NOTICE: 스키마 8 개 · 오브젝트 70 개 소유자 이전` + 사후 표.
(오브젝트 70 = 테이블 41 + 뷰 2 + matview 1 + 독립 시퀀스. serial 시퀀스는 테이블을 따라가므로
 개수는 상황에 따라 달라진다. **검산이 통과했으면 남은 postgres 소유는 0 이다.**)

## 4. 검증

```bash
# 온프렘과 같은 표가 나와야 한다
kubectl -n data exec "${PRIMARY#pod/}" -c postgres -- psql -U postgres -d foodbudget -c "
  SELECT relkind::text, pg_get_userbyid(relowner), count(*)
    FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
   WHERE n.nspname IN ('account','activity','chat','mealplan','notify','pantry','price','public','recipebook')
     AND relkind IN ('r','S','v','m','p') GROUP BY 1,2 ORDER BY 1,2;"
#  기대: fbapp  → r 41 · S 26 · v 2 · m 1
#        pgsync → m 2                      ← 건드리지 않는다

# pgsync 가 계속 돌고 있는지 (소유자 변경은 슬롯·복제에 영향이 없다)
kubectl -n data get pods -l app=pgsync
kubectl -n data logs deploy/mp-pgsync --tail=3
```

## 5. 되돌리기

되돌릴 이유는 사실상 없다(온프렘 값으로 맞추는 것이라 이쪽이 정답 상태다). 그래도 필요하면
같은 스크립트에서 `fbapp` ↔ `postgres` 를 바꿔 돌리면 된다. 🔴 단 **①의 사전 기록을 먼저 확인할 것** —
pgsync 소유 matview 2 개는 원래부터 pgsync 것이므로 되돌림 대상이 아니다.

## 6. 이 다음 (별건, 순서대로)

1. **bootstrap 롤 활성화 → 실행 → 재봉인** — `ops/pgsync-stable-alias/README.md` 절차.
   이 정합화가 끝나야 `inRoles: [fbapp, pgsync]` 가 **owner 권한을 실제로 상속**해서 성립한다.
   🔴 CNPG 에서 `disablePassword` 와 `passwordSecret` 은 상호 배타다 — 활성화할 때 필드를 **삭제**한다.
2. **`schema-roles.sql` 5단계** — `ALTER ROLE fbapp NOLOGIN`. EKS 의 `fbapp` 은 아직 `login=true` 다.
   적재 검증이 끝난 뒤 사람이 돌린다.
3. 🔴 **오늘(2026-08-14) 슈퍼유저로 손수 만든 트리거가 남아 있다** — `public_recipe_notify` ·
   `public_recipe_truncate`. 1 의 bootstrap 이 정상 경로로 다시 만들면서 정리된다.
