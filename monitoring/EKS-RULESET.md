# 1-28 — EKS 알림 룰셋: 무엇을 남기고 무엇을 뺐나

> 신설 2026-08-13. 근거 = 앱 레포 `docs/mp_aws_prep_checklist.md` **1-28** · 결정 **C-83**.
> 구조 정본 = `SITES.md` · 온프렘 관측 분기 명세 = `monitoring/SITE-SPLIT.md`.
> 🔴 **온프렘 룰은 손대지 않는다.** 오탐 정리는 이관 후 별건이다(C-83 형상 동결).

## 0. 방침 — 199개를 감사하지 않는다

체크리스트 1-28 은 "**알림 룰 199개 중 유효 발화 28종(14.1%)**" 이다.
그 14.1% 를 온프렘에서 끌어올리는 작업은 **이관과 무관하게 비싸고**, C-83 아래서는 만질 수도 없다.
그래서 방향을 뒤집었다 — **EKS 쪽 룰셋만 새로 정한다.** 온프렘 오탐은 이관 후 정리 대상으로 남긴다.

## 1. 숫자의 출처 (재현 가능)

### 1-1. 분모 — 라이브 알림 룰 수

```bash
# PrometheusRule 전수에서 alert 룰만 센다 (record 룰 제외)
kubectl get prometheusrules -A -o go-template='{{range .items}}{{range .spec.groups}}{{range .rules}}{{if .alert}}{{.alert}}{{"\n"}}{{end}}{{end}}{{end}}{{end}}' | wc -l
```

실측 **2026-08-13 = 202** (체크리스트의 199 는 측정 시점 차이. `monitoring` 은 manual-sync 라
git 이 라이브보다 앞서 있다 — `mp-app-sli` git 4 / 라이브 3, `mp-container-memory` git 5 / 라이브 2).

| 출처 | 알림 수 | 소유 |
|---|---:|---|
| kube-prometheus-stack 빌트인 15 그룹 | **134** | 차트 |
| `mp-*` (이 레포) 14 PrometheusRule | **68** | config 레포 |
| **합계** | **202** | |

### 1-2. 분자 — "유효 발화 28종" 의 정의

```promql
count by (alertname) (max_over_time(ALERTS{alertstate="firing"}[15d]))
```

실측 결과 **30종**. 여기서 둘을 뺀 **28종** 이 체크리스트의 숫자다 — 28/199 = **14.07%** 로 정확히 맞는다:

- `Watchdog` — 항상 발화하도록 설계된 데드맨 스위치(파이프라인 생존 확인용). 사건이 아니다.
- `InfoInhibitor` — 억제 규칙의 보조 신호. Alertmanager 에서 `"null"` 리시버로 버려진다.

🔴 **그러므로 "유효 발화" 는 "옳다" 가 아니라 "15일 보존창 안에 한 번이라도 울렸다" 이다.**
오탐도 여기 들어 있다(예: `MpDeploymentPodsOnSingleZone` 은 온프렘에서 참이었지만
EKS 에서는 구조적으로 오발화한다 — §3). 이 지표는 **"룰의 86%는 관측된 적 없는 가정"** 이라는
뜻이지, "28개는 믿어도 된다" 는 뜻이 아니다.

### 1-3. 28종의 내역

| 부류 | 수 | 알림 |
|---|---:|---|
| 스택 빌트인 | 13 | `TargetDown`(15회) · `KubeJobFailed`(11) · `KubePodNotReady`(10) · `KubePodCrashLooping`(9) · `KubeStatefulSetReplicasMismatch` · `KubeDeploymentReplicasMismatch` · `KubeContainerWaiting` · `KubeDeploymentRolloutStuck` · `KubeHpaMaxedOut` · `KubeMemoryOvercommit` · `KubeStatefulSetUpdateNotRolledOut` · `AlertmanagerFailedToSendAlerts` · `AlertmanagerClusterFailedToSendAlerts` |
| `mp-*` — **AWS 에서도 유효** | 7 | `MpAppHighP95Latency` · `MpContainerMemoryNearLimit` · `MpPGSyncDown` · `MpPGSyncCrashLooping` · `MpPGWALArchiveFailing` · `MpPGReplicationSlotRetainedWALHigh` · `MpPGReplicationSlotWALGrowing` |
| `mp-*` — **온프렘 물리 전용** | 4 | `MpHypervisorTempCritical` · `MpHypervisorTempHigh` · `MpHypervisorDiskReadBurst` · `MpHostCDown` — ✅ 0-3c 로 이미 eks 에서 제외됨 |
| `mp-*` — **크롤 파이프라인**(C-3 게이트) | 3 | `MpPollerStale` · `MpKurlyLatestRunFailed` · `MpConsumerLagUnobserved` — §4 미결 |
| `mp-*` — **EKS 에서 깨진다** | 1 | `MpDeploymentPodsOnSingleZone` — §3 에서 재작성 |

## 2. 판정 축 — 발화 의미론이 전부다

EKS 에 대상이 없는 룰이 **어떻게 실패하는지**는 expr 의 모양이 정한다. 이걸 안 가르면
"EKS 에 없으니 다 빼자" 라는 과잉 삭제나 "조용하니 놔두자" 라는 과소 대응이 된다.

| expr 모양 | 대상이 없을 때 | 결과 |
|---|---|---|
| `absent(X)` · `absent_over_time(X[…])` | 조건이 **참** | 🔴 **영구 발화** — 반드시 처리 |
| `X == 0` · `time() - X > N` · `X > N` | 시리즈가 비어 결과가 **빈 벡터** | 침묵 — 시끄럽진 않지만 **"감시 중" 이라는 착각**을 남긴다 |

`mp-*` 68개를 이 축으로 기계 분류한 결과: **absent 계열 23 / threshold 계열 45**.
0-3c(`mp-physical-layer`)와 C-18(`mp-minio`)이 먼저 처리된 것도 전부 absent 계열이었다.

## 3. ✅ 이번에 한 것 (config 레포 · `monitoring/overlays/eks`)

### ① `mp-workload-spread` 통째 교체 → `rules-workload-spread.yaml`

base 는 zone 을 노드 **이름 정규식**(`k8s-worker-([ab])[0-9]+` · `k8s-master`)으로 파생한다.
EKS 노드 이름은 `ip-10-…` 이라 하나도 안 맞아 **두 가지가 동시에** 터진다:

- `MpNodeZoneMapUnknown` → 셀렉터가 "이름 규칙에 안 맞는 노드" 라 **전 노드 영구 발화**
- zone 이 전부 빈 라벨로 접혀 `mp:deployment_single_zone:bool` 이 항상 1 →
  `MpDeploymentPodsOnSingleZone` 이 **replica 2 이상 전 Deployment 오발화**

**EKS 에서는 오히려 더 정확해진다.** 온프렘이 이름 규칙을 쓴 이유는 KSM 이 `kube_node_labels` 를
안 내보냈기 때문이고(2026-07-31 실측 0건 · **2026-08-13 재확인 여전히 0건**), EKS 노드에는
`topology.kubernetes.io/zone` 에 **진짜 AZ** 가 붙어 있다.
그 전제였던 KSM allowlist 를 **이번에 켤 수 있게 됐다** — 0-3 이 EKS 쪽 kube-prometheus-stack 을
config 소유로 신설하면서 values 가 이 레포 안으로 들어왔기 때문이다:

```yaml
# platform/argocd/overlays/eks/kube-prometheus-stack.yaml
kube-state-metrics:
  extraArgs: ["--metric-labels-allowlist=nodes=[topology.kubernetes.io/zone]"]
```

**라이브 검산** (2026-08-13, apiserver proxy 경유 read-only 쿼리):

| 확인 | 결과 |
|---|---|
| 새 join 식의 PromQL 문법 | `status: success` (결과는 빈 벡터 — allowlist 가 아직 꺼져 있어서) |
| join 모양 자체(`kube_node_labels` 대신 `kube_node_info`·`internal_ip` 로 치환해 실행) | **61 Deployment** 반환. `coredns`·`cilium-operator` 가 2, 단일 replica 는 1 — 의도한 의미 그대로 |
| 가드 알림 `count(kube_node_info) - (count(kube_node_labels{…!=""}) or vector(0))` | **5** (= 전 노드가 zone 미상) → allowlist 누락을 정확히 잡는다 |

🔴 **가드에서 밟은 함정** — 처음에 `kube_node_labels{label_topology_kubernetes_io_zone=""} > 0` 로 썼는데,
allowlist 가 꺼져 있으면 `kube_node_labels` 가 **시리즈 0개**라 셀렉터가 아무것도 안 잡고
**빈 벡터 = 침묵**이 된다. 가장 중요한 케이스에서 안 울리는 가드였다.
`count(...) or vector(0)` 로 0 을 만들어 빼는 형태로 고쳤다(위 표 3행이 그 검산이다).

⚠️ `MpDeploymentPodsOnSingleZone` 은 **C-29(AZ 당 노드 1대) 아래서 구조적으로 휴면**이다 —
가드가 `노드 2종 이상`인데 1:1 이면 노드 2종 = zone 2종이라 조건이 성립하지 않는다.
고장이 아니라 토폴로지의 결과이고, AZ 당 노드가 늘면 즉시 살아난다. 그래서 지우지 않았다.
이 국면에서 신호를 나르는 것은 `MpDeploymentPodsOnSingleNode` 다.

### ② `mp-backup` — AWS 에 **감시 대상 자체가 없는** 4알람 제거 (9 → 5)

| 제거 | 왜 |
|---|---|
| `MpBackupEtcdStale` | EKS 컨트롤플레인은 관리형 — **우리가 백업할 etcd 가 없다** |
| `MpBackupSourceStale` | 소스 레포 백업은 호스트 C(`.10`) = 온프렘 전용 |
| `MpBackupImageNeverArchived` | Harbor 이미지 아카이브. AWS 는 ECR 이고 복제는 AWS 몫 |
| `MpBackupPgOnsiteDumpStale` | `cronjob="mp-pg-onsite-dump"` = 온사이트 MinIO 덤프. C-18 로 MinIO 없음 |

넷 다 `time() - X > N` / `== 0` 이라 **지금도 조용하다.** 그런데도 지운 이유는 §2 의 두 번째 줄이다 —
남기면 AWS 온콜이 룰 목록에서 `MpBackupEtcdStale` 을 보고 "etcd 백업은 감시되고 있다" 로 읽는다.

**남긴 5개** = `MpBackupProbeMissing` · `MpBackupProbeFailed` · `MpBackupWalArchivingStalled` ·
`MpBackupPgBaseStale` · `MpBackupSecretsStale`. 대상이 AWS 에도 실재한다
(CNPG barman-cloud → S3 의 WAL·베이스 백업 = C-15 · 비밀 = Secrets Manager).

🔴 **`MpBackupProbeMissing`(= `absent(mp_backup_check_timestamp_seconds)`)은 EKS 에서 즉시 발화한다.
   그리고 그것이 맞다.** `mp_backup_*` 를 만드는 주체는 앱 레포 Ansible 롤(`backup_freshness`)의
   node-exporter **textfile collector** 이고 온프렘 호스트에만 있다. 즉 이 발화는 오탐이 아니라
   **참인 사실**이다 — "AWS 에 백업 신선도 관측이 없다."
   ⇒ 끄면 그 사실이 사라지고 아무도 안 만든다. **프로브의 EKS 이식을 선행 작업으로 승격할 것**(아래 §5).

## 4. 🔴 다른 레인으로 넘기는 것 (여기서 안 고쳤다)

전부 **absent 계열 = EKS 에서 영구 발화**다. 고칠 자리가 이 레인이 소유하지 않은 트랙의
eks 오버레이라, 같은 파일을 두 세션이 동시에 고치면 충돌한다. 패치 조각까지 적어 둔다.

### ⓐ `mp-redis-ha` 2알람 — **C-14 레인**

`platform/redis/base/monitoring.yaml`. 둘 다 `absent(...) or …` 형태다:

```promql
MpRedisNoReplica:          absent(redis_connected_slaves{namespace="data"}) or max(...) < 1
MpRedisMasterCountAbnormal: absent(redis_instance_info{namespace="data"}) or count(...) != 1
```

C-14 = EKS 는 **ElastiCache for Valkey** → `data` ns 에 Redis 파드도 exporter 도 없다
→ **둘 다 critical 로 영구 발화**한다. 이 레인에서 만들 수 있는 가장 시끄러운 오탐이다.

`platform/redis/overlays/eks/kustomization.yaml` 은 지금 순수 통과이고, 그 파일 머리말이
이미 *"이 트랙은 EKS 에서 통째로 사라진다 — `resources: []` 가 정답일 가능성이 높다.
**Wave B 에서 확정할 것**"* 이라고 적어 뒀다.
⇒ **그 결정이 먼저다.** 트랙을 통째로 비우면 이 2알람도 같이 사라져 별도 패치가 불필요하고,
반대로 내가 지금 `$patch: delete` 를 넣으면 그쪽이 `resources: []` 로 갈 때 **타깃 없는 패치로 렌더가 죽는다.**

### ⓑ `MpConsumerLagUnobserved` · `mp-kafka` 그룹 — **레인 A(크롤 Kafka→S3/SQS)**

```promql
MpConsumerLagUnobserved:   absent_over_time(kafka_consumergroup_lag{consumergroup="retail-refiner"}[10m]) or …
MpKafkaMetricsUnavailable: (mp-data-tier / mp-kafka 그룹, absent 계열)
```

C-3 은 크롤(CronJob 7 + **Kafka 3브로커**)을 **온프렘 상시 프로덕션**으로 남긴다.
그렇다면 EKS 에 `kafka_consumergroup_lag` 가 없고 두 알림은 영구 발화한다.
🔴 **그런데 `pipelines` 와 `platform/kafka` 둘 다 eks 오버레이가 있다** — 즉 "EKS 에도 Kafka/컨슈머가
있는가" 가 아직 확정이 아니다. **내가 정할 사안이 아니다.** 레인 A 의 S3/SQS 전환 결론에 달렸다:

- EKS 에 Kafka 없음으로 확정 → `pipelines/overlays/eks` 에서 `MpConsumerLagUnobserved` 제거 +
  `monitoring/overlays/eks` 에서 `mp-data-tier` 의 `mp-kafka` 그룹 제거(`mp-minio` 와 같은 방식)
- EKS 에도 Kafka 있음 → 둘 다 그대로 유효

파이프라인 나머지 8알람은 전부 threshold 계열이라 **침묵**한다 — 급하지 않다.

## 5. 🔴 미결 (사람 결정)

① **백업 신선도 프로브의 EKS 이식** — 위 §3② 참조. 지금 상태로 이관하면
   `MpBackupProbeMissing` 이 day-1 부터 critical 로 울린다. 그것이 참이라는 게 문제의 핵심이다.
   앱 레포 `backup_freshness` 롤은 node-exporter textfile 에 쓰는데, EKS 에서 같은 자리를
   어떻게 만들지(CronJob + Pushgateway? CloudWatch?)가 정해진 바 없다.
② **스택 빌트인 134개는 손대지 않았다** — 의도다. §1-2 의 발화 조사는 **온프렘 클러스터 것**이라
   EKS 에서 무엇이 시끄러울지의 근거가 되지 못한다. 근거 없이 `defaultRules.disabled` 를 채우면
   진짜 신호를 끄게 된다. **이관 후 15일이 지나면 §1-2 의 쿼리를 EKS 에서 그대로 한 번 더 돌려**
   같은 방식으로 정리한다(그때는 `site="aws"` 로 갈린다 — 0-3b 가 그것을 위해 있다).
③ **`group_by` 에 `site` 를 넣을 것인가** — C-22 로 Alertmanager 는 사이트별 각자라 기술적으로는
   불요. 다만 **같은 Slack 채널**로 들어와 사람 눈에는 섞인다. `SITE-SPLIT.md` §2 부작용 ②와 같은 질문.

## 6. 검증

```bash
# ① 온프렘 렌더 무변화 (안전기준)
kubectl kustomize monitoring/overlays/onprem | sha256sum        # 작업 전후 동일

# ② eks 에서 빠진 것
kubectl kustomize monitoring/overlays/eks | grep -c mp-physical-layer   # 0  (0-3c)
kubectl kustomize monitoring/overlays/eks | grep -c 'name: mp-minio'    # 0  (C-18)
kubectl kustomize monitoring/overlays/eks | grep -c MpBackupEtcdStale   # 0  (1-28 ②)
kubectl kustomize monitoring/overlays/eks | grep -c 'k8s-worker-'       # 0  (1-28 ①)

# ③ eks 에서 살아 있는 것
kubectl kustomize monitoring/overlays/eks | grep -c MpBackupProbeMissing        # 1 (의도된 참 발화)
kubectl kustomize monitoring/overlays/eks | grep -c label_topology_kubernetes_io_zone  # >0

python3 scripts/validate.py
```
