# platform/observability — C-18 (MinIO 삭제 → S3) 의 자격증명 트랙

## 실측 (2026-08-10 · 읽기 전용)

**MinIO 소비자는 3개가 아니라 4개다.**

| 소비자 | 무엇 | 어디 |
|---|---|---|
| `observability/loki` | 청크·인덱스·ruler | `platform/argocd/base/loki.yaml:38` |
| `observability/tempo` | 트레이스 블록 | `platform/argocd/base/tempo.yaml:100` |
| `observability/minio` | 자기 자신 | Ansible `k8s_minio` |
| 🔴 **`data/mp-pg-onsite-dump`** | **PG 온사이트 논리 덤프** | `platform/pg/base/onsite-backup.yaml` |

내용물 (`du -sh /export/*`, 총 941 MiB / 49 GiB = **2%**):

```
loki           471 MiB   (retention 168h = 7일)
mp-pg-onsite   346 MiB   ← 🔴 전체의 36.8%. **백업 트랙**이다
tempo          122 MiB   (retention 168h)
models         4 KiB     ← 사실상 비어 있다(랭킹 모델 사본 0개 — 이슈 #561 과 같은 사실)
```

⟳ **체크리스트 정정 제안** — §D5 는 "실사용 658 MiB · 온사이트 318 MiB(35%)" 로 적혀 있는데
오늘 실측은 **941 MiB · 346 MiB(36.8%)** 다. 결론(용량은 문제가 아니다)은 그대로다.

## 이 PR 이 한 것

| | 무엇 | 왜 지금 |
|---|---|---|
| ① | `lgtm-minio-creds` 를 **Ansible → ESO** 로 이관 | 🔴 CLAUDE.md 가 *"ESO/config 로 이관하기 전에는 `k8s_platform_apps` 를 지우지 말 것"* 이라고 못 박은 그 이관이다. **MinIO 철거의 선행**이고, 이것만으로도 "config 레포만 봐도 자격증명 출처가 보인다" 가 성립한다 |
| ② | EKS 오버레이에서 Loki·Tempo → **S3**(+ `region` · TLS · IRSA) | 🔴 **EKS 에는 MinIO 가 없다**(C-18). 이걸 안 하면 이관 당일 관측이 통째로 안 뜬다 |
| ③ | Loki 의 `nodeSelector: kubernetes.io/hostname: k8s-worker-b1` **제거**(eks 만) | EKS 에 그 노드가 없어 **영구 Pending**. base 는 그대로 — 온프렘에선 여전히 참이다(로컬 PV 가 b1 에 묶여 있다) |

## 🔴 ② 가 왜 4개를 한 묶음으로 가는가 (1-19)

```
① endpoint  minio.observability.svc:9000  →  s3.ap-northeast-2.amazonaws.com
② region    🔴 **실측 0건** — 온프렘 loki·tempo 어디에도 region 이 없다.
            MinIO 는 region 을 안 봤지만 S3 는 본다. 없으면 SDK 가 기본 리전(us-east-1)으로
            붙어 ap-northeast-2 버킷을 못 찾는다(301/PermanentRedirect).
③ insecure: true → false · forcepathstyle: true → false      ← 1-19 의 "뒤집기"
④ extraEnvFrom(lgtm-minio-creds) 삭제 + -config.expand-env 삭제
   🔴 그 시크릿이 EKS 에 없다. 남기면 파드가 CreateContainerConfigError 로 **아예 안 뜬다.**
   ⇒ 자격증명은 IRSA. accessKeyId/secretAccessKey 를 **적지 않으면** AWS SDK 기본 체인을 탄다.
```
하나만 하면 조용히 실패한다. ③만 먼저 하면 평문 HTTP 로 S3 에 붙으려 하고, ①만 하면 리전을 못 찾는다.

🟢 **egress 는 이미 열려 있다** — `platform/policies-observability/overlays/eks/netpol-observability-egress.yaml`
의 `mp-observability-egress-loki` · `-tempo` 가 `s3.ap-northeast-2.amazonaws.com` + STS 2개를
`toFQDNs` 로 허용한다(다른 레인이 먼저 깔아 뒀다). 이 PR 은 그 정책의 짝이다.

## 🔴 ① 전환 절차 — "무중단" 이 아니라 **"무변화"** 여야 한다

값이 지금 라이브와 **같아야** 한다. 다르면 Loki·Tempo 가 다음 재시작에 스토어 인증에 실패하고,
**Tempo 는 store init 실패 시 fail-fast** 다(base 주석: 그래서 421회 재시작으로 먼저 터졌다).

```bash
# 0) 지금 값의 출처 = Ansible secrets.yml 의 minio_root_user / minio_root_password
#    (roles/k8s_platform_apps/templates/lgtm-minio-secret.yaml.j2 가 그대로 넣는다)
#    🔴 값을 터미널에 출력하지 말 것. 아래는 **키 목록만** 본다.
kubectl -n observability get secret lgtm-minio-creds \
  -o go-template='{{range $k,$v := .data}}{{$k}}{{"\n"}}{{end}}'
#   → MINIO_ACCESS_KEY_ID / MINIO_SECRET_ACCESS_KEY

# 1) fb-secrets 에 적재 (사람 · 1회). 값은 secrets.yml 의 그 두 값과 **동일**해야 한다
kubectl -n fb-secrets create secret generic observability-secrets \
  --from-literal=LGTM_OBJECT_STORE_ACCESS_KEY_ID='<minio_root_user>' \
  --from-literal=LGTM_OBJECT_STORE_SECRET_ACCESS_KEY='<minio_root_password>'

# 2) 같은지 확인 — 🔴 값을 찍지 않고 해시만 비교한다
kubectl -n observability get secret lgtm-minio-creds \
  -o go-template='{{.data.MINIO_ACCESS_KEY_ID}}' | sha256sum
kubectl -n fb-secrets get secret observability-secrets \
  -o go-template='{{.data.LGTM_OBJECT_STORE_ACCESS_KEY_ID}}' | sha256sum
#   → 두 sha256 이 같아야 한다. 다르면 3) 으로 가지 말 것

# 3) sync (manual)
kubectl patch application -n argocd observability-secrets --type merge \
  -p '{"operation":{"sync":{"revision":"HEAD"}}}'
kubectl -n observability get externalsecret mp-lgtm-object-store   # SecretSynced / Ready

# 4) 소비자가 안 죽었는지 — 🔴 재시작 카운트가 안 늘어야 한다
kubectl -n observability get pods loki-0 tempo-0

# 5) 🔴 실제 왕복 — 재시작을 **일부러 한 번** 시켜 새 시크릿으로도 스토어에 붙는지 본다.
#    이걸 안 하면 "도는 파드는 옛 값을 들고 있어서 무증상" 인 상태를 초록으로 오독한다.
kubectl -n observability rollout restart statefulset/tempo   # fail-fast 라 제일 빨리 드러난다
kubectl -n observability logs sts/tempo --tail=50 | grep -i "started\|error"
```

**되돌리기**: Ansible 롤이 아직 살아 있으므로 `ansible-playbook k8s.yml --tags platform_apps`
한 번이 시크릿을 원래 값으로 되돌린다(멱등). 🔴 그래서 **롤 제거는 이 전환이 검증된 뒤**다 —
같은 PR 에 묶지 않았다.

## 🔴 아직 안 한 것 = 온프렘 MinIO 철거 (막혀 있다)

C-18 의 나머지 절반이다. **지금 하면 안 되는 이유가 넷이다:**

| | 막는 것 | 무엇이 필요한가 |
|---|---|---|
| ① | **S3 버킷이 없다** | `mp-observability-eks`(가칭) 생성 = AWS 계정(C-8) 미생성. 🔴 이름·계정이 정해지기 전에 온프렘을 끊으면 갈 곳이 없다 |
| ② | 🔴 **온프렘은 IRSA 가 불가** | EKS 밖이라 **정적 IAM 키**가 생긴다 = 체크리스트가 부르는 *"이 설계의 유일한 보안 후퇴"*(③번 항목). 온프렘 Loki·Tempo 를 S3 로 보낼지 자체가 **사람 결정**이다 — 온프렘은 C-3 으로 계속 사는데, 관측 데이터(retention 7일·파생)를 위해 정적 키를 하나 더 만들 값이 있는가 |
| ③ | 🔴 **`data/mp-pg-onsite-dump` 가 죽는다** | MinIO 가 목적지다(346 MiB = 내용물의 36.8%). C-18 은 *"온사이트 덤프는 barman 과 다른 버킷/계정"* 을 요구하고 그건 **2-8**(별건)이다. 🔴 MinIO 를 먼저 지우면 **PG 백업 3트랙 중 하나가 조용히 멈춘다** — CronJob 은 성공으로 마감될 수도 있다 |
| ④ | 데이터 | loki 471 + tempo 122 MiB. retention 이 **둘 다 168h(7일)** 이라 마이그레이션 없이 버려도 되지만, **그건 사람이 정할 일**이다("지난 7일 관측 이력을 버린다" 는 선언) |

**철거 순서(막힌 것이 풀린 뒤)**:
```
1. S3 버킷 생성 + IAM (①②)
2. mp-pg-onsite-dump 목적지 이전 (③ · 2-8 트랙) ← 🔴 이게 먼저다
3. 온프렘 loki·tempo → S3 (이 PR 의 eks 패치를 onprem 오버레이에도)
4. netpol: mp-minio-egress 삭제 · loki·tempo egress 에 S3 FQDN 추가
5. Ansible `k8s_minio` 롤 제거 + `k8s_platform_apps` 시크릿 태스크 제거
6. MinIO 콘솔 Service + HTTPRoute(minio.mealbong.cloud) 제거 → C-9 "내부 도구 6종" → **5종**
7. PVC 50Gi 회수 (b2 openebs-vg 여유가 16Gi 였다 — 이게 실질 이득이다)
```
🔴 **2 → 3 → 5 순서를 뒤집으면 백업이나 관측이 조용히 멈춘다.**

## ⚠️ 부수 발견 — 지금 자격증명은 MinIO **root** 다

`k8s_platform_apps` 템플릿이 `minio_root_user`/`minio_root_password` 를 그대로 넣는다
(템플릿 주석도 "root 계정 재사용" 을 인정하고 있다). 즉 observability ns 가 MinIO
**전 버킷**(loki·tempo·models·**mp-pg-onsite**)의 root 를 들고 있다 —
observability 가 뚫리면 PG 온사이트 덤프까지 넘어간다.

🔴 **이 PR 에서 버킷별 계정 분리를 하지 않았다.** C-18 로 MinIO 자체가 소멸하므로
**소멸할 자격증명에 부품을 붙이지 않는다.** 다만 위 철거가 미뤄지는 동안은 이 노출이 유지된다 —
철거 일정이 밀리면 그때 분리를 재평가할 것.
(대조: `mp-pg-onsite-minio` 는 처음부터 전용 키다 — `platform/pg/base/externalsecrets.yaml` 주석.)
