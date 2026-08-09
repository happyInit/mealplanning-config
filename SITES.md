# SITES.md — 사이트 분기(온프렘 / EKS) 구조

> 신설 2026-08-09. 근거 = 앱 레포 `docs/mp_aws_prep_checklist.md` **0-1**.
> 이 문서는 *구조*의 정본이다. 이관 결정(C-1~C-29)의 정본은 그 체크리스트다.

## 왜 "지우기"가 아니라 "가르기"인가

**C-3 — 온프렘은 이관 후에도 살아 있다.** ① DR 대기 사이트(Warm Standby) ② 크롤 상시 프로덕션,
이중 역할이다. 그래서 온프렘 전용 설정(nodeSelector · hostname TSC · MetalLB IP · Harbor LAN IP ·
MinIO 엔드포인트 · 물리계층 알림)은 **지우면 안 되고 `overlays/onprem` 으로 내려야** 한다.

0-1 이 만든 것은 **그 자리**다. 값을 옮기는 것도, eks 값을 채우는 것도 **Wave B 의 일**이다.

## 구조

```
<트랙>/
  base/                    매니페스트 본문 + kustomization.yaml  (사이트 공통)
  overlays/
    onprem/                ArgoCD Application 의 source.path 가 가리키는 곳
    eks/                   EKS 골격 — 🔴 지금은 base 통과라 값이 온프렘 그대로다
```

- `overlays/onprem` 은 **2026-08-09 시점에 전 트랙이 순수 통과**다(`resources: [../../base]`).
  그게 이 작업의 안전 기준이었다 — 재구성 전후 렌더가 같아야 한다.
- `overlays/eks` 는 `kustomization.yaml` 머리말에 **그 트랙에서 갈라야 하는 것**을
  체크리스트 항목 번호와 함께 적어 뒀다. 거기서부터 시작하면 된다.

### 트랙 목록 (32)

| 부류 | 개수 | 트랙 |
|---|---|---|
| 앱 서비스 | 14 | `services/*` (13 + `cloudflared`) — 2026-07 부터 이미 이 모양이었다 |
| 공용·진입점 | 4 | `common` `gateway` `gateway-internal` `ingress` |
| 관측·파이프라인 | 2 | `monitoring` `pipelines` |
| 데이터 CR | 6 | `platform/{pg,pooler,es,kafka,redis,pgsync}` |
| 정책 | 5 | `platform/policies{,-data,-ingress,-observability,-pipeline}` |
| 기타 | 1 | `platform/rollouts` |

**분기 수단이 없던 18개**(= 위에서 `services/*` 를 뺀 전부)에 0-1 이 골격을 넣었다.

> ℹ️ `bootstrap/argocd` 는 이 32개에 **들어가지 않는다** — 워크로드 트랙이 아니라
> ArgoCD 자신을 세우는 부트스트랩이고, 두 뿌리의 감시 범위 밖이다(§0-4).
> 다만 kustomize 디렉터리이긴 해서 `validate.py` 집계에는 잡힌다(32 → 33).

## 🔴 경계 — 여기 없는 것 둘

### ① ArgoCD 뿌리 2개 (`argocd/applications/` · `platform/argocd/`)

**의도적으로 재구성하지 않았다.** 이 두 디렉터리를 보는 root Application
(`mealplanning-root` · `platform-root`)이 **git 밖**에 있기 때문이다(체크리스트 **0-4**).
경로를 바꿔도 config 레포 커밋으로 root 의 `source.path` 를 따라 바꿀 수단이 없어,
머지하는 순간 두 뿌리가 빈 디렉터리를 보게 된다.

→ **사이트 분기는 0-4 와 함께** 한다. 설계 후보 둘:

| 안 | 방법 | 장점 | 단점 |
|---|---|---|---|
| **A. 사이트별 뿌리 디렉터리** | `argocd/applications/`(온프렘) 옆에 eks 용 디렉터리를 하나 더 두고, 각 사이트의 root Application 이 자기 것을 본다 | 단순·명시적. 사이트마다 **앱 목록 자체가 다른** 현실(아래 ②)과 맞는다 | Application 44개가 사실상 두 벌이 된다 |
| **B. ApplicationSet(git 디렉터리 제너레이터)** | `path: {{path}}/overlays/<site>` 로 템플릿 | DRY. 트랙 추가가 자동 | 라이브 Application 을 ApplicationSet 소유로 넘기는 컷오버가 필요하고, 앱마다 다른 sync 정책(**실측 2026-08-09: automated 30 / manual 16**)을 표현하기 번거롭다 |

**→ 판정 완료: A. 근거·실증·컷오버 절차는 아래 §0-4.**
🔴 다만 A 의 단점으로 적힌 *"Application 44개가 두 벌이 된다"* 는 **틀렸다** — 그건 복사를
전제했을 때 얘기고, kustomize 오버레이로 하면 **차이나는 필드만** 패치로 적는다(§0-4 실증).

### ② Helm 차트 Application 12개

`alloy` `loki` `tempo` `kubecost` `keda` `descheduler` `rollouts` + 오퍼레이터 5종은
**source 가 이 레포가 아니라 Helm 리포지터리**다. 사이트별 차이는 매니페스트가 아니라
`platform/argocd/<이름>.yaml` 안의 **인라인 `valuesObject`** 에 있다 —
예: `loki` 의 `nodeSelector: kubernetes.io/hostname: k8s-worker-b1`,
`tempo`·`kubecost` 의 `storageClass: openebs-lvm`, `kubecost` 의 `kubernetes.io/hostname: k8s-worker-a2`.

그 파일들은 위 ①(뿌리 디렉터리) 안에 있으므로 **분기 수단도 ① 과 같다.**
즉 0-4 를 풀지 않으면 이 12개는 분기할 방법이 없다. 0-5(nodeSelector)·0-8(SC)의 실작업 상당수가
여기에 걸려 있다.

## 실무 규칙

1. **매니페스트를 새로 추가하면 `base/kustomization.yaml` 의 `resources` 에 등록한다.**
   `platform/{es,kafka,pg,pgsync,pooler,redis,rollouts}` 7종은 2026-08-09 전까지 kustomization
   없는 "디렉터리형"이라 **파일만 두면 배포됐다.** 이제는 등록하지 않으면 조용히 배포되지 않는다.
2. **`overlays/onprem` 에 값을 얹을 때는 base 에서 빼는 것과 한 커밋으로.** 양쪽에 남으면
   "온프렘 전용을 골라냈다"는 착시만 생기고 EKS 로 그대로 샌다.
3. **`base/kustomization.yaml` 에 `namespace:` 를 새로 넣지 말 것.** 데이터 CR 트랙은 매니페스트가
   `metadata.namespace` 를 직접 갖고 있고, `platform/rollouts` 는 **일부러 갖고 있지 않다**
   (Application 의 destination `argo-rollouts` 가 채운다). 여기서 강제하면 렌더가 갈린다.
4. **`overlays/eks` 는 `validate.py` 의 정책 검사 대상이 아니다**(`SKIP_KUSTOMIZE_RE`).
   렌더 성공 여부만 `check_site_overlays()` 가 본다. **초록 = 이관 준비 완료가 아니다.**
5. **eks 오버레이가 없는 트랙은 경고로 뜬다.** 지금은 `services/cloudflared` 하나 —
   C-5(cloudflared = 온프렘 DR 전용 존치)라 의도된 부재다. 새로 뜨면 0-1 누락이다.

## 🔴 이 브랜치를 머지할 때 (0-1 반영)

18개 Application 의 `source.path` 가 `<트랙>` → `<트랙>/overlays/onprem` 으로 바뀐다.
**렌더 결과가 같으므로 리소스 변화는 0 이어야 한다.** 다만 반영 시점이 갈린다 (실측 2026-08-09):

- **auto-sync 2개** — `pipelines` · `rollouts-pullsecret` → 머지 즉시 재렌더된다.
  🔴 `rollouts-pullsecret` 은 `prune: true` 다. 렌더가 같으므로 프룬할 대상이 없지만,
  머지 직후 `kubectl -n argo-rollouts get externalsecret mp-harbor-pull` 로 한 번 확인할 것.
- **manual sync 16개** — `app-common` `gateway` `gateway-internal` `mp-ingress` `monitoring`
  `mp-policies{,-data,-ingress,-observability,-pipeline}` `pg` `pooler` `es` `kafka` `redis` `pgsync`
  → 머지만으로는 아무 일도 일어나지 않는다. 확인은 아래 한 줄이면 된다.

```bash
kubectl -n argocd get applications -o custom-columns=NAME:.metadata.name,PATH:.spec.source.path,SYNC:.status.sync.status
```

경로가 바뀐 뒤에도 `Synced` 면 성공이다 — 같은 것을 다른 자리에서 읽고 있다는 뜻이다.

## 검증 방법 (이 구조를 바꿀 때)

재구성이 라이브를 건드리지 않았음을 증명한 방법 그대로 쓰면 된다.

```bash
# 1) 바꾸기 전 렌더를 트랙마다 저장
for t in <트랙들>; do kubectl kustomize $t/overlays/onprem > before/$t.yaml; done
# 2) 바꾼 뒤 같은 것을 저장하고 diff — 온프렘은 0 이어야 한다
diff -r before after
```

디렉터리형 → kustomize 변환처럼 **바이트 비교가 성립하지 않는** 경우에는
문서를 `(apiVersion, kind, namespace, name)` 로 정렬하고 키 정렬 JSON 으로 덤프해 비교한다
(= 오브젝트 집합과 전 필드 값의 동일성). 0-1 은 두 방법을 다 썼다:

- 기존 kustomize 트랙 11종 → **바이트 단위 동일**(sha256 일치)
- 디렉터리형 7종 + 서비스 14종 → **정규화 후 전 필드 동일**(문서 232개, 차이 0)

🔴 **의도한 예외 2건** — 알림 `annotations.description` 안에 **런북 경로가 문자열로 박혀 있어서**
파일이 옮겨간 만큼 같이 고쳤다. 렌더 diff 가 0 이 아닌 곳은 여기뿐이다:

| 오브젝트 | 필드 | 바뀐 것 |
|---|---|---|
| `PrometheusRule/mp-workload-spread` | `groups[0].rules[6].annotations.description` | `monitoring/rules.yaml` → `monitoring/base/rules.yaml` |
| `PrometheusRule/mp-pipeline` | `groups[0].rules[1].annotations.description` | `pipelines/kustomization.yaml` → `pipelines/base/kustomization.yaml` |

알림의 신원은 `alertname` + 라벨이고 `annotations` 는 그룹핑·억제·중복제거에 참여하지 않는다 —
**울리는 조건도, 라우팅도 바뀌지 않는다.** 반대로 안 고치면 온콜이 런북을 따라가다 없는 파일을 만난다.

---

# §0-4 — ArgoCD 뿌리 IaC 화 · 사이트 분기 설계

> 신설 2026-08-09. 실측·실증 기반. 관련 산출물 = `bootstrap/argocd/`.

## 용어 (먼저 읽을 것)

| 말 | 뜻 |
|---|---|
| **root / app-of-apps** | 다른 Application 을 만드는 Application. 우리는 2개 — `mealplanning-root`(앱 23) · `platform-root`(플랫폼 21) |
| **child** | root 가 만든 Application. git 의 yaml 파일 하나 = child 하나 |
| **directory 모드** | ArgoCD 가 디렉터리의 `*.yaml` 을 **그대로** 매니페스트로 적용. 지금 두 뿌리가 이 모드다 |
| **kustomize 모드** | 디렉터리에 `kustomization.yaml` 이 있으면 kustomize 로 렌더해서 적용 |
| **오버레이(overlay)** | base 를 읽어 **차이나는 필드만** 패치로 얹는 kustomize 디렉터리. 0-1 이 32개 트랙에 깐 구조 |
| **`valuesObject`** | Helm 차트를 소스로 쓰는 Application 이 차트에 넘길 values 를 **Application yaml 안에 인라인**으로 적은 것 |
| **ApplicationSet** | Application 을 찍어내는 컨트롤러. 제너레이터(git 디렉터리·파일·리스트…)가 목록을 만들고 템플릿이 Application 을 생성 |
| **부트스트랩** | git 을 읽는 주체(=ArgoCD 와 그 뿌리) 자신을 **git 밖에서** 처음 한 번 세우는 일 |

## 지금 모양 (실측 2026-08-09)

```
                        Ansible  k8s_argocd 롤  (앱 레포 · git)
                                 │  kubectl apply
        ┌────────────────────────┼─────────────────────────┐
        ▼                        ▼                         ▼
  AppProject 4종           repo 자격증명            root Application 2
  mealplanning ⚠drift      fb-secrets Secret        platform-root      ✅IaC
  mealplanning-root        └ESO→ argocd ns          mealplanning-root  ❌IaC 밖
  platform                                             (손 apply + 손 patch)
  platform-root
                                 │
     ┌───────────────────────────┴────────────────────────────┐
     ▼                                                        ▼
 mealplanning-root                                       platform-root
 path: argocd/applications                               path: platform/argocd
 directory.recurse=true                                  directory.include=*.yaml
 automated{prune:TRUE, selfHeal}                         automated{prune:false, selfHeal}
 finalizer 없음                                           finalizer 있음
     │                                                        │
     ├─ child 23 (git 소스, 전부 services/*·트랙)              ├─ child 21
     │    → 0-1 이 이미 base/overlays 로 갈라 둠                │    ├─ git 소스 9  → 0-1 이 갈라 둠
     │                                                        │    └─ Helm 소스 12 → 🔴 분기 수단 없음
     ▼                                                        ▼
   전체 Application 46 = child 44 + root 2  ·  auto-sync 30 / manual 16
```

**막힌 지점 둘**
- ① root 2개가 git 밖 → `source.path` 를 config 커밋으로 못 바꾼다.
- ② Helm 소스 child 12개는 온프렘 값이 `platform/argocd/<이름>.yaml` 의 인라인
  `valuesObject` 에 있다 → 오버레이가 없으면 사이트별로 못 가른다.

## 선택지

### A. 사이트별 뿌리 디렉터리 (kustomize 오버레이)

뿌리가 보는 디렉터리 자체를 `base/ + overlays/{onprem,eks}` 로 만들고,
**각 사이트의 root Application 이 자기 overlay 를 본다.**

```
argocd/          base/ (child 23)  overlays/onprem/  overlays/eks/
platform/argocd/ base/ (child 21)  overlays/onprem/  overlays/eks/
                                          ▲                ▲
              온프렘 root ────────────────┘                │
              EKS   root ─────────────────────────────────┘
```

- **①** root 의 `path` 는 여전히 부트스트랩이 정한다. 다만 값이 **사이트당 한 줄**로 줄어
  Ansible 변수(`argocd_site`) 하나로 표현된다.
- **②** ✅ 푼다. `valuesObject` 는 kustomize 패치로 **필드 단위** 수정이 된다 — **실증했다**:

  | 대상 | onprem 렌더 | eks 패치가 바꾼 필드 |
  |---|---|---|
  | `loki` (valuesObject 128줄) | 원본과 전 필드 동일 | `singleBinary.nodeSelector` 삭제 · `persistence.storageClass` → `gp3` |
  | `tempo` | 동일 | `nodeSelector` 삭제 · `persistence.storageClassName` → `gp3` |
  | `kubecost` (패치 없음) | 동일 | 0 |

  JSON merge patch 의 `null` 이 키 삭제로 동작해 **nodeSelector 를 통째로 걷어낼 수 있다.**
  패치하지 않은 필드는 128줄짜리 values 안에서도 한 글자도 안 바뀐다.

### B. ApplicationSet

제너레이터가 트랙 목록을 만들고 템플릿이 Application 을 찍는다.

- **①** ❌ **못 푼다.** ApplicationSet 오브젝트 자체를 누군가 클러스터에 넣어야 한다 —
  부트스트랩 문제가 사라지는 게 아니라 **이름만 바뀐다**(root Application → ApplicationSet).
- **②** ❌ 사실상 못 푼다. git 디렉터리 제너레이터는 `path` 만 바꿔 끼운다. Helm 12개의
  차이는 path 가 아니라 **인라인 values 본문**이라 템플릿 변수로 안 나온다.
  풀려면 values 를 별도 파일로 빼는 multi-source 재작성이 선행돼야 하는데,
  **그 재작성을 하고 나면 A 로도 똑같이 풀린다** — 즉 B 의 고유 이득이 아니다.
- 추가 부담: sync 정책이 앱마다 다르다(**automated 30 / manual 16** — 실측). 템플릿 하나로
  못 덮어 리스트 제너레이터에 44항목을 적게 되고, 그러면 DRY 이득이 사라진다.
- 컨트롤러는 이미 떠 있다(`argocd-applicationset-controller` 1/1, CRD 존재, 현재 AppSet 0개).
  **막는 것은 기술이 아니라 적합성이다.**

### C. 뿌리 자기관리 (self-managed root) — 참고

root Application 을 자기가 감시하는 디렉터리 안에 두면, 부트스트랩 이후엔 `path` 를
config 커밋으로 바꿀 수 있다. **①을 진짜로 없애는 유일한 안**이다.
🔴 대가가 크다 — `mealplanning-root` 는 `prune: true` 라 root 파일을 잘못 건드리면
자기 자신을 프룬한다. `platform-root` 는 finalizer 가 있어 **삭제가 child 21개 →
PG·ES·Kafka CR 까지 연쇄**한다. 이관 준비 기간에 감당할 위험이 아니다.

## 비용

| 항목 | A | B |
|---|---|---|
| config 레포 작업량 | 파일 이동 44 + 오버레이 4 + 패치(차이나는 필드만 — 현재 **15**) | 전 트랙 Application 정의 재작성 |
| Ansible 작업량 | 템플릿 1개 신설 + 변수 1개 + drift 2건 수정 | 같음(부트스트랩은 그대로) |
| 라이브 중단 | **없음**(아래 절차 준수 시) | 라이브 44개를 AppSet 소유로 넘기는 컷오버 필요 |
| 러닝코스트 | kustomize — 이미 전 트랙이 쓰는 도구 | 새 개념 1개 추가 |
| **미검증** | ArgoCD 가 `spec.source.directory` 가 명시된 상태에서 `kustomization.yaml` 을 만나면 어느 모드로 가는가 → **회피 설계로 무해화**(아래) | AppSet 인수인계 무중단 여부 |

## 권고 — **A**

② 를 실제로 푸는 것이 A 뿐이고, ① 은 어느 안을 골라도 부트스트랩이 남는다.
"둘 다 푸는가"로 갈랐을 때 A 만 남는다.

**포기하는 것**
- **DRY 의 일부** — 트랙을 새로 만들 때 오버레이 2개를 손으로 만들어야 한다(B 였다면 자동).
  → 대신 `validate.py check_site_overlays()` 가 빠진 트랙을 경고로 잡는다(이미 동작 중).
- **①의 근본 해결** — 뿌리는 계속 Ansible 이 세운다. C안만이 이걸 없애는데 대가가 너무 크다.
  즉 0-4 의 목표는 *"뿌리를 git 안으로"* 가 아니라 **"뿌리를 재현 가능하게 + 사이트를 변수 하나로"** 로 다시 잡는다.
- **ApplicationSet** — 지금 도입하지 않는다. 트랙이 수십 개 더 늘거나 사이트가 3개 이상이 되면 재검토.

## 🔴 컷오버 절차 — 한 PR 로 끝나지 않는다

**실측된 제약**: kustomize 는 기본 load restrictor 에서 **자기 루트 밖 파일을 `resources` 로 못 읽는다**
(`security; file ... is not in or below ...` 실측). 즉 *"파일은 그대로 두고 오버레이만 얹기"* 가 **불가능**하다 —
base 로 **실제 이동**이 필요하다. 그런데 이동하는 순간 라이브 뿌리가 보던 디렉터리가 비고,
`mealplanning-root` 는 **`prune: true` + auto-sync** 라 **child 23개가 통째로 프룬된다.**

→ **3단계로 나눈다. 각 단계가 단독으로 안전해야 한다.**

1. **(config PR) 복사** — `argocd/overlays/{onprem,eks}` · `platform/argocd/overlays/{onprem,eks}` 를
   **새로 만든다. 기존 `argocd/applications/*.yaml`·`platform/argocd/*.yaml` 은 건드리지 않는다.**
   이 시점엔 같은 Application 정의가 git 에 두 벌 있지만 **뿌리는 옛 경로만 읽으므로 새 쪽은 무생물**이다.
2. **(앱 레포 PR + 적용) 뿌리 재지정** — Ansible `k8s_argocd` 가 `mealplanning-root` Application 을
   **처음으로** 관리하게 하고, 두 뿌리의 `path` 를 `…/overlays/{{ argocd_site }}` 로 바꾼다.
   적용 후 확인:
   ```bash
   kubectl -n argocd get applications -o custom-columns=NAME:.metadata.name,PATH:.spec.source.path,SYNC:.status.sync.status
   ```
   **child 44개가 그대로 있고 전부 Synced** 여야 한다. 하나라도 사라지면 즉시 되돌린다.
3. **(config PR) 옛 경로 삭제** — 2 가 안정된 것을 확인한 뒤에만.

🔴 1↔3 사이에 **정본이 두 곳**이다. 그 창에서 child 를 수정하면 **양쪽 다** 고쳐야 한다.
창을 짧게 가져가는 것이 이 절차의 유일한 비용이다.

🔴 **`kustomization.yaml` 을 지금 감시 중인 디렉터리(`argocd/applications/`·`platform/argocd/`)에
넣지 말 것.** ArgoCD 가 directory 모드를 유지하면 그 파일을 매니페스트로 취급해 sync 가 깨지고,
kustomize 모드로 전환되면 `resources` 에 없는 child 가 프룬된다. **어느 쪽인지 확인하지 않았고,
위 절차는 그 질문 자체를 회피한다** — 새 디렉터리에만 kustomization 을 둔다.

## 이 구조가 열어주는 것 (Wave B)

Helm 12개의 인라인 `valuesObject` 에 갇혀 있던 **사이트 결합 값 15건**이 패치 가능해진다.

| 값 | 건수 | 어디 |
|---|---|---|
| **0-5** nodeSelector (`kubernetes.io/hostname`) | 5 | `loki` 1 · `kubecost` 4 |
| **0-5** nodeSelector (`topology.kubernetes.io/zone: host-b`) | 1 | `tempo` 1 |
| **0-8** storageClass (`openebs-lvm`) | 6 | `loki` 1 · `tempo` 1 · `kubecost` 4 |
| **0-9** Harbor LAN IP (`192.168.0.10/…`) | 1 | `rollouts` initContainer 이미지 |
| MinIO 인클러스터 엔드포인트 | 2 | `loki` · `tempo` (S3 전환 대상) |

나머지 8개(`alloy` `keda` `descheduler` + 오퍼레이터 5)는 **사이트 결합 값이 0** 이라
분기 자체가 필요 없다 — eks 오버레이에서 패치할 것이 없다.
