-- EKS `foodbudget` — 오브젝트 소유자 정합화 (postgres → fbapp)
--
-- 🔴 이 스크립트는 **EKS 전용**이다. 온프렘에서 돌리지 말 것 — 온프렘은 이미 fbapp 소유다(C-83 동결).
-- 실행 절차·근거·검증은 같은 디렉터리 README.md.
--
-- 안전장치
--   · 단일 트랜잭션 — 중간 실패 시 전부 롤백
--   · lock_timeout 10s — pgsync 가 도는 중이므로 무한 대기 대신 빨리 실패한다
--   · 끝에 검산: 대상 스키마에 postgres 소유가 남으면 RAISE EXCEPTION → 롤백
--   · 필터가 `relowner = 'postgres'::regrole` 이라 pgsync 소유물은 **구조적으로 제외**된다
--     (public._view · recipebook._view matview · table_notify 함수 2종)

\set ON_ERROR_STOP on
BEGIN;
SET lock_timeout = '10s';

DO $mig$
DECLARE r record; n_sch int := 0; n_obj int := 0;
BEGIN
  -- ① 스키마 8개. `public` 은 제외한다 — 온프렘도 pg_database_owner 이고, 그게 PG 16 기본값이다.
  FOR r IN SELECT nspname FROM pg_namespace
            WHERE nspname IN ('account','activity','chat','mealplan','notify','pantry','price','recipebook')
              AND nspowner = 'postgres'::regrole
  LOOP
    EXECUTE format('ALTER SCHEMA %I OWNER TO fbapp', r.nspname);
    n_sch := n_sch + 1;
  END LOOP;

  -- ② 테이블·시퀀스·뷰·matview.
  --    테이블을 먼저 옮긴다 — serial/identity 시퀀스는 테이블 소유자 변경이 함께 끌고 가므로
  --    뒤의 시퀀스 루프는 대개 0건이 된다(그래도 남는 독립 시퀀스를 위해 둔다).
  FOR r IN SELECT n.nspname, c.relname, c.relkind FROM pg_class c
             JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname IN ('account','activity','chat','mealplan','notify','pantry','price','public','recipebook')
              AND c.relkind IN ('r','p','S','v','m')
              AND c.relowner = 'postgres'::regrole
            ORDER BY CASE c.relkind WHEN 'r' THEN 1 WHEN 'p' THEN 1 ELSE 2 END
  LOOP
    EXECUTE format('ALTER %s %I.%I OWNER TO fbapp',
      CASE r.relkind WHEN 'r' THEN 'TABLE' WHEN 'p' THEN 'TABLE'
                     WHEN 'S' THEN 'SEQUENCE' WHEN 'v' THEN 'VIEW'
                     ELSE 'MATERIALIZED VIEW' END, r.nspname, r.relname);
    n_obj := n_obj + 1;
  END LOOP;

  RAISE NOTICE '스키마 % 개 · 오브젝트 % 개 소유자 이전 (postgres -> fbapp)', n_sch, n_obj;
END
$mig$;

DO $chk$
DECLARE left_over int;
BEGIN
  SELECT count(*) INTO left_over
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
   WHERE n.nspname IN ('account','activity','chat','mealplan','notify','pantry','price','public','recipebook')
     AND c.relkind IN ('r','p','S','v','m')
     AND c.relowner = 'postgres'::regrole;
  IF left_over > 0 THEN
    RAISE EXCEPTION '검산 실패: postgres 소유 % 개 잔존 — 전체 롤백한다', left_over;
  END IF;
END
$chk$;

COMMIT;

-- 사후 확인 — 온프렘과 같은 표가 나와야 한다(fbapp: r 41 · S 26 · v 2 · m 1 / pgsync: m 2).
SELECT relkind::text AS kind, pg_get_userbyid(relowner) AS owner, count(*) AS n
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname IN ('account','activity','chat','mealplan','notify','pantry','price','public','recipebook')
   AND c.relkind IN ('r','S','v','m','p')
 GROUP BY 1, 2 ORDER BY 1, 2;
