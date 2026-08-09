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

**아직 정하지 않았다.** 0-4 에서 결정할 것.

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
