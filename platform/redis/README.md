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

---

# C-14 단일화 설계 (2026-08-10) — 🔴 **설계만. 아직 아무것도 안 바꿨다**

> C-14: **AWS = ElastiCache for Valkey `cache.t4g.micro` Multi-AZ 2노드 · 온프렘도 단일 Redis 로 단순화**(Sentinel 제거).
> 판단 축은 비용이 아니라 **Sentinel 운영 부담**이었다 — 오퍼레이터 결함 우회 코드가 프로덕션에 있다.
> 이 절은 그 "온프렘 단일화" 를 실측 위에서 설계한다.

## 1. 실측 (2026-08-10 · 읽기 전용)

**지금 도는 것 = 6 파드**

| 파드 | 요청 CPU | 실사용 CPU | 요청 메모리 | 실사용 메모리 |
|---|---|---|---|---|
| `mp-redis-0` (master) | 100m | **4m** | 256Mi | **14Mi** |
| `mp-redis-1` (replica) | 100m | **4m** | 256Mi | **13Mi** |
| `mp-redis-s-0/1/2` (Sentinel ×3) | 50m ×3 | **3m** ×3 | 128Mi ×3 | 35·40·40Mi |
| 🔴 `redis-operator` (별 ns) | **500m** | **4m** | 500Mi | **33Mi** |
| `mp-redis-pgsync` (별건·이미 단일) | 50m | 23m | 64Mi | 4Mi |

**데이터**
```
used_memory 2.88 MB   maxmemory 150 MB   keys 6 (그중 4개가 TTL 보유)
aof_enabled 0         PVC 0개            connected_clients 9
```

🔴 **핵심 숫자: 오퍼레이터 하나가 CPU 요청의 59%(500 / 850m)다.** 키 6개짜리 캐시를 관리하려고.
요청/실사용 비 = **125배**. 클러스터 평균 9.4배(C-29 실측)를 크게 벗어나는 단일 최악 항목이다.

⇒ **단일화의 이득은 파드 4개가 아니라 오퍼레이터다.** 이게 설계를 결정한다(§3).

회수량 = 350m + 500m = **850m CPU** / 896Mi + 500Mi = **1,396Mi**
🟢 체크리스트 C-45 표의 `−850m / −1,396 MiB` 와 **정확히 일치**한다(독립 재현).

## 2. 🔴 전제 정정 — **Python 코드를 되돌릴 필요가 없다**

C-14 항목에 *"클라이언트가 Sentinel-aware 로 짜여 있어 코드도 되돌려야 한다"* 고 적혀 있는데,
**실측하면 그렇지 않다.** Redis 소비자는 6곳이고(4곳이 아니다) 전부 이미 단일 호스트 경로를 갖고 있다:

| # | 소비자 | Sentinel 분기 | 단일 폴백 | 되돌리기에 필요한 코드 변경 |
|---|---|---|---|---|
| 1 | `services/price/app/db.py:33` | `if settings.redis_sentinels:` | `Redis(host=REDISHOST, port=REDISPORT)` | **없음** |
| 2 | `services/chat/app/db.py:50` | 동일 | 동일 | **없음** |
| 3 | `pipelines/stream/_redis.py:26` | `if REDIS_SENTINELS:` | `Redis.from_url(REDIS_URL)` | **없음** |
| 4 | `pipelines/ingest/refresh_price_matview.py:34` | `if sentinels:` | `Redis.from_url(REDIS_URL)` | **없음** |
| 5 | 🔴 `services/video/app/store.py:24` | **없음** | host/port 직결뿐 | **없음** (애초에 Sentinel 을 안 씀) |
| 6 | 🔴 `services/ocr/app/store.py:77` | **없음** | host/port 직결뿐 | **없음** (동일) |

⇒ **단일화의 코드 변경량은 0줄이다.** 되돌리기는 **ConfigMap 2개**의 문제다(§4).
   4곳은 "env 없으면 기존 단일 호스트 경로 — 현행 VM(.8) 동작 불변" 이라는 **하위호환 설계**가
   처음부터 들어가 있었다(각 파일 주석). 그 설계가 지금 값을 한다.

🔴 **덤으로 드러난 것 — `video`·`ocr` 은 Sentinel 경로에 **한 번도 들어간 적이 없다.**
   즉 두 서비스는 `mp-redis-master` Service 를 직접 보고 있고, 그 Service 는
   **노드 상실 국면에서 갱신되지 않는다**(오퍼레이터가 ordinal-0 고집 — 이 README 상단·handoff §4).
   ⇒ **Sentinel 작업이 고치려던 바로 그 버그를 두 서비스는 지금도 갖고 있다.**
   체크리스트 `1-14`("video Redis 재시도 부재")가 가리키는 것이 이것이고, 실제로는 **video 만이 아니라
   ocr 도** 해당한다. 🟢 단일화하면 이 버그는 **원인이 소멸**한다(파드가 1개면 ordinal-0 이 늘 정답).
   즉 **1-14 는 C-14 의 선행이 아니라 C-14 로 해소되는 항목**이다 — 순서를 뒤집어 이해하고 있었다.

## 3. 목표 형상 — 오퍼레이터를 버리고 `mp-redis-pgsync` 패턴을 재사용한다

| 안 | 무엇 | 회수 | 판정 |
|---|---|---|---|
| A | 같은 오퍼레이터의 **standalone `Redis` CR** | 350m / 896Mi (오퍼레이터 잔류) | ❌ **이득의 59% 를 포기한다.** 키 6개를 위해 500m 컨트롤러를 남긴다 |
| B | **plain Deployment + Service** (`mp-redis-pgsync` 와 동일 패턴) | **850m / 1,396Mi** | ✅ **채택** |
| C | Bitnami redis 차트(architecture=standalone) | 850m / 1,396Mi | ❌ 새 차트·새 values·새 업그레이드 축. B 보다 부품이 많다 |

🟢 **B 를 고르는 결정적 이유는 자원이 아니라 "새 패턴이 0개" 라는 것이다** —
`platform/pgsync/base/redis-pgsync.yaml` 에 **이미 손으로 만든 단일 Redis 가 돌고 있다**:
```yaml
kind: Deployment            replicas: 1
image: docker.io/library/redis:7-alpine
args: ["redis-server", "--save", "", "--appendonly", "no"]   # 영속성 없음
requests: {cpu: 50m, memory: 64Mi}   limits: {memory: 128Mi}
+ bare Service (port 6379)
```
12일간 무사고로 돌았고(재시작 0), 같은 ns 에 있고, 같은 사람이 운영한다.
⇒ 앱 Redis 를 **그 파일의 형제**로 만들면 리뷰할 새 개념이 없다.

**제안 스펙** (실사용 4m/14Mi · maxmemory 150M 기준):
```
image     docker.io/library/redis:7-alpine     # 🔴 quay.io/opstree/* 를 안 쓴다 — 오퍼레이터 배포본이다
args      redis-server --save "" --appendonly no
          --maxmemory 150mb --maxmemory-policy allkeys-lru
requests  cpu 50m · memory 256Mi        limits  memory 256Mi   (CPU limit 없음 — §13.7)
Service   `mp-redis` (bare) : 6379      + exporter sidecar 유지(9121 · 기존 PodMonitor 계약)
PVC       0개 (지금도 0개다)
```
🔴 `--maxmemory-policy allkeys-lru` 를 **명시**한다. 상단 절이 기록한 실측 함정
(`maxmemory 0` + `noeviction`)이 그대로 재발하지 않게, 오퍼레이터 config 가 아니라 args 로 박는다.

## 4. 전환 절차 — 🔴 순서를 뒤집으면 캐시가 통째로 빈다

```
1. 새 단일 Redis 를 **나란히** 띄운다 (이름 충돌 회피: 임시 `mp-redis-solo` + Service)
     🔴 기존 RedisReplication 을 먼저 지우지 않는다. 롤백 손잡이를 남긴다.

2. ConfigMap 2개를 고친다 — 🔴 **두 가지를 같이** 해야 한다
     app/app-common            REDIS_SENTINELS  삭제
                               REDISHOST        mp-redis-master.data.svc → mp-redis-solo.data.svc
     pipeline/mp-pipeline-env  REDIS_SENTINELS  삭제
                               REDIS_URL        redis://mp-redis-master…  → redis://mp-redis-solo…
     🔴 **`REDIS_SENTINELS` 만 지우면 안 된다.** 폴백 대상 `mp-redis-master` 는
        **오퍼레이터가 소유한 Service** 라 4단계에서 함께 사라진다 → 그때 캐시가 전면 미스가 된다.

3. 🔴 **`rollout restart`** — `envFrom.configMapRef` 는 **파드 기동 시점에 주입**된다.
   ConfigMap 을 바꾸고 ArgoCD sync 해도 **도는 파드는 옛 값을 그대로 쓴다**(체크섬 어노테이션 없음).
   대상 = app ns 의 Redis 소비자 4종(price·chat·video·ocr) + pipeline ns 워크로드.
   ⚠️ 이 단계를 빼면 "정책은 초록인데 아무 것도 안 바뀐" 상태로 보인다.

4. 검증 후 철거:  RedisReplication `mp-redis` 삭제 → Sentinel 3파드·replica 동반 소멸
                  → redis-operator Application 삭제 → ns `redis-operator-system` 회수
                  → 임시 이름 `mp-redis-solo` 를 `mp-redis` 로 되돌리는 것은 **별 단계**(Service 이름
                    바꾸면 또 rollout restart 다). 이름을 처음부터 `mp-redis` 로 잡고 싶으면
                    1단계에서 기존 CR 을 먼저 지워야 하는데 그건 롤백 손잡이를 버리는 것이다.
                    ⇒ **권고 = `mp-redis-solo` 이름을 그대로 유지**한다. 이름의 아름다움보다
                      전환 중 롤백 가능성이 비싸다.

5. 관측 정리: `MpRedisNoReplica` 알람 **삭제**(복제본이 설계상 0이 된다 — 남기면 영구 발화) ·
   `monitoring/base/rules-data-tier.yaml` 의 Sentinel 관련 룰 정리 · handoff 문서 상태줄 갱신.
```

**되돌리기**: 2·3 단계를 역으로(ConfigMap 복원 + rollout restart). 1단계 새 Redis 는 남겨 둬도 무해하다.
4단계를 지난 뒤의 되돌리기는 오퍼레이터 재설치가 필요하니 **4단계 전에 최소 1일 관찰**한다.

## 5. 왜 이 단순화가 안전한가 (그리고 어디가 안 안전한가)

🟢 **안전한 근거 — 데이터가 캐시다**
```
keys 6 (4개가 TTL) · used_memory 2.88 MB · aof 0 · PVC 0
소비자 4곳이 "Redis 미가용이면 best-effort 우회" 를 명시적으로 구현 (price·chat 캐시 헬퍼 · ocr 인메모리 폴백)
⇒ 파드 재시작 = 캐시 워밍 손실뿐. 지금도 그렇다(save "" · appendonly no).
```

🔴 **안 안전한 곳 = `video` 하나다.** `services/video/app/store.py` 머리말이 직접 이렇게 적어 뒀다 —
*"Redis 장애 시 동작: 잡 저장/조회는 실패해야 정직하다"*. 즉 **의도적으로 폴백이 없다**(잡 상태는
캐시가 아니라 정본이다). 단일 Redis 는 replica 가 없으니 **파드 재시작 = video 잡 상태 유실**이다.
```
지금:   2 파드 + Sentinel → 재시작 1개면 다른 쪽이 받는다 (단, video 는 master Service 직결이라
                            페일오버 이득을 실제로 못 받고 있다 — §2 참조)
단일화: 1 파드          → 재시작 = 진행 중 잡 상태 유실
```
🔴 **그런데 지금도 유실된다.** `save ""` + `appendonly no` + PVC 0 이라 **master 파드가 재시작하면
   데이터가 사라진다.** replica 가 있어도 Sentinel 페일오버는 *파드 상실*을 덮지 물리적 재시작 후
   빈 마스터를 덮지 않고, 무엇보다 video 는 Sentinel 을 안 본다.
⇒ **단일화가 video 를 새로 취약하게 만드는 것이 아니라, 이미 있는 취약점을 드러낸다.**
   판단: 이것을 C-14 의 블로커로 두지 않는다. 대신 **별건**으로 올린다 —
   `1-14` 를 "video Redis 재시도 부재" 에서 **"video 잡 상태의 영속성 부재"** 로 재정의하는 것이
   맞다(재시도를 넣어도 빈 Redis 를 재시도할 뿐이다). 진짜 해법은 잡 상태를 PG 로 옮기거나
   Redis 에 AOF 를 켜는 것이고, 둘 다 C-14 와 독립이다.

## 6. 🔴 AWS(ElastiCache) 쪽 — **여기가 진짜 코드 변경이 필요한 곳이다**

§2 가 "단일화에 코드 변경 0" 이라고 했지만, **ElastiCache 전환은 다르다.**

```
실측: 코드베이스 전체에 `rediss://` 0건 · `ssl=True` 0건 · `ssl_cert_reqs` 0건
```
ElastiCache for Valkey 에 **전송 중 암호화(in-transit encryption)** 를 켜면:

| 소비자 | 접속 방식 | TLS 되나 |
|---|---|---|
| `pipelines/stream/_redis.py` · `pipelines/ingest/refresh_price_matview.py` | `Redis.from_url(REDIS_URL)` | 🟢 **된다** — `REDIS_URL` 을 `rediss://` 로 바꾸면 redis-py 가 TLS 를 켠다. **코드 변경 0** |
| `services/price` · `chat` · `video` · `ocr` | `Redis(host=…, port=…)` | 🔴 **안 된다** — `ssl=` 인자가 없다. 평문으로 붙어 실패한다 |

⇒ **C-14 의 코드 작업 = "Sentinel 되돌리기" 가 아니라 "host/port 4곳에 TLS 를 넣는 것"** 이다.
   방향이 반대였다. 권고 = 4곳을 `Redis.from_url(REDIS_URL)` 로 통일한다 —
   그러면 스킴 하나로 TLS·비TLS·포트가 다 표현되고, 사이트 분기가 **ConfigMap 값 하나**로 줄어든다
   (온프렘 `redis://mp-redis-solo.data.svc:6379/0` / AWS `rediss://<elasticache>:6379/0`).
   🔴 **암호화를 끄는 선택도 있다** — 같은 VPC 안이고 5인 규모다. 다만 그건
   *"사람이 정할 일"* 이고, 끄면 §1.1 보안 계층 모델의 "전송 중 암호화" 칸이 비게 된다.
   ES 가 이미 같은 예외(HTTP TLS 끔)를 갖고 있어 **두 번째 예외가 된다** — 그 사실을 알고 정할 것.

## 7. 미결 (사람이 정할 것)

- ① `video`·`ocr` 잡 상태의 영속성 — `1-14` 재정의(§5). C-14 와 독립이지만 같이 보는 게 낫다
- ② ElastiCache 전송 중 암호화 on/off (§6). on 이면 **4개 서비스에 코드 변경**이 붙는다
- ③ `mp-redis-pgsync` 를 앱 Redis 로 **합칠지** — 지금은 별 인스턴스다(PGSync 전용 브로커).
  🔴 합치면 파드가 하나 더 줄지만, **PGSync 의 CDC 상태와 앱 캐시가 한 장애 도메인**이 된다.
  #555(논리슬롯 무효화)를 겪은 트랙이라 **권고는 분리 유지**다. DB 번호로 나누는 것도
  같은 프로세스라 같은 재시작을 공유하므로 격리가 아니다
- ④ 임시 이름 `mp-redis-solo` 를 영구화할지(§4-4 권고 = 그대로 유지)
