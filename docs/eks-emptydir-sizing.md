# 0-8d — emptyDir `sizeLimit` 사이징 (EKS 오버레이 전용)

> 신설 2026-08-13. 근거 = 앱 레포 `docs/mp_aws_prep_checklist.md` **0-8d** · 결정 **C-83**(온프렘 동결·AWS 는 덧셈만) · **C-16**(EBS gp3) · **C-29**(`m7g.xlarge` × 3).
> 🔴 이 문서는 **값의 근거**다. 값 자체는 각 트랙의 `overlays/eks/kustomization.yaml` 에 있다.

## 왜 상한이 필요한가

`sizeLimit` 없는 emptyDir 은 **노드 루트 볼륨(ephemeral-storage)** 을 무제한으로 먹는다.
한 파드가 폭주하면 노드가 `DiskPressure` 로 들어가고 kubelet 이 **그 파드가 아니라 노드 위의
다른 파드들까지** 축출한다. 상한이 있으면 초과한 파드 **하나만** 축출된다.

🔴 **반대 위험도 실재한다** — 상한을 실사용량보다 낮게 잡으면 평상시에 파드가 축출된다.
그래서 이 항목은 "적당히 걸어 두는" 것이 아니라 **먼저 재고 그 숫자로 정하는** 것이다.

🔴 **온프렘에는 걸지 않는다**(C-83). eks 오버레이에만 있으므로 **온프렘 축출 위험 = 0**.

## 실측 (2026-08-13 · 라이브 5노드 · 파드 175개)

측정법 = `kubelet /stats/summary` 의 pod-level `volume` 엔트리를 파드 spec 의 emptyDir 정의와
조인(`kubectl get --raw /api/v1/nodes/<node>/proxy/stats/summary`). PVC·projected 는 제외.

| 구분 | 개수 |
|---|---:|
| emptyDir 전체 | 203 |
| 그중 `sizeLimit` **없음** | **190** |
| └ `medium: Memory`(RAM 사용 · 노드 디스크 아님) | 25 |
| └ **디스크 기반 = 실제 노드 볼륨을 먹는 것** | **165** |

🔴 체크리스트의 "**160개**" 는 이 **165**(디스크 기반)와 같은 수다 — 파드 수 변동 범위 안이다.
`medium: Memory` 25개(`istio-envoy` 20 · `shm` 2 · `config-out` 2 · `local-certs` 1)는
**노드 디스크가 아니라 파드 메모리 한도**를 먹으므로 이 항목의 대상이 아니다. 섞어 세면 안 된다.

### 165개가 실제로 얼마나 쓰고 있나

관측된 157개(스케줄 직후라 stats 에 아직 안 잡힌 8개 제외) 합계 = **1,139 MiB**.

| 백분위 | 사용량 |
|---|---:|
| p50 | 4 KiB |
| p75 | 28 KiB |
| p90 | 7.4 MiB |
| p99 / 최대 | **209 MiB** |

노드별 emptyDir 합계 3.0 / 135.7 / 207.2 / 339.2 / 453.9 MiB — 노드 ephemeral 용량
**47.4 GiB(allocatable 43.7 GiB)** 대비 **최대 1.0%**.
⇒ **지금 이 클러스터에서 emptyDir 은 디스크 압박의 원인이 아니다.** 상한은 폭주 방어이지 절감이 아니다.

### 🔴 그런데 큰 것들은 **이 레포가 못 건드린다**

상위 소비자를 소유자별로 갈라 보면 이 레포의 사정이 드러난다:

| 소유자 | 개수 | 최대 | 이 레포에서 `sizeLimit` 설정 | 왜 |
|---|---:|---:|---|---|
| **Istio 사이드카 주입분**(`workload-socket`·`credential-socket`·`workload-certs`·`istio-data`) | 76 | 4 KiB | ❌ **불가** | 파드 spec 이 아니라 **admission 시점에 istiod 가 주입**한다. kustomize 가 렌더하는 Deployment 에는 존재하지 않는다 → 앱 레포(istio sidecar injector 템플릿) 소관 |
| **차트/오퍼레이터 소유**(argocd `var-files`·`static-files` 209 MiB · rollouts `plugin-bin`/`vendored-plugin` 73 MiB · CNPG `scratch-data` 61 MiB · ECK · Grafana `storage` 51 MiB) | 약 30 | 209 MiB | ❌ 대부분 불가 | 차트가 만드는 파드 템플릿이다. Helm values 로 열려 있지 않으면 방법이 없다 |
| **config 레포 소유**(우리 매니페스트에 적힌 것) | **20** | **32 KiB** | ✅ **19개 가능** — 이 항목이 하는 일 | 나머지 1개 = `services/cloudflared` 의 `tmp`. **eks 오버레이 자체가 없다**(C-5 = cloudflared 는 온프렘 DR 전용) → 의도된 제외 |

🔴 **바이트는 우리 것이 아닌 쪽에 있고, 통제권은 우리 것에만 있다.** 그래서 이 항목의 실효는
"디스크 절감"이 아니라 **"우리 워크로드가 노드를 망가뜨리지 못하게 막는 것"** 이다.
209 MiB 짜리 argocd·rollouts 볼륨은 **여전히 무제한**이고, 그건 노드 EBS 사이징(C-16)과
kubelet eviction 임계값 쪽에서 흡수해야 한다(아래 §미결).

## 값과 근거

config 레포 소유 20개(그중 EKS 대상 **19개**)의 관측 최대는 **32 KiB**(pgsync `plugins-dir`)다. 아래 값은 전부
**관측치의 수천 배**이며, 동시에 **한 볼륨이 상한까지 차도 노드를 못 채우는** 크기다.

| 볼륨 | 트랙 | 관측 | `sizeLimit` | 근거 |
|---|---|---:|---:|---|
| `tmp` (API 서비스 11종) | account · chat · mealplan · notify · operations · pantry · price · recipe · recipebook · frontend · ranking-serving | 4 KiB | **64Mi** | `readOnlyRootFilesystem` 때문에 존재하는 순수 스크래치. 관측의 16,000배 |
| `tmp` (바이너리 페이로드 3종) | **ocr**(Deployment + canary CronJob) · **video** | 4 KiB | **512Mi** | 🔴 관측이 4 KiB 인 것은 **저부하 시점**일 뿐이다. ocr 은 영수증 이미지, video 는 YouTube 추출물을 다룬다 — 언젠가 /tmp 로 버퍼링하면 64Mi 는 **축출 사유**가 된다. 관측이 없는 경로에는 넓게 준다 |
| `models` | ranking-serving | — | **1Gi** | 🔴 온프렘은 이 볼륨을 **PVC(`mp-ranking-model` 1Gi)로 치환**한다(overlays/onprem). EKS 오버레이에는 그 치환이 없어 emptyDir 인 채다 → **온프렘 PVC 와 같은 크기**를 상한으로 잡았다. ⚠️ 아래 §미결 ① |
| `cache` | frontend | 24 KiB | **64Mi** | nginx proxy cache |
| `run` | frontend | 8 KiB | **8Mi** | pid·소켓만 |
| `checkpoint` | pgsync | 12 KiB | **64Mi** | 재시작 시 재동기화 체크포인트 |
| `plugins-dir` | pgsync | 32 KiB | **32Mi** | ConfigMap 복사본(`..data` 심링크 함정 회피용) |

**안전 검산** — 19개가 **동시에 상한까지** 차도 합계 ≈ **3.4 GiB**.
노드 allocatable ephemeral **43.7 GiB** 의 8% 다. 즉 이 상한들이 다 터져도 노드는 안 죽는다.
(`sizeLimit` 은 예약이 아니라 상한이라 평시 점유는 0 이다.)

## 🔴 미결 (사람 결정)

① **ranking-serving `models` 가 EKS 에서 emptyDir 인 것 자체** — 온프렘은 durability(B1)를 위해
   PVC 로 바꿨는데 EKS 오버레이에는 그 치환이 없다. 파드가 재시작할 때마다 모델을 다시 받아야 하고,
   `ranker.pkl` 은 백업 대상이기도 하다(체크리스트 0-8b 의 PVC 목록에 있다).
   **0-8(스토리지) 레인 소관**이지 이 항목이 정할 것이 아니다 — 여기서는 상한만 걸고 사실을 남긴다.
② **노드 루트 EBS 크기와 kubelet eviction 임계값**(C-16) — 위에서 봤듯 실제 바이트는 이 레포 밖
   (argocd 209 MiB · rollouts 73 MiB × 2 · CNPG 61 MiB × 5)에 있다. 노드당 emptyDir 합계는
   지금 최대 454 MiB 지만 **상한이 없으므로 보장이 아니다.** 노드 디스크를 정할 때
   `imagefs`·컨테이너 쓰기 레이어와 함께 계산할 것.
③ **Istio 주입 emptyDir 76개** — 값은 0 에 가깝지만 상한이 없다. 걸려면 앱 레포의
   sidecar injector 템플릿(`istio-sidecar-injector` ConfigMap)을 손봐야 한다. 이관과 무관한 별건.
④ **`platform/argocd` 의 `vendored-plugin`**(실측 73 MiB) — 상한을 걸 수 있는 자리가
   Helm Application 의 인라인 `valuesObject` 안 **리스트**라, JSON merge patch 가 통째로 교체한다
   (SITES.md §0-9 가 경고하는 그 함정 — `command`·`securityContext`·`resources`·`volumeMounts` 를
   전부 옮겨 적어야 한다). **위험 대비 이득이 낮아 이번 범위에서 뺐다.** 73 MiB 는 폭주가 아니라 상수다.

## 검증

```bash
# ① 온프렘 렌더 무변화 (이 항목의 안전기준)
kubectl kustomize <트랙>/overlays/onprem | sha256sum      # 작업 전후 동일

# ② eks 에 상한이 실제로 붙었나 — 19개가 나와야 한다 (cloudflared 는 eks 오버레이 없음)
for t in services/* platform/pgsync; do
  [ -d "$t/overlays/eks" ] && kubectl kustomize "$t/overlays/eks"
done | grep -c 'sizeLimit'
```
