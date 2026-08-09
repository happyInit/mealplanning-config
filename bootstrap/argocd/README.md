# bootstrap/argocd — ArgoCD 자신을 세우는 오브젝트

> 신설 2026-08-09. 근거 = 앱 레포 `docs/mp_aws_prep_checklist.md` **0-4**.
> 구조 정본 = `SITES.md §0-4`.

## 🔴 이 디렉터리는 ArgoCD 가 읽지 않는다

두 뿌리(`mealplanning-root` = `argocd/applications` · `platform-root` = `platform/argocd`)의
감시 범위 **밖**이다. 일부러 그렇게 뒀다 — 여기 있는 것은 **뿌리 자신**이라, 뿌리가 읽으면
자기를 자기가 관리하는 순환이 된다(그 선택지의 위험은 `SITES.md §0-4` C안).

**적용자는 Ansible `k8s_argocd` 롤**(앱 레포)이다. 이 디렉터리는 그 롤이 만들어내야 할
**목표 상태의 기록**이며, 지금은 아직 정본이 아니다 —
정본을 Ansible 템플릿에 둘지 여기로 옮길지는 **미결(사람 결정)**.

## 무엇이 들어 있나

| 오브젝트 | 종류 | 지금 IaC 에 있나 |
|---|---|---|
| `mealplanning` | AppProject | ✅ Ansible `argocd-repo.yaml.j2` ③ — 단 **drift 있음**(아래) |
| `mealplanning-root` | AppProject | ✅ Ansible `argocd-repo.yaml.j2` ④ |
| `platform` | AppProject | ✅ Ansible `argocd-platform-project.yaml.j2` |
| `platform-root` | AppProject | ✅ Ansible `argocd-platform-root.yaml.j2` ① |
| `platform-root` | Application | ✅ Ansible `argocd-platform-root.yaml.j2` ② |
| **`mealplanning-root`** | **Application** | ❌ **어디에도 없다 — 손으로 apply 후 손으로 patch** |

즉 0-4 의 실제 공백은 **한 오브젝트 + drift 2건 + 사이트 파라미터 부재**다.
체크리스트의 *"AppProject 3·root Application 2 ·repo 자격증명이 레포에 없음"* 은
**과장이다**(실측 2026-08-09): AppProject 는 **4개**이고 **4개 다 Ansible 에 있다**.
repo SSH 자격증명도 IaC 안이다 — `fb-secrets` ns Secret → ESO → argocd ns
(`argocd-repo.yaml.j2` ①②, 키는 `secrets.yml` vault `argocd_repo_ssh_key`).

## 🔴 실측된 drift 2건 (라이브 ≠ IaC)

`kubectl.kubernetes.io/last-applied-configuration` 과 라이브 spec 을 비교해 찾았다.

1. **`mealplanning` AppProject 에 `mp-ingress` destination 이 라이브에만 있다.**
   Ansible `defaults/main.yml` 의 `argocd_allowed_namespaces` 는 `app`·`data`·`pipeline` 3개뿐.
   → 지금 `--tags argocd` 를 돌리면 **`mp-ingress` 가 지워지고 `mp-ingress` Application 이 배포 거부**된다.
   같은 사고가 descheduler 에서 이미 한 번 났다(`defaults/main.yml` 주석에 기록돼 있다).

2. **`mealplanning-root` Application 의 `project` 가 patch 로 바뀌어 있다.**
   last-applied = `mealplanning` / 라이브 = `mealplanning-root`.
   되돌릴 IaC 가 없어서 지금은 무해하지만, 누가 그 원본을 다시 apply 하면
   root 가 `InvalidSpecError` 로 죽는다(앱 child 23개 정지).

> ⚠️ `platform-root` Application 의 `directory.recurse: false` 가 라이브에서 안 보이는 것은
> drift 가 아니다 — false 는 제로값이라 API 서버가 직렬화에서 떨어뜨린 것이다.

## 검증 (이 디렉터리를 바꿀 때)

`overlays/onprem` 렌더가 **라이브와 전 필드 동일**해야 한다. 2026-08-09 기준 오브젝트 6 ·
필드 95 · 차이 0 으로 증명했다.

```bash
kubectl kustomize bootstrap/argocd/overlays/onprem > /tmp/render.yaml
ssh ubuntu@192.168.0.17 'sudo kubectl -n argocd get appproject mealplanning mealplanning-root platform platform-root -o json'
ssh ubuntu@192.168.0.17 'sudo kubectl -n argocd get application mealplanning-root platform-root -o json'
# → (apiVersion,kind,name) 로 정렬해 spec 전 필드 비교. 차이 0 이어야 한다.
```

🔴 `kubectl apply` 로 확인하지 말 것. 이 오브젝트들은 클러스터 전체의 배포 울타리라
잘못 적용하면 Application 44개가 한꺼번에 정지한다.
