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
