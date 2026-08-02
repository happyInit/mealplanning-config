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

platform/                # 🔵 인프라 담당 소관 (2026-07-29 신설) — 앱 트랙과 뿌리가 다르다
  argocd/                #    플랫폼 child Application. `platform-root` 가 이 디렉토리를 집는다
                         #    오퍼레이터 5(automated) + 데이터 CR 5·pipelines(🔴 manual sync — 런북 Q8)
  pg/ pooler/ es/        #    데이터 CR 본문 (P2 — 정본 런북 = food-budget-app/docs/mp_k8s_p2_data_runbook.md)
  kafka/ pgsync/
  redis/                 #    🔴 의도적으로 비어 있음 — Q3 실물 검증 분기 대기 (README 참조)
  policies/              #    🔴 NetworkPolicy 연기 메모 — default-deny 베이스라인과 함께 별건

pipelines/               # 🔵 인프라 소관·project=mealplanning — 컨슈머 4 + CronJob 11 (dark-deploy)
```

- **뿌리가 둘이다** — `mealplanning-root`(앱, `argocd/applications/`) · `platform-root`(플랫폼,
  `platform/argocd/`). 서로 남의 디렉토리를 보지 않으므로 한쪽 실수가 다른 트랙으로 안 번진다.
  프로젝트도 분리(`mealplanning` / `platform`)라 배포 가능한 ns 도 다르다.
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

## 검증 — 푸시 전에 돌릴 것

```
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
| securityContext 베이스라인 | JSON-Patch `op: add` 가 merge 가 아니라 replace 라 하드닝이 렌더에서 증발한다 |

베이스라인(`scripts/policy-baseline.txt`)은 **알려진 위반을 얼려둔 것**이고 줄어들기만 해야 한다.
새 위반만 실패시킨다. 갱신은 `python3 scripts/validate.py --list`.

🔴 **자동 실행은 없다 — 지금은 푸시 전에 손으로 돌리는 것이 관문이다.**
이 조직의 CI 는 Jenkins(호스트 C), CD 는 ArgoCD 이고 이미 구성돼 있다. 이 레포의 검증을
그 파이프라인에 얹을지는 별건이며, 얹지 않아도 스크립트는 로컬에서 그대로 돈다.

`kustomize` 는 없으면 `kubectl kustomize` 로 대체되고, `kubeconform` 은 없으면 스키마 검증만
건너뛴다(경고). 즉 추가 설치 없이도 나머지 검사는 전부 동작한다.
