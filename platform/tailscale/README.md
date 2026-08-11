# platform/tailscale — C-53 사람 평면 (Tailscale)

> **한 문장: "데이터는 S3 로, 사람은 Tailscale 로."**
>
> 🔴 이 트랙에 데이터 평면(크롤 운반 C-44 · 서비스 간 호출)을 태우면 그 실측 근거가 무효가 된다.
> 경계선은 두 곳에서 **강제**된다 — tailnet 쪽 `acl/policy.hujson` 의 `tests`(deny 단정 →
> 어기면 ACL push 실패)와 K8s 쪽 `base/netpol-proxy-egress.yaml`(프록시가 data ns 5432·9200 밖으로 못 나감).
>
> ⟳ **PG WAL 복제만은 예외가 생겼다 — C-55(2026-08-11, 선생님 지시).** 이 트랙은 C-53(08-10)
> 시점에 쓰였고 그때는 무조건 금지였다. C-55 는 C-40 의 기각 근거를 **"복제 슬롯을 만들 때만
> 성립한다"** 로 좁혔고(원격 replica cluster 는 슬롯 없이 붙어 백프레셔가 없다),
> PG 복제를 2스텝으로 갈랐다 — **Step 1(S3 아카이브, 터널 없음)이 이관 선행이고
> Step 2(Tailscale 직접 스트리밍)는 이관 후**다. 🔴 **지금은 여전히 금지이고, ACL 의 deny 단정도
> 그대로 둔다** — Step 1 만으로 Warm Standby 가 성립하므로 미리 열 이유가 없다.
> 가드레일은 하나로 압축된다: **원격 standby 용 복제 슬롯을 만들지 않는다.**

## 왜 Operator 와 subnet router 가 **세트**인가 — ⟳ 온프렘 한정이다

⟳ **C-55 로 AWS 쪽 subnet router 는 미채택**이 됐다. 하던 일을 다른 것이 이미 덮고 있었기 때문이다 —
노드 접속 = **SSM Session Manager**(C-37) · ElastiCache = 파드에서 `kubectl exec` ·
VPC-B/C 는 원래도 못 붙었다(VPC-A 대역만 광고). 남는 근거는 break-glass 뿐인데 그것도 SSM 이 덮는다.
⇒ **EC2 5대 → 4대.** 도구 서브넷은 비우되 **대역은 예약 유지**(사후 변경 불가 · 비용 $0).
🟡 되살릴 조건 = EKS 엔드포인트 IP 허용목록을 **exit node** 로 채우기로 할 때(체크리스트 미결 ⑮).

🔴 **온프렘은 다르다 — router 를 유지한다.** SSM 이 없고, 하이퍼바이저 `.12`·호스트 C `.10` 은
K8s Service 가 아니라 Operator 로 노출할 수도 없다. **router 가 유일한 경로다.**
아래 "세트" 논리는 그래서 **온프렘에서만** 성립한다(AWS 는 Operator 단독).

```
subnet router 가 광고할 수 있는 것 = LAN/VPC CIDR
  그 안에 있는 것   호스트 C `.10`(Harbor·Jenkins·SonarQube) · Proxmox `.12` · 노드 `.17`–`.20`
  🔴 그 안에 없는 것  PostgreSQL · Elasticsearch — **파드**다(10.20.0.0/16 Cilium 오버레이)
                     ⇒ subnet router 만으론 PG 에 못 붙는다
```
⇒ Operator 가 **Service 를 tailnet 디바이스로** 노출한다(`pg-pooler` · `es-es-http`).
대상마다 태그가 붙어 ACL 이 **포트 단위로** 좁힐 수 있다 — CIDR 광고에는 그 손잡이가 없다.

🔴 그래서 **Service CIDR(10.96.0.0/12)·파드 CIDR(10.20.0.0/16)을 광고하지 않는다.**
광고하면 클러스터의 모든 ClusterIP(Kafka·Redis·app ns·kubelet)가 tailnet 에서 도달 가능해진다.

## 🔴 키 정책 (C-53 ⑤) — auth key 가 새 장기 자격증명이 되면 안 된다

| | 결정 | 왜 |
|---|---|---|
| 자격증명 종류 | 🟢 **OAuth 클라이언트** (auth key 아님) | auth key 는 문자열 자체가 tailnet 가입 권한 = 장기 자격증명이다. OAuth 클라이언트는 오퍼레이터가 **필요할 때마다 단기 auth key 를 스스로 발급**한다 ⇒ 우리가 보관하는 비밀은 회수 가능한 클라이언트 하나로 줄고, 파드가 든 것은 단기 키다 |
| 🔴 secrets.yml 에 auth key | **금지** | 적는 순간 위 이득이 전부 사라진다 |
| 클라이언트 스코프 | `devices:core` + `auth_keys`(write), **태그 4개만** | OAuth 클라이언트는 자기 태그 밖의 디바이스를 만들 수 없다 = 폭발 반경 상한. `tag:k8s-operator` `tag:mp-pg` `tag:mp-es` `tag:mp-subnet-onprem` |
| 키 만료 | 🔴 **끄지 않는다** (기본 만료 유지) | "만료 없음" 은 곧 영구 자격증명이다. 오퍼레이터가 재발급하므로 만료를 켜 둬도 운영이 안 아프다 |
| 디바이스 자동승인 | 🔴 **끈다** = ACL 에 `autoApprovers` 를 **넣지 않는다** | 넣으면 키 하나로 **새 서브넷 경로가 사람 승인 없이** 열린다. 그게 바로 "키가 장기 자격증명이 되는" 경로다 |
| 서브넷 경로 승인 | **사람이 1회** (admin console → Machines → Edit route settings) | 위의 직접적 귀결. 승인 전에는 조용히 안 통한다 — 버그처럼 보이지만 의도다 |
| 사람 계정 | 5인 실명 (`group:dev`) | Tailscale **Personal 은 6인까지 무료**(실측 2026-08-10) ⇒ 요금 **$0**. 🔴 7인이 되는 순간 Standard **$8/user/월** 이다 → 서비스 계정을 사람 자리로 쓰지 말 것(그래서 태그 소유자가 `tag:k8s-operator` 다) |
| Tailscale SSH | **안 켠다** (`"ssh": []`) | 노드 접근은 이미 팀 SSH 키(`team_ssh_keys` 롤)가 정본이다. 두 신원 체계를 겹치면 "누가 들어왔나" 의 정본이 둘이 된다 |

**비용** = **월 $0**. ⟳ C-53 시점의 "$1.90" 은 AWS 도구 서브넷 `t4g.nano` subnet router 몫이었는데
**C-55 로 그 EC2 가 미채택**됐다. Tailscale 요금은 Personal 6인 무료라 $0 이고,
온프렘·AWS 양쪽 남는 구성요소(Operator·프록시·온프렘 router)는 전부 **클러스터 안 파드**라 추가 요금이 없다.

## 🔴 선행 3건 — 하나라도 없으면 sync 가 실패하거나 조용히 안 붙는다

| | 무엇 | 어디 | 없으면 |
|---|---|---|---|
| ① | ns `tailscale`, **PSS `enforce: privileged`** | 🔴 **app 레포 Ansible** `k8s_cluster_base` (ArgoCD 는 ns 를 만들지 않는다) | Tailscale v1.78+ 프록시 main 컨테이너는 `/dev/net/tun` 때문에 privileged + CAP_NET_ADMIN 이다 ⇒ 오퍼레이터 ns 기본값 `baseline` 이면 **프록시가 admission 거부**. 오퍼레이터 파드는 baseline 으로도 뜨므로 **"오퍼레이터는 떴는데 디바이스가 안 생긴다"** 로 헷갈리게 실패한다 |
| ② | AppProject `platform` destinations 에 `tailscale` | 🔴 **app 레포 Ansible** (실측: 라이브 AppProject 의 last-applied 가 `argocd-platform-project.yaml.j2` 출력과 일치) | 배포 거부 |
| ③ | sourceRepos 에 `https://pkgs.tailscale.com/helmcharts` | 🔴 **app 레포 Ansible** `k8s_argocd/defaults` | `InvalidSpecError: repo is not permitted` (descheduler 가 이걸로 죽어 있던 전례) |

🟢 ①②는 `group_vars/k8s_nodes.yml` 의 `k8s_operator_namespaces` **한 줄**이 동시에 만든다
(두 롤의 공통 입력이다). 단 PSS 는 `baseline` 하드코딩이라 오버라이드가 따로 필요하다.

## 적용 순서 — 🔴 뒤집으면 진단이 오래 걸린다

netpol 거부는 RST 가 아니라 **드롭**이라 로그가 안 남는다. 그래서 **연결을 먼저 세우고 나중에 잠근다.**

```
0)  ACL 먼저   acl/policy.hujson 을 tailnet 에 push (group:dev 이메일 5개 채운 뒤)
               🔴 태그가 tagOwners 에 없으면 다음 단계 OAuth 발급이 막힌다
1)  OAuth      admin console → Settings → OAuth clients (스코프·태그 = 위 키 정책)
2)  fb-secrets kubectl -n fb-secrets create secret generic tailscale-secrets \
                 --from-literal=TS_OAUTH_CLIENT_ID=... --from-literal=TS_OAUTH_CLIENT_SECRET=...
               🔴 값 확인에 describe 금지. 키 목록은 -o go-template
3)  Ansible    선행 3건 (ns privileged · AppProject · sourceRepos)
4)  sync       tailscale-operator  ← CRD 가 여기서 설치된다
5)  sync       tailscale (CR 트랙)  ← 🔴 netpol-proxy-egress.yaml 은 아직 kustomization 에서 빼고
6)  승인       admin console → Machines → mp-subnet-onprem 의 192.168.0.0/24 승인
7)  왕복 확인   psql -h <magicdns> -p 5432 ... / curl http://<magicdns>:9200
8)  잠금       netpol-proxy-egress.yaml 넣고 다시 sync
9)  🔴 재확인   7) 을 **다시** 한다. 지난 실측 교훈 — 관측창에 안 잡혔을 뿐인 경로가 있었다
```

MagicDNS 이름 확인(값 아님, 이름만):
```bash
kubectl -n tailscale get secret -l tailscale.com/parent-resource=mp-pg \
  -o go-template='{{range .items}}{{.metadata.name}}{{"\n"}}{{end}}'
```

## 되돌리기

사람 평면이라 **끊어도 데이터가 안 다친다.** 그게 이 설계의 좋은 성질이다.
```bash
# 1) CR 트랙만 내리기 (Service 노출·subnet router 소멸 · 오퍼레이터는 유지)
kubectl -n argocd patch application tailscale --type merge \
  -p '{"spec":{"syncPolicy":null}}'   # 이미 manual — 확인용
kubectl -n argocd delete application tailscale     # finalizer 가 자식까지 정리한다

# 2) 전체 철거
kubectl -n argocd delete application tailscale tailscale-operator
# 🔴 admin console 에서 디바이스도 지운다 — K8s 오브젝트만 지우면 tailnet 에 유령 디바이스가 남는다
# 🔴 OAuth 클라이언트를 revoke 한다. 파일 삭제 ≠ 자격증명 무효화
#    (github_runner PAT 에서 같은 실수를 한 전례가 있다)
```

## 남은 것

- `acl/policy.hujson` 의 `group:dev` = **실제 이메일 5개로 채워야 한다**(레포 공개라 미기재)
- ACL 을 GitOps 로 밀지(`tailscale/gitops-acls` 액션) 손으로 붙여넣을지 = 사람 결정.
  🔴 GitOps 로 하면 API 키가 또 하나 생긴다 — 5인 규모에선 손으로 붙여넣는 게 자격증명 수가 적다
- ~~AWS 쪽 도구 서브넷 `t4g.nano` subnet router = Terraform 트랙~~ → ⟳ **C-55 로 미채택**(2026-08-11).
  SSM(C-37)이 그 역할을 이미 덮는다. **만들지 말 것** — 안 나올 EC2 를 1대 만들게 된다
- eks 오버레이 = 미착수(`overlays/eks/kustomization.yaml` 의 목록이 해야 할 일).
  ⟳ 그 목록에서 **subnet router 항목은 빠졌다**(C-55) — AWS 쪽은 **Operator 단독**이다
- 🔴 C-55 Step 2(이관 후 Tailscale 직접 스트리밍)를 붙일 때 이 트랙에서 할 일:
  ① ACL 의 `tag:mp-pg → tag:mp-pg` deny 단정을 **의도적으로** 걷는다
  ② AWS 쪽 `data/pg-rw` 를 Operator 로 노출하고 온프렘은 `aws-pg` Service 로 받는다
     (🟢 CNPG 는 Tailscale 을 모른다 — `connectionParameters.host: aws-pg` 한 줄)
  ③ 🔴 진짜 작업량은 **인증서**다 — CNPG 복제는 비밀번호가 아니라 클라이언트 인증서를 쓴다.
     AWS 의 `pg-ca`·`pg-replication` 을 복사해 `externalClusters[].sslRootCert/sslCert/sslKey` 로
     참조해야 하고, C-23(양 사이트 독립·PushSecret 미채택)이라 **수동**이며 만료되면 다시 해야 한다
