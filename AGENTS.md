# AGENTS.md — mealplanning-config

AI 코딩 에이전트(Claude Code, opencode, Codex, Copilot 등)를 위한 이 레포의 컨텍스트.

> 신설 2026-08-04. 사람용 진입점은 `README.md` — 이 문서는 그 요약이 아니라 **에이전트가
> 자주 밟는 지뢰**를 앞으로 당겨놓은 것이다. 충돌하면 `README.md` 와 정본 런북이 맞다.
> 🔴 앱 레포(`happyInit/food-budget-app`)의 `CLAUDE.md`·`AGENTS.md` 는 **여기 자동 로드되지 않는다.**
> 인프라 결정의 why/how 가 필요하면 그 레포 `docs/mp_k8s_infra_migration_plan.md` 를 따로 읽어야 한다.

## 이 레포가 뭔가

**mealplanning 앱의 K8s 배포 매니페스트만 담는 ArgoCD GitOps config 레포.**
ArgoCD 가 여기를 watch 해서 클러스터를 이 내용에 맞춘다 — 즉 **여기 있는 것 = 클러스터에 떠 있어야 할 것**.

🔴 **앱 소스 코드는 여기 없다.** Python 서비스·Dockerfile·파이프라인은 전부 `happyInit/food-budget-app`.
여기서 애플리케이션 로직을 고치려 하지 말 것 — 레포를 잘못 찾은 것이다.

🔴 **모든 ArgoCD 트랙이 같은 모양이다** (2026-08-09, AWS 이관 0-1) — 정본 = [`SITES.md`](SITES.md).

```
<트랙>/base/               매니페스트 본문 (사이트 공통)
<트랙>/overlays/onprem/    ArgoCD Application 의 source.path 가 가리키는 곳
<트랙>/overlays/eks/       EKS 골격 — 🔴 Wave B 에서 채운다. 렌더 검사 대상에서 제외
```
```
argocd/                앱 child Application (mealplanning-root 가 집는다) — base/overlays 있음
platform/argocd/       플랫폼 child Application (platform-root)      — 같은 모양
services/<svc>/        앱 서비스 13 + cloudflared. overlays/onprem 의 images.newTag = Jenkins 자리
platform/              인프라 소관 — pg pooler es kafka redis pgsync rollouts policies*
pipelines/             컨슈머 + CronJob.  🔴 pipelines/jobs/ 는 트랙 밖(1회성 kubectl)
monitoring/            PrometheusRule·ServiceMonitor·대시보드
common/ gateway/ gateway-internal/ ingress/   공용·라우트·게이트웨이
ops/                   🔴 ArgoCD 비대상 — 수동 migration/runbook 번들
scripts/validate.py    푸시 전 관문 (아래 §검증)
scripts/sites.yaml     🔴 사이트(온프렘/EKS) 결합 값의 단일 선언점 — validate.py 가 읽는다
tests/                 validate.py 의 단위 테스트
```

🔴 **새 매니페스트를 추가하면 `base/kustomization.yaml` 의 `resources` 에 등록해야 한다.**
`platform/{es,kafka,pg,pgsync,pooler,redis,rollouts}` 는 2026-08-09 전까지 "디렉터리형"이라
파일만 두면 배포됐다. 이제는 아니다 — **등록 안 하면 조용히 배포되지 않는다.**

**뿌리가 둘이다** — `mealplanning-root`(앱, `argocd/overlays/<site>`) · `platform-root`(플랫폼,
`platform/argocd/overlays/<site>`). 서로 남의 디렉터리를 보지 않는다. 새 매니페스트를 추가하면 **해당 뿌리 밑
child Application 에 배선돼 있는지** 확인할 것. 파일만 만들면 ArgoCD 는 그것을 모른다.

## 🔴 절대 규칙

1. **이미지 핀은 `:sha`** — `:latest` 금지. ArgoCD 가 변경을 감지 못 하고 롤백 대상이 사라진다.
   `overlays/onprem/kustomization.yaml` 의 `images.newTag` 는 **Jenkins 가 커밋하는 자리**다 — 손으로 건드리지 말 것.
2. **비밀 값을 여기 두지 않는다** — 전부 ESO(`fb-secrets` → `ClusterSecretStore/fb-kubernetes`).
   이 레포엔 `ExternalSecret`(참조)만 있고 실제 값은 없다. 비밀번호·토큰·인증서 키를 파일에 쓰는 순간 규칙 위반이다.
3. **`.github/` 를 만들지 말 것** — 이 조직의 CI 는 **Jenkins**(호스트 C)다. GitHub Actions 는 은퇴했고
   되살릴 수 없다. 과거에 이걸 모르고 config 레포에 워크플로를 추가했다가 원복한 사고가 있다(config#98 → #100).
   돌지 않는 껍데기가 남으면 "여긴 Actions 로 CI 한다"로 읽혀 CI 정본이 둘로 보인다.
4. **namespace 를 여기서 만들지 않는다** — `app` 등은 인프라 Ansible 롤이 PSS 라벨과 함께 생성한다.
5. **4-dot FQDN 금지** — `<svc>.<ns>.svc.cluster.local` 은 파드 search 도메인의 `local` 때문에 ISP 로 새어
   공인 IP 로 해석된다(실측 21.7%). `<svc>.<ns>.svc` 까지만 쓴다.
6. **`ops/` 를 Application source 로 연결하지 않는다** — 파괴 가능한 일회성 절차는 desired state 가 아니다.
7. **새로 짓는 이름은 `mp-` 접두사** (`fb-` 금지). 단 **기존 실물 이름**(`fb-data`·`fb-secrets` ns·
   `fb-kubernetes` SecretStore·`es-es-http`·`es-es-elastic-user` 등)은 **그대로 참조**한다 — 참조를 깨면 배포가 죽는다.
   K8s 세부: `Service` 는 bare 이름(`account`·`recipe`…), 그 외 오브젝트는 `mp-` 접두사.

## 검증 — 🔴 푸시 전에 반드시 돌린다

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
python3 scripts/validate.py
```

이 레포엔 CI 가 없다. **이 두 명령이 유일한 관문이다.**

검사하는 것은 "문법이 맞나"가 아니라 **렌더 결과가 의도대로 나오나**다 — 여기서 새어나간 사고는
전부 *적용이 성공하고 에러도 없던* 것들이었다:

| 검사 | 무엇을 막나 |
|---|---|
| `kustomize build` 전체 | 렌더 실패 |
| `kubeconform` | 스키마 위반. 🔴 ArgoCD 의 apply 는 strict 가 아니라 **모르는 필드를 조용히 프루닝**한다 |
| 4-dot FQDN 금지 | 위 §절대규칙 5 |
| `:latest` 금지 | ArgoCD 가 변경을 감지 못 함 |
| `topologyKey` 중복 금지 | patchMergeKey 충돌로 제약 하나가 조용히 사라진다 |
| CNPG failsafe 정책 | instance 간 `5432/8000` 비대칭과 K8s/Cilium additive `8000` 우회를 막는다 |
| 사이트 레지스트리 분기 | `overlays/eks` 렌더가 Harbor LAN IP 를 가리키면 실패. eks 는 정책 검사에서 빠지므로(SKIP_KUSTOMIZE_RE) **이 검사만이 유일한 관문**이다 — 이관 당일 ImagePullBackOff 방지 |
| securityContext 베이스라인 | JSON-Patch `op: add` 는 merge 가 아니라 replace 라 하드닝이 렌더에서 증발한다 |

**`scripts/policy-baseline.txt` 는 알려진 위반을 얼려둔 목록**이고 **줄어들기만 해야 한다.**
🔴 검증을 통과시키려고 베이스라인에 줄을 추가하지 말 것 — 그건 회귀를 승인하는 것이다.
갱신이 정말 필요하면 `python3 scripts/validate.py --list`.

### 도구가 없을 때
- `PyYAML` 은 **필수** (`pip3 install --user pyyaml`).
- 렌더러는 `kustomize` → 없으면 `kubectl kustomize` 폴백. **둘 다 없으면 검증이 아예 못 돈다.**
- `kubeconform` 은 없으면 스키마 검증만 건너뛴다(경고).

## ArgoCD 반영 — 머지가 곧 배포는 아니다

🔴 **auto-sync 여부가 앱마다 다르다** (2026-08-02 실측: automated 26 / manual 15).

**manual sync 15개** = `app-common` · `gateway` · `gateway-internal` · `monitoring` · `mp-cloudflared` ·
`pipelines` · `mp-policies{,-data,-pipeline}` · **데이터 CR 6종**(`pg` `pooler` `es` `kafka` `redis` `pgsync`).
→ 이 목록에 해당하는 것을 고쳤다면 **머지만으로는 아무 일도 일어나지 않는다.**

```bash
kubectl patch application -n argocd <앱> --type merge -p '{"operation":{"sync":{"revision":"HEAD"}}}'
```

🔴 **`envFrom.configMapRef` 는 파드 기동 시점에만 주입된다.** `common/base/app-common.yaml` 같은 ConfigMap 을
바꾸고 sync 해도 **도는 파드는 옛 값을 그대로 쓴다.** 체크섬 어노테이션이 없어 ArgoCD 가 자동으로
굴려주지 않으므로, 해당 워크로드에 `rollout restart` 가 **별도로** 필요하다.

## PGSync — 🔴 정본이 두 레포에 갈라져 있다

`platform/pgsync/base/schema-configmap.yaml` 과 `plugins-configmap.yaml` 의 내용은
앱 레포 `deploy/pgsync/schema.json` · `deploy/pgsync/plugins/*.py` 의 **사본**이다.
(앱 레포 쪽이 빌드 컨텍스트 원본, 여기 ConfigMap 이 실제 배포본.)

**한쪽만 고치면 갈린다.** PGSync 관련 변경은 반드시 양쪽을 같이 본다.

인덱스 settings/mapping 의 정본은 **이 레포** `ops/pgsync-stable-alias/recipes-index.json` 이다.
🔴 alias 뒤의 물리 인덱스명을 앱·PGSync 설정에 직접 박거나 앱 레포에 mapping 사본을 만들지 말 것.
alias 라이프사이클 절차 = `ops/pgsync-stable-alias/README.md`.

## 클러스터 접근

로컬 개발 머신에서 `kubectl` 이 클러스터에 **직접 닿지 않는다.** 전부 SSH 경유:

```bash
ssh wsl-dev 'kubectl get pods -n app'
ssh wsl-dev 'kubectl -n argocd get applications | grep -v Synced'
```

- 🔴 **원격에 `argocd` CLI 는 없다** — ArgoCD 조작은 전부 kubectl 로.
- `wsl-dev` 는 각자 로컬 `~/.ssh/config` 의 별칭이다. **이 레포는 public 이라 실주소·계정·포트를 커밋하지 않는다.**
- 원격에도 클론이 있다(`~/mealplanning-config`) — **조회용**이다. 편집·push 는 로컬에서.

## 머지 정책

- **이 레포는 직접 머지 허용** — desired state 매니페스트라 리뷰 병목을 두지 않는다.
- 🔴 **앱 레포 `happyInit/food-budget-app` 는 정반대다 — PR 리뷰 필수, 직접 머지 금지.**
  두 레포를 함께 고치는 작업에서 습관적으로 같은 절차를 적용하지 말 것.
- 앱 레포의 문서·스키마가 이 레포의 ops SSOT 를 참조할 때는 **config 쪽이 먼저 머지**돼야 한다.

## 작업 시 주의

- **확정된 것만 기록한다.** 추천·검토중인 안을 결정처럼 매니페스트나 주석에 쓰지 말 것.
- **해소된 결정을 재논의하지 말 것** — CNI=**Cilium** · 서비스메시=**Istio sidecar** · Gateway API 구현체=**Istio** ·
  외부 LB=**MetalLB** · 부트스트랩=**kubeadm 직접** · Redis 오퍼레이터=**OT-Container-Kit** ·
  ES=**인증 켬·HTTP TLS 끔** · CD=**ArgoCD 단독**. 근거는 앱 레포 `docs/mp_k8s_infra_migration_plan.md`.
- 주석은 **왜**를 남긴다. 이 레포의 기존 매니페스트는 값 옆에 근거·실측·사고이력을 달아두는 관례가 있다 —
  그 밀도를 따라간다.
