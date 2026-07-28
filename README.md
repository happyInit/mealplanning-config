# mealplanning-config

**mealplanning 앱의 K8s 배포 매니페스트 (ArgoCD GitOps config 레포).**

이 레포는 *desired state* 하나만 담는다 — 무엇이 클러스터에 떠 있어야 하는가. ArgoCD가
이 레포를 watch해서 클러스터를 여기 맞춘다. **앱 소스는 여기 없다**(그건 `food-budget-app`).

> 정본: [`food-budget-app/docs/mp_k8s_infra_migration_plan.md §7.3`](https://github.com/happyInit/food-budget-app/blob/main/docs/mp_k8s_infra_migration_plan.md) ·
> 오브젝트 설계 [`mp_k8s_infra_object_spec.md`](https://github.com/happyInit/food-budget-app/blob/main/docs/mp_k8s_infra_object_spec.md)

## 구조

```
argocd/applications/     # 서비스별 child Application (app-of-apps). 서비스 추가 = 파일 하나 추가
services/<svc>/
  base/                  # Deployment·Service·HTTPRoute·NetworkPolicy·ExternalSecret (환경 공통)
  overlays/
    onprem/              # 온프렘 — 이미지 :sha 핀(Jenkins가 커밋), openebs-lvm SC
    eks/                 # EKS 이식 오버레이 (플랜 §8) — ECR·gp3 등 다른 것만
```

- **AppProject·root Application·repo 자격증명은 여기 없다** — `food-budget-app`의 `k8s_argocd`
  Ansible 롤이 부트스트랩한다(master 전용). 여기는 root가 읽는 child app + 매니페스트만.

## 규칙 🔴

- **이미지 핀은 `:sha`** — `:latest` 금지(ArgoCD가 변경 감지 못 하고 롤백 대상 없음, 플랜 §7.4).
  `overlays/onprem/kustomization.yaml`의 `images.newTag`를 Jenkins가 `:sha`로 커밋한다.
- **비밀은 여기 두지 않는다** — 전부 ESO(`fb-secrets` → `ClusterSecretStore/fb-kubernetes`).
  이 레포에는 `ExternalSecret`(참조)만 있고 실제 값은 없다.
- **ns는 여기서 만들지 않는다** — `app` 등은 인프라 롤이 PSS 라벨과 함께 생성.

## 자동 sync (활성화 경계)

현재 child app은 **auto-sync OFF**(배선만). 최초 배포는 수동 sync로 검증하고, git push→자동
배포 활성화는 **P2**에 인프라 담당과 켠다(플랜 §7.4 "최초의 CD는 P2").
