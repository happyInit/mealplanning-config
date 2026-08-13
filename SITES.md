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

- `overlays/onprem` 은 **2026-08-09 시점에 전 트랙이 순수 통과**였다(`resources: [../../base]`).
  그게 이 작업의 안전 기준이었다 — 재구성 전후 렌더가 같아야 한다.
  🔴 **2026-08-10 부터 예외 1건** — `platform/es/overlays/onprem` 이 nodeSelector 를 다시 얹는다
  (0-5·0-7 로 base 에서 걷어낸 온프렘 zone 핀). **안전 기준은 그대로다**: 이사 전후로
  onprem 렌더 33개가 **바이트 단위 동일**함을 확인했다. 순수 통과가 깨진 것이 아니라,
  "온프렘 값은 onprem 오버레이가 갖는다"는 이 문서의 규칙이 처음 실제로 쓰인 것이다.
- `overlays/eks` 는 `kustomization.yaml` 머리말에 **그 트랙에서 갈라야 하는 것**을
  체크리스트 항목 번호와 함께 적어 뒀다. 거기서부터 시작하면 된다.

### 트랙 목록 (34)

| 부류 | 개수 | 트랙 |
|---|---|---|
| 앱 서비스 | 14 | `services/*` (13 + `cloudflared`) — 2026-07 부터 이미 이 모양이었다 |
| 공용·진입점 | 4 | `common` `gateway` `gateway-internal` `ingress` |
| 관측·파이프라인 | 2 | `monitoring` `pipelines` |
| 데이터 CR | 6 | `platform/{pg,pooler,es,kafka,redis,pgsync}` |
| 정책 | 5 | `platform/policies{,-data,-ingress,-observability,-pipeline}` |
| 기타 | 3 | `platform/rollouts` · `platform/observability`(0-2 C-18 선행) · **`platform/cluster-baseline`**(0-3, 2026-08-13 — 아래 §0-3) |

**분기 수단이 없던 18개**(= 위에서 `services/*` 를 뺀 전부)에 0-1 이 골격을 넣었다.

> ℹ️ **위 34 는 "워크로드 트랙" 만이다.** `base/` 를 가진 디렉터리는 실제로 **37개**이고, 차이 3은:
> `bootstrap/argocd`(ArgoCD 자신을 세우는 부트스트랩 — 두 뿌리의 감시 범위 밖, §0-4) +
> **뿌리 트랙 2개**(`argocd` · `platform/argocd` — §0-4 컷오버로 생겼다. 이들은 *다른 트랙을 배포하는*
> Application 목록이라 워크로드가 아니다).
> 셋 다 kustomize 디렉터리이긴 해서 `validate.py` 집계에는 잡힌다(**34 → 37**).

## 경계 — 여기 없던 것 둘 (✅ 둘 다 해소, 2026-08-10)

### ① ArgoCD 뿌리 2개 — ✅ **해소됨**

> **지금 모양** = `argocd/{base,overlays/{onprem,eks}}` · `platform/argocd/{base,overlays/{onprem,eks}}`.
> 두 뿌리가 `…/overlays/{{ argocd_site }}` 를 본다. **전 트랙이 예외 없이 같은 모양이 됐다.**
> 아래는 왜 한동안 예외였는지의 기록이다.

0-1 시점에는 **의도적으로 재구성하지 않았다.** 이 두 디렉터리를 보는 root Application
(`mealplanning-root` · `platform-root`)이 **git 밖**에 있었기 때문이다(체크리스트 **0-4**).
경로를 바꿔도 config 레포 커밋으로 root 의 `source.path` 를 따라 바꿀 수단이 없어,
머지하는 순간 두 뿌리가 빈 디렉터리를 보게 됐다.
→ 0-4 가 3단계 컷오버로 풀었다(아래 §0-4 "컷오버 절차"). 앱 레포 Ansible 이 두 뿌리를
관리하게 되면서 `path` 가 변수 하나로 표현된다.

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
   ⚠️ **예외 둘** — 이 두 축만은 eks 렌더의 **내용**을 보고, 초록이 실제 보증이 된다.
   ① `check_eks_secret_store()` — ESO 백엔드(아래 6)
   ② `check_registry_split()` — 레지스트리(0-9, 2026-08-10). eks 렌더의 `mp-*` 이미지를
      전수 보고 Harbor LAN IP 가 남아 있으면 **실패**시킨다(아래 7).
5. **eks 오버레이가 없는 트랙은 경고로 뜬다.** 지금은 `services/cloudflared` 하나 —
   C-5(cloudflared = 온프렘 DR 전용 존치)라 의도된 부재다. 새로 뜨면 0-1 누락이다.
6. 🔴 **`ExternalSecret` 을 base 에 새로 추가하면 그 트랙 eks 오버레이에도 `secretStoreRef` 패치를 더한다.**
   안 하면 온프렘은 멀쩡히 돌아서 아무도 모르고, AWS 에서만 NotReady 가 된다.
   `validate.py check_eks_secret_store()` 가 실패로 잡는다(아래 §ESO).
7. **이미지 레지스트리는 `scripts/sites.yaml` 이 정본이다.** 값 자체는 각 오버레이의
   kustomize `images` 트랜스포머에 있지만(= CD 가 `newTag` 를 쓰는 자리라 거기 있어야 한다),
   **불일치는 sites.yaml 기준으로 기계가 잡는다.** 계정 ID 가 정해지면 sites.yaml 한 줄을
   고치고 `validate.py` 가 열거해 주는 오버레이 목록대로 맞춘다 — 손으로 세지 않는다.
   🔴 Elasticsearch CR 의 `spec.image` 처럼 **CRD 필드는 `images` 트랜스포머가 못 건드린다**
   (기본 fieldSpec 이 컨테이너 경로뿐이다). 그런 자리는 patch 로 직접 간다.

## §0-2 — ESO 비밀 백엔드의 사이트 분기

> 신설 2026-08-10. 근거 = 체크리스트 **0-2**·**0-16**, 결정 **C-23**(비밀 = 양 사이트 독립).
> 스토어 본문·값 적재·IAM 최소권한·미결 = **`bootstrap/eso/README.md`**.

**온프렘 = `fb-kubernetes`(K8s provider) 유지 / EKS = `mp-aws-ssm`(SSM ParameterStore + IRSA).**

### 갈리는 것은 한 필드다

`ExternalSecret` 에서 사이트마다 다른 값은 **`secretStoreRef.name` 하나뿐**이고,
`remoteRef` **67엔트리는 한 글자도 안 바뀐다.** 성립 근거 둘:

| 전제 | 왜 |
|---|---|
| 스토어의 `spec.provider.aws.prefix: /mp/prod/` | key 앞에 그대로 이어 붙어 파라미터 이름이 된다(`app-secrets` → `/mp/prod/app-secrets`). 🔴 **끝 슬래시가 load-bearing** — 없으면 `/mp/prodapp-secrets` 가 된다 |
| `property` 이름에 gjson 메타문자 0건 | AWS provider 는 `property` 를 JSON 값 안의 gjson 경로로 읽는다. 67/67 실측 — **새 키 이름에 `.` 을 넣지 말 것** |

이걸 안 지키면 구현자가 67엔트리에 경로를 손으로 붙이기 시작하고, 그 순간 두 사이트의
매니페스트가 갈려 base 공유가 무너진다. 그래서 **`prefix` 는 편의가 아니라 구조적 전제다.**

### 스토어 자체는 왜 `bootstrap/eso/` 에 있나

`ClusterSecretStore` 는 클러스터 스코프이고 **ExternalSecret 이 생기기 전에 이미 있어야** 하는
부트스트랩 오브젝트다. 워크로드 트랙과 같은 수명주기에 태우면 닭-달걀이 된다.
`bootstrap/argocd` 와 같은 성격 — **ArgoCD 두 뿌리의 감시 범위 밖**이고, 적용자는 Ansible
`k8s_eso` 롤(앱 레포)이며, 여기 있는 것은 **목표 상태의 기록**이다.
🔴 `base/` 가 없다 — 두 스토어는 `provider` 아래가 통째로 갈려 공유 필드가 0이다(README 참조).

### 패치가 걸린 곳 (26 ExternalSecret / 20 트랙)

| 트랙 | ExternalSecret |
|---|---|
| `services/*` 12 | 앱 비밀 13 (`video` 만 2개 — `mp-video-secrets`·`mp-gcp-sa`) |
| `common` `ingress` `pipelines` `platform/pgsync` `platform/rollouts` | `mp-harbor-pull` 5 |
| `gateway-internal` `ingress` | `mp-cloudflare-api-token` 2 |
| `pipelines` | `mp-pipeline-secrets` |
| `platform/es` | `mp-es-service-accounts` — 🔴 **이 트랙만 `name:` 지정**. 같은 트랙 `mp-elasticsearch-exporter-auth` 는 generator(Password CR) 기반이라 `secretStoreRef` 가 **일부러 없다**. `kind` 만으로 잡으면 없던 필드가 생긴다 |
| `platform/pg` | `mp-pg-replica-source` · `mp-pg-onsite-minio` (`mp-pg-backup-s3` 는 **삭제** — 아래) |
| `platform/pgsync` | `mp-pgsync-secrets` |

**`services/cloudflared` 는 패치가 없다** — eks 오버레이 자체가 없고(C-5) 그게 정답이다.

### 🔴 이 레포 밖에 남은 ExternalSecret 2개

라이브 30개 중 **`observability/mp-alertmanager-slack`** 과 **`argocd/repo-food-budget-config`**
는 config 레포에 없다(Ansible 이 직접 apply). **eks 에서 이 둘의 스토어를 안 갈면
알림이 조용히 멈추고 ArgoCD 가 레포를 못 읽는다.** 앱 레포 쪽 숙제 —
`bootstrap/eso/README.md` "앱 레포에 필요한 것 ③".

### 0-16 (정적 AWS 키)이 여기 얹힌 이유

같은 파일을 두 번 열지 않으려고 한 커밋에 넣었다. **eks 오버레이에서만** 벌어지는 일이다:

- `pipelines` — `mp-pipeline-secrets` 에서 `AWS_ACCESS_KEY_ID`·`AWS_SECRET_ACCESS_KEY` 두 엔트리 제거.
  🔴 **워크로드 22개를 고치는 게 아니다.** 전부 `envFrom.secretRef` 로 시크릿을 **통째로** 받으므로
  시크릿에서 키를 빼면 22개가 한꺼번에 정리된다. (온프렘은 2개 CronJob 이 여전히 키가 필요해
  0-14d 처럼 시크릿을 쪼개야 한다 — **사이트별로 해법이 다르다**.)
  인덱스 기반 `op: remove` 앞에 **`op: test` 를 붙였다** — base 의 순서가 바뀌면 렌더가 죽어서 드러난다.
- `platform/pg` — `mp-pg-backup-s3` ExternalSecret 삭제 + ObjectStore `inheritFromIAMRole: true`
  + Cluster `serviceAccountTemplate` 애너테이션(`PLACEHOLDER` 계정 ID). 셋이 한 묶음이다.
  ⚠️ `mp-pg-onsite-minio` 는 **MinIO 자격증명이라 AWS 가 아니다** → 0-16 범위 밖. 안 건드렸다.

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
  mealplanning ✅IaC       fb-secrets Secret        platform-root      ✅IaC
  mealplanning-root        └ESO→ argocd ns          mealplanning-root  ✅IaC(#581)
  platform                                             (✅ 2026-08-10 IaC 편입 · 앱 #581)
  platform-root
                                 │
     ┌───────────────────────────┴────────────────────────────┐
     ▼                                                        ▼
 mealplanning-root                                       platform-root
 path: argocd/overlays/onprem                            path: platform/argocd/overlays/onprem
 directory 없음(=Kustomize 모드)                          directory 없음(=Kustomize 모드)
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

1. **(config PR) 복사** — ✅ **실행 완료 2026-08-10** (`feat/argocd-roots-cutover-1`).
   `argocd/{base,overlays/{onprem,eks}}` · `platform/argocd/{base,overlays/{onprem,eks}}` 를
   **새로 만들었다. 기존 `argocd/applications/*.yaml`·`platform/argocd/*.yaml` 은 건드리지 않았다.**
   이 시점엔 같은 Application 정의가 git 에 두 벌 있지만 **뿌리는 옛 경로만 읽으므로 새 쪽은 무생물**이다.

   **렌더 불변 증명** (방법 = 아래 §검증 방법의 정규화 비교):

   | 뿌리 | 옛 경로(directory 모드) | 새 경로(kustomize) | 문서 | 신원 집합 | 전 필드 값 |
   |---|---|---|---|---|---|
   | `mealplanning-root` | `argocd/applications` (recurse=true) | `argocd/overlays/onprem` | 23 = 23 | 동일 | **차이 0** |
   | `platform-root` | `platform/argocd` (include=`*.yaml`) | `platform/argocd/overlays/onprem` | 21 = 21 | 동일 | **차이 0** |

   - 복사한 44개 파일은 원본과 **바이트 단위 동일**(`cmp` 전건 통과).
   - 렌더 **텍스트**는 바이트 비교가 성립하지 않는다 — kustomize 가 재직렬화하며 **주석을 버리고 키를 정렬**한다
     (27,942B → 11,909B / 64,560B → 25,600B). ArgoCD 는 파싱된 오브젝트를 비교·적용하므로 판정 기준은
     전 필드 값 동일이고, 이는 0-1 이 디렉터리형 트랙에 쓴 것과 같은 방법이다.
   - 렌더된 child 44개 = 라이브 Application 46개 − 뿌리 2개, **이름 집합 완전 일치**(2026-08-10 실측).

   🔴 **이 단계가 라이브를 못 건드리는 근거 2줄** — 새 디렉터리를 두 뿌리가 읽지 않는다.
   - `mealplanning-root` 는 `argocd/applications` 만 본다. 새 `argocd/base`·`argocd/overlays` 는
     그 **밖의 형제 디렉터리**라 `recurse: true` 의 탐색 범위에 아예 없다.
   - `platform-root` 는 `platform/argocd` 를 보되 **`recurse` 가 없다(=false)** → 하위 디렉터리를 읽지 않는다.
     새 `platform/argocd/base`·`overlays` 는 하위다. 게다가 이 뿌리는 **`prune: false`** 라,
     설령 읽는다 해도 **child 삭제는 구조적으로 불가능**하다(같은 이름·같은 내용의 중복 적용 = SSA no-op).
2. **(앱 레포 PR + 적용) 뿌리 재지정** — Ansible `k8s_argocd` 가 `mealplanning-root` Application 을
   **처음으로** 관리하게 하고, 두 뿌리의 `path` 를 `…/overlays/{{ argocd_site }}` 로 바꾼다.
   적용 후 확인:
   ```bash
   kubectl -n argocd get applications -o custom-columns=NAME:.metadata.name,PATH:.spec.source.path,SYNC:.status.sync.status
   ```
   **child 44개가 그대로 있고 전부 Synced** 여야 한다. 하나라도 사라지면 즉시 되돌린다.
3. **(config PR) 옛 경로 삭제** — ✅ **실행 완료 2026-08-10** (`feat/argocd-roots-cutover-3`).
   `argocd/applications/*.yaml`(23) · `platform/argocd/*.yaml`(21) 삭제.
   **삭제 전후 렌더 차이 0**(뿌리가 보는 경로에서 각각 Application 23 · 21 그대로).
   같이 정합시킨 것 = `validate.py` 의 `DIRECTORY_APPS` 에서 두 뿌리 제거(이제 kustomize 트랙이다)
   · `bootstrap/argocd/base/roots.yaml` 을 라이브 실물로 갱신 · README·AGENTS 의 구조 서술.

✅ **1↔3 사이의 "정본이 두 곳" 창은 닫혔다**(2026-08-10 같은 날). 그 창에서 child 를 수정하면
양쪽 다 고쳐야 했는데, 이제 정본은 `argocd/base` · `platform/argocd/base` 하나씩이다.

### 🔴 실측된 답 — `directory` 가 있으면 kustomize 자동판별이 없다

2단계 직전에 라이브 Application **46개를 전수 조회**해 확정했다(2026-08-10):

| 조건 | `status.sourceType` | 예외 |
|---|---|---:|
| `spec.source.directory` **있음** | `Directory` | 0 |
| `directory` 없음 + `kustomization.yaml` 있음 | `Kustomize` | 0 |
| `directory` 없음 + kustomization 없음 | `Directory`(자동판별) | 0 |

→ **2단계는 `path` 만 바꿔선 안 되고 `directory:` 블록을 같이 걷어내야 한다.** 남겨 두면 뿌리가
`kustomization.yaml` 을 매니페스트로 읽어 유효 리소스가 0 이 되고, 앱 뿌리는 `prune: true` 다.
앱 레포 #582 가 그 처리를 포함했고, 적용 후 두 뿌리의 `sourceType` 이 **`Kustomize`** 로 전환된 것을
확인했다(child 44개 생존 · 전부 Synced).

⚠️ 그래도 **감시 중인 디렉터리 최상단에 `kustomization.yaml` 을 두지 않는다**는 원칙은 유효하다 —
지금은 뿌리가 `overlays/onprem` 을 보고 그 아래에만 kustomization 이 있다.

## 이 구조가 열어주는 것 (Wave B)

Helm 12개의 인라인 `valuesObject` 에 갇혀 있던 **사이트 결합 값 15건**이 패치 가능해진다.

| 값 | 건수 | 어디 |
|---|---|---|
| **0-5** nodeSelector (`kubernetes.io/hostname`) | 5 | `loki` 1 · `kubecost` 4 |
| **0-5** nodeSelector (`topology.kubernetes.io/zone: host-b`) | 1 | `tempo` 1 |
| **0-8** storageClass (`openebs-lvm`) | 6 | `loki` 1 · `tempo` 1 · `kubecost` 4 |
| **0-9** Harbor LAN IP (`192.168.0.10/…`) | 1 | `rollouts` initContainer 이미지 — 🔴 **0-9 의 유일한 미해결분**(아래) |
| MinIO 인클러스터 엔드포인트 | 2 | `loki` · `tempo` (S3 전환 대상) |

🔴 **"열어준다"는 아직 미래형이다** (2026-08-10 확인). 위 설계는 **실증**됐지만
`platform/argocd/` 는 여전히 평평하다 — `base/`·`overlays/` 가 없다(§0-4 컷오버 1단계 미실행).
그래서 이 15건은 **지금 config 레포 PR 로 못 고친다.** 0-5(nodeSelector 6건)·0-8(SC 6건)의
실작업 상당수가 여기 갇혀 있으므로, **0-4 컷오버가 그 항목들의 선행**이다.
0-27(CPU 요청)도 같은 벽에 걸린다 — `redis-operator`·`kubecost-finopsagent` 의 CPU 요청은
이 레포에 **값이 아예 없고**(차트 기본값) Application 의 `valuesObject` 로만 덮을 수 있다.

나머지 8개(`alloy` `keda` `descheduler` + 오퍼레이터 5)는 **사이트 결합 값이 0** 이라
분기 자체가 필요 없다 — eks 오버레이에서 패치할 것이 없다.

---

# §0-9 / 0-10 — 레지스트리 분기 · 검증기 사이트화 (2026-08-10)

## 한 것

- **0-9** eks 렌더에서 Harbor LAN IP 를 없앴다. 0-1 직후 상태는 `services/*` 13종만 ECR 매핑이
  있었고 나머지는 렌더가 `192.168.0.10/…` 을 그대로 가리켰다 — 채운 곳:
  `services/video`(images 블록 자체가 비어 base 의 `:latest` 가 샜다) · `pipelines`(2종) ·
  `platform/pgsync` · `platform/es`(pgsync + **Elasticsearch CR `spec.image` 는 patch**).
- **1-31(config 레포 절반)** Harbor pull secret `ExternalSecret/mp-harbor-pull` 을 eks 에서 제거
  (`common` · `ingress` · `pipelines` · `platform/rollouts` 4트랙).
- **0-10** `validate.py` 에서 온프렘 LAN CIDR 리터럴을 걷고 `scripts/sites.yaml` 로 옮겼다
  (+ `check_registry_split()` 신설).

## 🔴 남은 1건 — `platform/argocd/rollouts.yaml` 의 initContainer

Argo Rollouts 컨트롤러의 Gateway API 플러그인 initContainer 이미지가
`192.168.0.10/mealplanning/mp-rollouts-gatewayapi-plugin:<sha>` 다.

**여기서 못 고친다.** 그 값은 Helm 소스 Application 의 **인라인 `valuesObject`** 안에 있고,
`platform/argocd/` 는 아직 `base/ + overlays/` 로 안 갈라져 있다(§0-4 컷오버 1~3단계가 선행).
`check_registry_split()` 도 그래서 이걸 못 본다 — **의도된 사각지대이므로 여기 적어 둔다.**

컷오버로 `platform/argocd/overlays/eks` 가 생기면 아래 패치 한 조각이면 된다:

```yaml
patches:
  - target: {group: argoproj.io, kind: Application, name: rollouts}
    patch: |
      apiVersion: argoproj.io/v1alpha1
      kind: Application
      metadata: {name: rollouts}
      spec:
        source:
          helm:
            valuesObject:
              controller:
                initContainers:
                  - name: vendored-gatewayapi-plugin
                    image: <eks.registry>/mp-rollouts-gatewayapi-plugin:314b898fe831b2cbe38c2f17fa43a00441bcce08
                    # …원본의 command·securityContext·resources·volumeMounts 전부 그대로 옮길 것
```

⚠️ `initContainers` 는 **리스트**라 JSON merge patch 가 통째로 교체한다 — 원본의 나머지 필드
(`command`·`securityContext`·`resources`·`volumeMounts`)를 빠짐없이 옮겨 적어야 한다.
§0-4 가 `loki` 에서 실증한 "필드 하나만 갈아끼우기"는 대상이 **맵**이라 성립했던 것이다.
빠뜨리면 `readOnlyRootFilesystem`·`runAsUser: 999` 같은 하드닝이 조용히 증발한다.

## ECR 쪽 전제 (이 레포 밖 · 사람 결정)

- **리포지터리를 미리 만들어야 한다.** ECR 은 push 시 자동 생성되지 않는다 —
  eks 렌더가 참조하는 **`mp-*` 리포지터리 17개**가 대상이다(실측 — Terraform 소관).
  🔴 여기에 `mp-rollouts-gatewayapi-plugin`(위 미해결분) 과, EKS 로 안 가는 `mp-cloudflared`
  (C-5 온프렘 DR 전용)는 **안 들어 있다.** 전자는 컷오버 뒤 18개가 된다.
- **arm64.** 노드가 `m7g.xlarge`(Graviton)라 지금의 amd64 단일 이미지는 안 뜬다.
  특히 우리가 굽는 `mp-elasticsearch-nori`·`mp-pgsync`·`mp-rollouts-gatewayapi-plugin` 은
  멀티아키 빌드가 **앱 레포 CI 의 선행 과제**다.
- **C-3 = Harbor 존치(ECR 미러).** 온프렘 오버레이는 계속 Harbor 를 가리킨다 — 지우는 게 아니다.

## 앱 레포(Ansible) 쪽에 필요한 변경 — 명세

🔴 **이 레포에서 할 수 없는 부분이다.** 뿌리를 적용하는 주체가 Ansible `k8s_argocd` 롤이라
(앱 레포 `infra/ansible/roles/k8s_argocd/`) 거기 PR 이 선행돼야 위 컷오버 2단계가 성립한다.

1. **`mealplanning-root` Application 템플릿 신설** — 지금 **어떤 IaC 에도 없는 유일한 오브젝트**다.
   `argocd-platform-root.yaml.j2` 가 플랫폼 쪽에 하는 일을 앱 쪽에 그대로 해주면 된다.
   정확한 목표 spec = `bootstrap/argocd/base/roots.yaml`(라이브와 필드 단위 일치 검증됨).
   🔴 `spec.project` 는 **`mealplanning-root`** 다. `mealplanning` 으로 쓰면 root 가
   `InvalidSpecError` 로 죽는다(그래서 지금 라이브에 손 patch 가 들어가 있다).
   🔴 finalizer 는 **라이브에 없다**. 붙이면 삭제 의미론이 바뀌므로 별건으로 판단할 것.

2. **`argocd_allowed_namespaces` 에 `mp-ingress` 추가** — 라이브에만 있는 drift 다.
   지금 `--tags argocd` 를 돌리면 **destination 이 지워지고 `mp-ingress` Application 이 배포 거부**된다.
   descheduler 가 같은 방식으로 이미 한 번 죽었다(`defaults/main.yml` 주석).

3. **사이트 변수 도입** — `argocd_site: onprem` 를 두고 두 뿌리의 `path` 를
   `argocd/overlays/{{ argocd_site }}` · `platform/argocd/overlays/{{ argocd_site }}` 로.
   이게 A안에서 사이트를 가르는 **유일한 스위치**가 된다.

4. (선택) **정본 일원화** — 위 오브젝트들의 정본을 Ansible 템플릿에 계속 둘지,
   `bootstrap/argocd/` 로 옮기고 Ansible 은 `kubectl apply -k` 만 할지. **미결(사람 결정)**.
   지금은 Ansible 템플릿이 정본이고 `bootstrap/` 은 기록이다 — 둘이 갈리지 않게 하는 것이 관건.

---

# §0-3 — ArgoCD 밖(Ansible 단독)이던 것을 **EKS 쪽에만** config 로 (2026-08-13)

> 근거 = 앱 레포 `docs/mp_aws_prep_checklist.md` **0-3** · 결정 **C-83**(온프렘 형상 동결 · AWS 는 덧셈만).
> 함께 해소된 것 = **0-3b**(externalLabels) · **1-27**(retentionSize) · **0-3c 잔여**(스크레이프 job) ·
> **1-28**(알림 룰셋 — `monitoring/EKS-RULESET.md`) · **0-8d**(emptyDir 상한 — `docs/eks-emptydir-sizing.md`).

## 🔴 계획이 바뀌었다 — "소유권 이전" 이 아니라 "EKS 에 신설"

종전 0-3 은 **온프렘의 Helm 소유를 ArgoCD 로 넘기는** 작업이었다. C-83 으로 **철회**한다.

| | 종전 계획 | 지금 |
|---|---|---|
| 온프렘 | Helm/Ansible → ArgoCD 소유로 이전 | **손대지 않는다**(형상 동결) |
| EKS | (이전된 것을 상속) | `overlays/eks` 에 **신설** |
| 위험 | 🔴 ArgoCD 가 prune 할 수 있는 **유일한 국면**이었다. 라이브 PVC **21/21 이 reclaimPolicy=Delete** 라 Prometheus 30Gi·`ranker.pkl` 이 날아갈 수 있었다(체크리스트 0-8b) | **위험 소멸** — 온프렘 오브젝트를 ArgoCD 가 아예 안 본다 |

## ArgoCD 밖 릴리스는 정확히 4종이었다 (실측 2026-08-13)

판정 = `helm list -A` + 오브젝트에 `argocd.argoproj.io/tracking-id` **부재**.

| 릴리스 | ns | 차트 | EKS 처리 |
|---|---|---|---|
| `metrics-server` | kube-system | `metrics-server-3.13.1` | ✅ 신설 — 🔴 **EKS 기본 제공 아님. 라이브 account HPA 가 의존한다** |
| `node-exporter` | kube-system | `prometheus-node-exporter-4.56.1` | ✅ 신설(스택과 별개 릴리스 구조 유지) |
| `kube-prometheus-stack` | observability | `kube-prometheus-stack-87.20.0` | ✅ 신설 |
| `minio` | observability | `minio-5.4.0` | 🔴 **신설하지 않는다** — C-18(MinIO 삭제 · S3). 다시 세우면 이관 목적을 되돌린다 |

🔴 **metrics-server 가 이 항목에서 제일 위험한 한 개다.** 없으면 **파드는 뜨고 스케일만 조용히 죽는다**
(HPA `FailedGetResourceMetric` · `kubectl top` 불가). 우리 룰셋에 "HPA 가 메트릭을 못 읽는다" 를
보는 알람이 **없어서** 부하가 올 때까지 아무도 모른다.

## 새 트랙 `platform/cluster-baseline` — base 가 **의도적으로 비어 있다**

담는 것 = **PriorityClass 3 · Namespace 5(PSA 라벨) · ResourceQuota 2 · LimitRange 3**.
전부 온프렘에서는 Ansible 소유다 → base 에 본문을 두면 `overlays/onprem` 이 상속해
① 온프렘 렌더가 바뀌고(C-83 위반) ② 라이브 오브젝트 소유가 Ansible↔ArgoCD 이중이 된다(selfHeal 왕복).

```
platform/cluster-baseline/
  base/kustomization.yaml        resources: []   ← 사이트 공통 본문이 0 이라 비었다. 지우지 말 것
  overlays/onprem/               렌더 0 바이트 = 정답 (Application 도 만들지 않았다)
  overlays/eks/                  본문의 유일한 자리
```

🔴 **`base/` 를 지우면 `validate.py check_site_overlays()` 가 이 트랙을 발견하지 못해**
eks 오버레이가 조용히 썩는다. 비어 있음은 실수가 아니라 계약이다.

- **PriorityClass** — 없으면 참조하는 워크로드 **46개**가 Pending 이 아니라 **admission 에서 거부**된다.
- **PSA** — 🔴 **EKS 는 PSA 라벨을 기본으로 안 붙인다.** 없으면 하드닝(#505)이 AWS 에서
  **조용히 사라진다**(파드는 전부 잘 뜨므로 아무도 모른다). 보안 회귀 중 발견이 가장 늦는 유형이다.
- **LimitRange** — 요청 미기재 컨테이너가 요청 0 으로 스케줄돼 스케줄러가 노드 용량을 과대평가한다.

## 🔴 앱 레포(Ansible)에 필요한 변경 — 이것 없이는 **sync 가 거부된다**

`AppProject` 는 IaC 밖 drift 이력이 있는 자리다(mp-ingress·descheduler 가 각각 한 번씩 죽었다).
실측 2026-08-13 기준 **네 줄**이 빠져 있다:

| # | 대상 | 넣을 것 | 없으면 |
|---|---|---|---|
| ① | `AppProject/platform.sourceRepos` | `https://kubernetes-sigs.github.io/metrics-server/` | metrics-server sync 거부 |
| ② | 〃 | `https://prometheus-community.github.io/helm-charts` | node-exporter·kube-prometheus-stack sync 거부(**둘이 같은 레포**) |
| ③ | `AppProject/platform.destinations` | `app` · `pipeline` · `mp-ingress` | ResourceQuota·LimitRange 가 "destination is not permitted" |
| ④ | `AppProject/platform.clusterResourceWhitelist` | `""/Namespace` · `scheduling.k8s.io/PriorityClass` | "resource not permitted in project" |

그 외 이 레포 밖 전제 2건:
- `ExternalSecret/observability/mp-alertmanager-slack` — **config 레포에 없다**(§0-2 "이 레포 밖에 남은
  ExternalSecret 2개"). 없으면 Alertmanager 파드가 시크릿 마운트 실패로 **아예 안 뜬다.**
- SSM `/mp/prod/observability-secrets` 에 `GRAFANA_ADMIN_USER`·`GRAFANA_ADMIN_PASSWORD`
  (→ `platform/observability/overlays/eks/externalsecret-grafana-admin.yaml`).
  🔴 온프렘 values 는 `grafana.adminPassword` 를 평문으로 갖지만 그 파일은 git 밖이다.
  **이 레포는 공개**라 같은 방법을 쓸 수 없어 `admin.existingSecret` 으로 우회했다.

⚠️ **순서 의존 1건** — `observability-secrets` Application 의 `source.path` 가 아직
`platform/observability/overlays/onprem` 이다(eks 렌더에서 git 소스 child **10개**가 여전히
onprem 경로다 — §0-4 가 남긴 일괄 전환). 그 전환 전까지 위 Grafana ExternalSecret 은 무생물이다.

## 검증 (이번 변경이 온프렘을 안 건드렸다는 증명)

```
onprem 오버레이 37개 렌더 — 작업 전후 **바이트 단위 동일** (cmp 37/37 통과)
  집계 sha256(파일별 해시의 해시) = afe00ca088809759318066df81ae42b1a385ab1ccb0d3a789b8e9a74de6fccd7
새 트랙 platform/cluster-baseline/overlays/onprem 렌더 = **0 바이트**
python3 scripts/validate.py = ✅ 통과 (경고 2건 — 작업 전과 동일)
  kustomization 37 → 38 (새 트랙의 onprem 오버레이 1개) · 매니페스트 287 → **287**(불변)
```
