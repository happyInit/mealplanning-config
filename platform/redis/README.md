# platform/redis — 🔴 비어 있는 것이 맞다 (A-1 검증 분기 대기)

앱 Redis(캐시·핫딜) CR 은 **실물 검증 결과가 나와야 쓸 수 있다** — 런북 Q3:

- 후보 = OT-Container-Kit RedisReplication + RedisSentinel (오퍼레이터는 이미 child 로 배포됨)
- 검증 = master 파드 kill → **master Service 가 실제로 갱신되는지** + 소요시간 + 클라이언트 에러 형태
- 분기: 통과 **(A)** 앱 무변경 → 여기 CR 작성 / 부실 **(C)** 클라이언트 Sentinel 전환(접속 코드 4곳) /
  불신 **(B)** 수제 구성
- 담당자 핸드오프 = `food-budget-app/docs/mp_k8s_redis_ha_handoff.md` (PR #347)

결과가 나오면: CR 을 여기 두고 `platform/argocd/redis.yaml` child 를 추가하면 root 가 집는다.
pipelines/configmap.yaml 의 `REDIS_URL` placeholder 와 전환창 스텝 7 의 Redis 좌표도 그때 확정된다.
