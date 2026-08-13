# platform/pg — sync 전 사람이 할 일

정본: 실행 순서·게이트 = `food-budget-app/docs/mp_k8s_p2_data_runbook.md`.

## 1. fb-secrets 에 `data-secrets` 적재 (ESO 선행조건)

```bash
kubectl -n fb-secrets create secret generic data-secrets \
  --from-literal=STREAMING_REPLICA_PASSWORD='<VM streaming_replica 비밀번호 — infra/ansible/secrets.yml>' \
  --from-literal=PGSYNC_PG_PASSWORD='<VM pgsync 롤 비밀번호 — basebackup 으로 그대로 승계됨>' \
  --from-literal=PG_BACKUP_AWS_ACCESS_KEY_ID='<mp-backup IAM — ~/.aws/credentials [mp-backup]>' \
  --from-literal=PG_BACKUP_AWS_SECRET_ACCESS_KEY='<동일>'
```

- 검증: `kubectl -n data get externalsecret` 전부 `SecretSynced`/Ready.
- 값의 정본은 여기 적지 않는다(레포에 비밀 금지 — README 규칙).

## 2. VM pg_hba 에 worker-a1(.20) 줄 확인

파드→VM 은 노드 IP 로 SNAT 되므로 **4개 노드 전부** 필요하다. `.17/.18/.19` 는 §2-A-4 적용분,
`.20` 은 §2-C-0 항목 — 미적용이면 data_tier 롤 재실행(멱등).

검증(아무 노드에서): `psql "host=192.168.0.8 user=streaming_replica dbname=postgres replication=1" -c 'IDENTIFY_SYSTEM'`

## 3. promote 장전 (T-1, 런북 §4 T-1일)

`cluster.yaml` 의 `replica.enabled: true → false` 커밋을 **머지만** 해 둔다(장전).
전환창 §4-4 에서 ArgoCD manual sync 1클릭이 발사다. selfHeal 이 없으므로(child 는 manual)
머지돼 있어도 sync 전에는 아무 일도 일어나지 않는다.

## 4. barman S3 경로 사이트 분기 (0-23 · 0-30) — 적용·검증·되돌리기

> 2026-08-10. **왜** 가르는지는 `base/objectstore.yaml` 머리말에 있다. 여기는 **어떻게** 다.
> 🔴 `pg` Application 은 **manual sync** 다(실측: `syncPolicy` 에 `automated` 없음 · `retry` 만).
> 즉 **머지 ≠ 적용**이다. 아래 sync 를 사람이 돌릴 때까지 라이브는 안 바뀐다.

### 4-1. 이 PR 이 온프렘 라이브에 만드는 변화 = 필드 1개

렌더 결과를 라이브 CR 과 필드 단위로 비교한 실측(2026-08-10):

```
DIFF  configuration.serverName   rendered = pg      live = (없음)
      그 밖의 모든 필드           일치
```

`serverName` 을 안 적으면 플러그인이 **Cluster 이름**으로 기본값을 잡는다. Cluster 이름이 `pg` 이고
라이브 `status.serverRecoveryWindow` 의 키도 `pg` 다 ⇒ **실효값이 이미 `pg`**. 이 커밋은 그 암묵값을
글자로 고정하는 것이므로 **S3 경로는 1바이트도 안 움직인다.** `pg-eks` 는 `overlays/eks` 에만 있고
그 오버레이는 아직 배포 대상이 아니다(`validate.py` 도 eks 를 스킵한다).

### 4-2. 적용 (온프렘)

경로가 안 움직이므로 정비창이 필요 없다. 다만 **플러그인 설정 변경이므로** 인스턴스가 굴려지지
않는지는 눈으로 본다(굴러도 pooler 가 재접속을 흡수하지만, 그건 확인할 일이지 가정할 일이 아니다).

```bash
# 0) 사전 기록 — 되돌릴 때 비교 기준
kubectl -n data get objectstore mp-pg-backup -o yaml > /tmp/os-before.yaml
kubectl -n data get cluster pg -o jsonpath='{.status.phase}{"\n"}'
kubectl -n data get pods -l cnpg.io/cluster=pg -o wide     # 재시작 카운트 기록

# 1) sync (manual)
kubectl patch application -n argocd pg --type merge \
  -p '{"operation":{"sync":{"revision":"HEAD"}}}'

# 2) 파드가 굴렀는지 — RESTARTS 가 0)과 같아야 한다
kubectl -n data get pods -l cnpg.io/cluster=pg -o wide

# 3) 아카이빙이 살아 있는지 (이게 진짜 게이트다)
kubectl -n data get cluster pg \
  -o jsonpath='{.status.conditions[?(@.type=="ContinuousArchiving")]}{"\n"}'
#   → status:"True" 여야 한다. "False" 면 즉시 4-4 로 되돌린다.

# 4) 경로가 그대로인지 — 키가 여전히 `pg` 하나여야 한다
kubectl -n data get objectstore mp-pg-backup \
  -o jsonpath='{.status.serverRecoveryWindow}{"\n"}'
#   → {"pg":{...}} · 🔴 `pg` 아닌 키가 새로 생겼으면 경로가 움직인 것이다
```

### 4-3. 왕복 증명 (sync 후 1회 · 새 base backup 이 실제로 앉는지)

WAL 아카이빙 조건이 True 여도 그건 "명령이 성공했다" 지 "복구할 수 있다" 가 아니다.
🔴 **읽기 전용 확인만으로는 부족하다** — base backup 을 한 번 실제로 받아본다.

```bash
kubectl -n data create -f - <<'EOF'
apiVersion: postgresql.cnpg.io/v1
kind: Backup
metadata: {name: mp-pg-verify-0023, namespace: data}
spec:
  cluster: {name: pg}
  method: plugin
  pluginConfiguration: {name: barman-cloud.cloudnative-pg.io}
EOF

kubectl -n data get backup mp-pg-verify-0023 -w    # phase → completed
```

기대 소요 = **약 40초**(실측: `mp-pg-daily-20260809180000` 이 18:00:00→18:00:39 = **39초**).
완료 후 `firstRecoverabilityPoint` 가 유지되고 `lastSuccessfulBackupTime` 이 방금으로 갱신되면 끝.

### 4-4. 되돌리기

경로가 안 움직이므로 되돌리기도 데이터 조치가 아니다 — **커밋 되돌리고 다시 sync**:

```bash
# config 레포에서 이 PR 을 revert 한 PR 을 머지한 뒤
kubectl patch application -n argocd pg --type merge \
  -p '{"operation":{"sync":{"revision":"HEAD"}}}'
kubectl -n data get cluster pg \
  -o jsonpath='{.status.conditions[?(@.type=="ContinuousArchiving")]}{"\n"}'
```

긴급 시 직접 되돌리기(레포와 라이브가 갈리므로 **직후에 revert PR 을 반드시 낸다**):
`kubectl -n data patch objectstore mp-pg-backup --type merge -p '{"spec":{"configuration":{"serverName":null}}}'`

### 4-5. 🔴 아직 안 한 것 (다음 사람에게 넘기는 것)

| | 무엇 | 왜 여기서 안 했나 |
|---|---|---|
| ① | 온프렘을 `pg` → `pg-onprem` 으로 **대칭 리네임** | 새 경로엔 base 가 0개다. 첫 base(**39초**)가 앉기까지 최근 데이터의 복구 지점이 없다. 이름 대칭은 그 값을 못 한다. **0-20**(bootstrap 이 파괴된 `192.168.0.8` 을 가리켜 Cluster 재생성이 필요)에 얹으면 공짜다 |
| ② | IAM 분리 — 온프렘 정적 키 `pg/*` 만 · EKS IRSA 롤 `pg-eks/*` 쓰기 + `pg/*` **읽기** | AWS 계정이 아직 없다(C-8). 🔴 `pg/*` 읽기를 빼먹으면 **C-51 페일백의 원본을 못 읽는다** |
| ③ | 버킷/계정 분리 | 같은 버킷에 tfstate·barman 이 이미 있다. C-18 이 요구하는 "다른 버킷/계정" 은 *온사이트 덤프* 트랙(2-8) 요구다 — barman 프리픽스 분리와는 별건 |
| ④ | 옛 경로 정리 | 리네임을 안 했으므로 **정리할 옛 경로가 없다**. ①을 하면 그때 `pg/pg/` 를 30일(보존창) 지나 지운다 |

🔴 **`cluster.yaml` 쪽에 남는 제약** (이 레인 소유가 아니므로 전달만): EKS 최초 시딩을 온프렘
아카이브에서 받는다면 `bootstrap.recovery` + `externalClusters` 의 serverName 은 **온프렘 값 `pg`** 다.
거기에 `pg-eks` 를 적으면 빈 경로를 읽어 부트스트랩이 실패한다. *쓰는 곳*과 *읽는 곳*은 다른 필드다.
