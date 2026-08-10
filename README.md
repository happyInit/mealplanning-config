# mealplanning-config

**mealplanning 앱의 K8s 배포 매니페스트 (ArgoCD GitOps config 레포).**

이 레포의 ArgoCD 대상 경로는 *desired state*만 담는다 — 무엇이 클러스터에 떠 있어야 하는가.
ArgoCD가 그 경로를 watch해서 클러스터를 여기 맞춘다. **앱 소스는 여기 없다**(그건
`food-budget-app`). `ops/`만 예외로, Argo가 참조하지 않는 수동 migration bundle이다.

> 정본: [`food-budget-app/docs/mp_k8s_infra_migration_plan.md §7.3`](https://github.com/happyInit/food-budget-app/blob/main/docs/mp_k8s_infra_migration_plan.md) ·
> 오브젝트 설계 [`mp_k8s_infra_object_spec.md`](https://github.com/happyInit/food-budget-app/blob/main/docs/mp_k8s_infra_object_spec.md)

## 구조

🔴 **2026-08-09(AWS 이관 0-1)부터 ArgoCD 대상 트랙은 전부 같은 모양이다** — `base/` + `overlays/{onprem,eks}`.
자세한 규칙·경계는 **[`SITES.md`](SITES.md)**.

```
argocd/applications/     # 서비스별 child Application (app-of-apps). 서비스 추가 = 파일 하나 추가
                         # 🔴 여기와 platform/argocd/ 만 base/overlays 가 없다 — ArgoCD "뿌리"라
                         #    뿌리 Application 2개가 IaC 밖이다(체크리스트 0-4). 사이트 분기는 그 항목 소관.
<트랙>/
  base/                  # 매니페스트 본문 (사이트 공통)
  overlays/
    onprem/              # 온프렘 — ArgoCD Application 의 source.path 가 가리키는 곳
    eks/                 # EKS 골격 (🔴 Wave B 에서 채운다. 지금은 base 통과 = 온프렘 값 그대로)

services/<svc>/          # 앱 서비스 13 + cloudflared. overlays/onprem 의 images.newTag = Jenkins 가 커밋
common/ gateway/         # 공용 ConfigMap · HTTPRoute
gateway-internal/        # 내부 도구 6종 게이트웨이(.15) — 🔴 온프렘 색이 가장 짙은 트랙
ingress/                 # 공개 진입점(mp-ingress ns · MetalLB .14)
monitoring/              # PrometheusRule·ServiceMonitor·대시보드 (rules-physical = 온프렘 전용)
pipelines/               # 🔵 인프라 소관 — 컨슈머 + CronJob.  jobs/ 는 트랙 밖(1회성 kubectl)
platform/                # 🔵 인프라 담당 소관 — 앱 트랙과 뿌리가 다르다
  argocd/                #    플랫폼 child Application. `platform-root` 가 이 디렉터리를 집는다
  pg/ pooler/ es/        #    데이터 CR 본문 (P2 — 정본 런북 = food-budget-app/docs/mp_k8s_p2_data_runbook.md)
  kafka/ pgsync/ redis/
  rollouts/              #    Argo Rollouts ns 의 Harbor pull secret
  policies*/             #    NetworkPolicy (app · data · ingress · observability · pipeline)
ops/                     # 🔴 Argo 비대상 수동 migration/runbook — suspended template + gated runner
```

- **뿌리가 둘이다** — `mealplanning-root`(앱, `argocd/applications/`) · `platform-root`(플랫폼,
  `platform/argocd/`). 서로 남의 디렉토리를 보지 않으므로 한쪽 실수가 다른 트랙으로 안 번진다.
  프로젝트도 분리(`mealplanning` / `platform`)라 배포 가능한 ns 도 다르다.
- **AppProject·root Application·repo 자격증명은 여기 없다** — `food-budget-app`의 `k8s_argocd`
  Ansible 롤이 부트스트랩한다(master 전용). 여기는 root가 읽는 child app + 매니페스트만.
- **`ops/`를 Application source로 연결하지 않는다** — 파괴 가능한 일회성 절차는 permanent desired
  state가 아니다. 각 bundle의 runner가 confirmation/preflight/cleanup을 소유한다.

## 규칙 🔴

- **이미지 핀은 `:sha`** — `:latest` 금지(ArgoCD가 변경 감지 못 하고 롤백 대상 없음, 플랜 §7.4).
  `overlays/onprem/kustomization.yaml`의 `images.newTag`를 Jenkins가 `:sha`로 커밋한다.
- **비밀은 여기 두지 않는다** — 전부 ESO(`fb-secrets` → `ClusterSecretStore/fb-kubernetes`).
  이 레포에는 `ExternalSecret`(참조)만 있고 실제 값은 없다.
- **ns는 여기서 만들지 않는다** — `app` 등은 인프라 롤이 PSS 라벨과 함께 생성.

## 자동 sync (활성화 경계)

현재 child app은 **auto-sync OFF**(배선만). 최초 배포는 수동 sync로 검증하고, git push→자동
배포 활성화는 **P2**에 인프라 담당과 켠다(플랜 §7.4 "최초의 CD는 P2").

## 검증 — 푸시 전에 돌릴 것

```
python3 -m unittest discover -s tests -p 'test_*.py'
python3 scripts/validate.py
```

렌더 결과(= 실제로 클러스터에 가는 것)를 검사한다. "문법이 맞나"가 아니라 **"의도대로 렌더되나"**를
본다 — 이 레포에서 새어나간 사고는 전부 적용이 성공하고 에러도 없던 것들이었다:

| 검사 | 무엇을 막나 |
|---|---|
| `kustomize build` 전체 | 렌더 실패 |
| `kubeconform` | 스키마 위반. 🔴 ArgoCD 의 apply 는 strict 가 아니라 **모르는 필드를 조용히 프루닝**한다 |
| 4-dot FQDN 금지 | `<svc>.<ns>.svc.cluster.local` 이 파드 search 의 `local` 때문에 ISP 로 새어 공인 IP 로 해석된다(실측 21.7%) |
| `:latest` 금지 | ArgoCD 가 변경을 감지 못 하고 롤백 대상이 없어진다 |
| `topologyKey` 중복 금지 | patchMergeKey 충돌로 제약 하나가 조용히 사라진다 |
| CNPG failsafe 정책 | instance 간 `5432/8000` 비대칭과 K8s/Cilium additive `8000` 우회를 막는다 |
| securityContext 베이스라인 | JSON-Patch `op: add` 가 merge 가 아니라 replace 라 하드닝이 렌더에서 증발한다 |

베이스라인(`scripts/policy-baseline.txt`)은 **알려진 위반을 얼려둔 것**이고 줄어들기만 해야 한다.
새 위반만 실패시킨다. 갱신은 `python3 scripts/validate.py --list`.

🔴 **자동 실행은 없다 — 지금은 푸시 전에 손으로 돌리는 것이 관문이다.**
이 조직의 CI 는 Jenkins(호스트 C), CD 는 ArgoCD 이고 이미 구성돼 있다. 이 레포의 검증을
그 파이프라인에 얹을지는 별건이며, 얹지 않아도 스크립트는 로컬에서 그대로 돈다.

`kustomize` 는 없으면 `kubectl kustomize` 로 대체되고, `kubeconform` 은 없으면 스키마 검증만
건너뛴다(경고). 즉 추가 설치 없이도 나머지 검사는 전부 동작한다.
