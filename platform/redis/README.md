# platform/redis — ✅ 분기 C 확정 (2026-07-29)

앱 Redis(캐시·핫딜) CR. 런북 Q3 의 실물 검증이 끝나 **분기 C(Sentinel-aware 클라이언트)** 로 확정됐다.
근거·수치 전문 = `food-budget-app/docs/mp_k8s_redis_ha_handoff.md §4`.

## 왜 C 인가 (4단계 실측 요약)

| 국면 | A = `mp-redis-master` Service | **C = Sentinel-aware** |
|---|---|---|
| 파드 delete | ✅ | ✅ 승격 0~5초 |
| **노드 상실(파드 Pending)** | 🔴 **엔드포인트 영구 공백** | ✅ **5초 승격 + 정합** |
| failback 직후 | ✅ | ✅ 10초 내 일치 |
| 슬레이브 `master_host` | ✅ | ✅ |

**A 는 "master 파드가 뜰 수 없는 동안" 을 못 넘는다.** 오퍼레이터가 ordinal-0 을 master 로 고집하기 때문이다.

## 이 구성이 성립하는 두 전제 — 바꾸지 말 것

1. **오퍼레이터 이미지 `v0.26.0`** (`platform/argocd/redis-operator.yaml` 의 `redisOperator.imageTag`).
   0.25.0 에서는 스플릿브레인·#1779·sentinel 영구 오염이 실측됐다. 차트는 0.25.0 이 최신이라 **이미지만** 올린 조합이다.
2. **Sentinel 은 `RedisReplication.spec.sentinel` 인라인** — 별도 `RedisSentinel` CR 로 두면 오퍼레이터와
   Sentinel 이 서로를 몰라 failback 이 sentinel 시야를 오염시킨다(`READONLY`, 245초+ 관측).

## 접속 좌표

```
Sentinel : mp-redis-s-{0,1,2}.mp-redis-s-hl.data.svc:26379   (3대 전부 열거 — 헤드리스 단일 이름은 1개만 잡힐 수 있다)
그룹명    : mymaster        🔴 소문자. 인라인 sentinel 의 기본값이고 CR 로 못 바꾼다
폴백     : mp-redis-master.data.svc:6379   (코드가 아직 Sentinel 모드가 아닐 때만)
```

## 🔴 운영 수칙

- **영속성 금지** — 볼륨을 붙이면 ① LocalPV 라 파드가 노드에 고정돼 **노드 사망이 영구 장애가 되고**
  ② 2026-07-22 AOF 손상 사고(16시간 크래시루프·무알람)가 재발한다. 캐시 유실은 설계된 비용이다.
- **페일오버 후 price 캐시 예열** — 페일오버마다 캐시가 비므로 `mp-poller-price-matview` 를 1회 수동 실행한다.
  자동 복구는 매시 :20 이라 방치하면 최대 60분 공백이다(§6 "price 캐시는 nGrinder 병목 대책의 절반").
  ```
  kubectl -n pipeline create job --from=cronjob/mp-poller-price-matview redis-warm-$(date +%s)
  ```
- **`MpRedisNoReplica` 알람을 끄지 말 것** — 인라인 구성에서는 오퍼레이터가 슬레이브를 고쳐주지 않아
  (#1779) 복제본 0 상태가 조용히 지속될 수 있다. `redis_up` 으로는 안 잡힌다. 이 규칙이 유일한 탐지 경로다.
