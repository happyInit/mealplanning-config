# netpol 사이트 분기 — 적용 순서와 검증 게이트 (0-17 · 0-18)

> 신설 2026-08-10. 브랜치 `feat/eks-netpol`. 근거 = 앱 레포 `docs/mp_aws_prep_checklist.md`
> **0-17**(egress 무제한 11 워크로드) · **0-18**(netpol 재작성) · 이슈 #549 · 감사 #53 #57 #58.
>
> 🔴 **이 브랜치는 온프렘 라이브를 바꾸지 않는다.** 5개 정책 트랙 전부
> `kubectl kustomize <트랙>/overlays/onprem` 렌더가 **바이트 단위로 이전과 동일**함을 확인했다.
> 바뀐 것은 ① base 가 사이트 중립이 된 것 ② `overlays/eks` 가 처음으로 내용을 갖게 된 것,
> 두 가지뿐이다.

---

## 0. 한 장 요약

| | 온프렘 | EKS |
|---|---|---|
| LAN `192.168.0.0/24` ipBlock **7건** | base → `overlays/onprem` 으로 **자리만 이동**(값·위치 동일) | **복원 안 함.** 6건은 `fromEntities: host` CNP 로 대체, 1건(observability)은 삭제 |
| 내부 GW `ingress: [- {}]` | 그대로(2026-08-03 사고 원복분) | **포트 443·80 으로 절단** |
| 와일드카드 `*.kurly.com` | 그대로(크롤 = 온프렘 프로덕션) | 크롤 정책 3종째 **삭제** |
| 와일드카드 `*.argotunnel.com` | 그대로(터널 = 온프렘 DR) | cloudflared 정책 2종째 **삭제** |
| Kafka egress(브로커·EO·exporter) | 없음(현행 유지) | **화이트리스트 신설** |
| observability egress 8종 | 없음(현행 유지) | **`world` 제거**(= 인터넷·IMDS 차단) |
| IMDS | 해당 없음 | ns 5곳에 **`egressDeny` 하한선** |

**0-17 이 세는 "무제한 11" 의 실물**(2026-08-10 실측):

- data 3 — `kafka-combined-0..2`(브로커·KRaft 컨트롤러 겸용) · `kafka-entity-operator` · `kafka-kafka-exporter`
  → `base/kustomization.yaml` 이 *"Kafka 는 Strimzi 소유라 제외"* 로 비워 둔 자리다.
- observability 8 — `prometheus` · `alertmanager` · `grafana` · `kube-state-metrics` ·
  `kube-prometheus-stack-operator` · `loki` · `tempo` · `mp-gw-internal-istio`
  → `netpol-observability-baseline.yaml` 이 policyTypes 에 **Ingress 만** 넣었기 때문이다(주석에 명시).
  MinIO 만 예외로 이미 잠겨 있었고, 그건 C-18 로 EKS 에선 아예 사라진다.

---

## 1. 🔴 먼저 알아야 할 것 — LAN ipBlock 7건은 **원래 하던 일을 하고 있지 않다**

0-18 이 "VPC CIDR 로 기계적 치환 금지" 라고 못 박은 이유가 여기 있다.

7건 중 6건은 주석이 전부 *"kubelet probe 는 노드 IP 에서 오므로 노드 서브넷을 연다"* 라고 적고 있다.
**그런데 Cilium 에서 그 규칙은 노드 트래픽에 걸리지 않는다.** 실측:

```bash
kubectl -n kube-system get cm cilium-config -o jsonpath='{.data.policy-cidr-match-mode}'
# → (빈 값)
```

`policy-cidr-match-mode` 가 비어 있으면 CIDR 셀렉터는 **`world` 신원에만** 매칭된다.
노드는 `host`/`remote-node`, apiserver 는 `kube-apiserver` 라는 **예약 신원**을 갖고 있어
CIDR 규칙에 안 걸린다. probe 가 실제로 통과하는 이유는 Cilium 의 `allow-localhost: auto`(기본값)다.

→ 즉 그 6건의 **실효는 "kubelet 통과"가 아니라
"LAN 의 임의 장비(= `world` 중 그 대역) → 이 파드, 포트 무제한"** 이다.

팀은 이미 같은 사실을 한 번 비싸게 배웠다 —
`platform/policies-observability/base/netpol-observability-entities.yaml` 머리말이
2026-08-03 내부 도구 전면 장애의 원인으로 정확히 이걸 적고 있다.

**결론 두 가지**

1. EKS 에서 `10.10.0.0/16` 으로 바꿔 쓰면 **얻는 것 0, 잃는 것은 VPC 안 임의 리소스에 대한 전 포트 개방**이다.
   그래서 `fromEntities: host` 로 다시 썼다.
2. 🔴 **온프렘도 같은 상태다** — 이 브랜치는 온프렘을 안 건드리지만, 걷어내는 것은
   별도 작업으로 남는다(아래 §8).

---

## 2. 적용 순서

### 2-A. 온프렘 (= 이 브랜치를 머지할 때)

정책 트랙 5개는 전부 **manual sync** 다(`mp-policies{,-data,-ingress,-observability,-pipeline}`).
머지만으로는 아무 일도 일어나지 않는다.

```bash
# 렌더가 같으므로 sync 해도 리소스 변화가 0 이어야 한다
for a in mp-policies mp-policies-data mp-policies-ingress mp-policies-observability mp-policies-pipeline; do
  kubectl -n argocd get application $a -o custom-columns=NAME:.metadata.name,PATH:.spec.source.path,SYNC:.status.sync.status --no-headers
done
```

⚠️ **sync 를 서두를 필요가 없다.** 이 브랜치의 온프렘 산출물은 "같은 것을 다른 파일에서 읽는다" 가
전부다. `Synced` 인 채로 두었다가 다음 정책 변경 때 함께 나가도 된다.

### 2-B. EKS (신규 클러스터)

🔴 **순서를 지켜야 한다.** app ns 에서 배운 규칙과 같다 —
**허용 정책 먼저, default-deny 마지막.** 여기서는 "default-deny 를 켜는 정책"이 어느 것인지가
파일 이름에 안 드러나므로 아래 표를 본다.

| 단계 | 적용 | default-deny 를 켜나 | 안 하면 |
|---|---|---|---|
| ① | `platform/policies-*/overlays/eks` 의 **IMDS deny 5종** | ❌ (`enableDefaultDeny: false`) | — 단독으로 안전. 제일 먼저 넣어도 된다 |
| ② | `netpol-node-probe.yaml`(app·data) | ❌ (data 는 `ingress: false`, app 은 이미 잠긴 ns) | — |
| ③ | `netpol-kafka-egress.yaml` | 🔴 **켠다**(egress) | Kafka 가 조용히 끊긴다 → §4 게이트 필수 |
| ④ | `netpol-observability-egress.yaml` | 🔴 **켠다**(egress) | 스크레이프·알림·S3 가 조용히 끊긴다 → §5·§6 |
| ⑤ | base 의 기존 정책 묶음 | 기존과 동일 | — |

⚠️ ③·④ 는 **한 번에 sync 하지 말 것.** observability 를 먼저 잠그면
"죽은 것을 알아챌 수단"이 같이 죽는다 — base kustomization 이 이미 같은 경고를 적어 뒀다.
**③(Kafka) → 관찰 → ④(observability)** 순으로 간다.

---

## 3. 게이트 — app ns probe 포트 (0-18)

`overlays/eks/netpol-node-probe.yaml` 은 app ns 의 probe 를 **`:15021` 로만** 받는다.
전제 = "app ns 워크로드는 전부 사이드카가 있고 probe 가 rewrite 된다".

```bash
# 사이드카 없는 워크로드가 하나라도 있으면 그 파드는 원래 포트로 probe 를 받는다
kubectl -n app get pods -o json | python3 -c '
import json,sys
for p in json.load(sys.stdin)["items"]:
    names=[c["name"] for c in p["spec"]["containers"]]
    if "istio-proxy" not in names: print("NO SIDECAR:", p["metadata"]["name"], names)
'
```

출력이 비어야 통과. 하나라도 나오면 그 워크로드를 `netpol-node-probe.yaml` 의 셀렉터에서 빼고
포트 무제한으로 따로 열거나(data ns 판과 같은 형태), 사이드카 주입을 고친다.

🔴 실패 증상은 **에러가 아니라 NotReady** 다. 파드가 Service 엔드포인트에서 빠지면서
"배포는 성공했는데 트래픽이 안 간다" 로 나타난다.

---

## 4. 게이트 — Kafka egress (0-17)

`netpol-kafka-egress.yaml` 은 세 워크로드의 egress 를 화이트리스트로 바꾼다.
목록이 틀리면 **RST 가 아니라 드롭**이라 로그가 안 남는다.

**적용 전** — 실제로 나가는 곳을 먼저 센다:

```bash
kubectl -n kube-system exec ds/cilium -c cilium-agent -- \
  hubble observe --from-namespace data --to-namespace data --last 500 \
  --label strimzi.io/cluster=kafka -o compact
# 그리고 클러스터 밖으로 나가는 것이 정말 0인지
kubectl -n kube-system exec ds/cilium -c cilium-agent -- \
  hubble observe --from-namespace data --to-identity 2 --last 200
```

⚠️ **관측창이 짧으면 못 본다.** Kafka 는 상주 커넥션이라 브로커 간 9090/9091 은
재시작·리더 선출 때만 새로 뜬다. app ns 때 PG·ES 를 Hubble 로 뽑았다가 5432·9200 을
통째로 놓칠 뻔한 것과 **같은 함정**이다 — 그래서 이 파일의 포트는 관측이 아니라
**Strimzi 리스너 규약**에서 뽑았다.

**적용 후** — 30분 관찰:

```bash
kubectl -n kube-system exec ds/cilium -c cilium-agent -- \
  hubble observe --from-namespace data --verdict DROPPED --last 200
kubectl -n data get kafka kafka -o jsonpath='{.status.conditions[*].type}'   # Ready 유지
kubectl -n data get kafkatopics                                              # Ready 유지 = EO 살아 있음
```

🔴 **가장 비싼 누락은 `9090`(KRaft 컨트롤러)** 이다. 빠지면 정족수가 깨져 클러스터가 통째로
unavailable 이 되고, 증상이 "브로커는 Running 인데 프로듀스가 전부 타임아웃" 이라 원인이 안 보인다.

---

## 5. 게이트 — DNS L7 병합 (0-17, observability)

`netpol-observability-egress.yaml` 은 alertmanager·loki·tempo 를 `toEntities: cluster` 그룹에서
**일부러 뺐다.** 근거 = "L7 없는 허용과 L7 있는 허용이 같은 목적지에 겹치면 느슨한 쪽이 이긴다"
→ `toEntities: cluster` 가 kube-dns:53 을 L7 없이 열어 버리면 DNS 프록시가 안 붙고
`toFQDNs` 가 **영원히 IP 를 학습하지 못한다.**

🔴 **이건 문서 기반 판단이다. 실증하고 넘어갈 것.**

```bash
# 1) toFQDNs 를 쓰는 파드가 실제로 IP 를 학습했는지
kubectl -n kube-system exec ds/cilium -c cilium-agent -- \
  cilium-dbg fqdn cache list | grep -E 'slack|amazonaws'
# 2) 그 엔드포인트에 DNS L7 프록시가 붙었는지
kubectl -n kube-system exec ds/cilium -c cilium-agent -- \
  cilium-dbg endpoint list | grep -i observability
```

캐시가 비어 있으면 병합 함정이 실재한다는 뜻이다. 그때도 고치는 방법은 같다 —
**FQDN 쓰는 워크로드는 넓은 `toEntities` 규칙에서 뺀다.**

반대로 병합이 문제가 아닌 것으로 판명되면, alertmanager/loki/tempo 도
`toEntities: [cluster]` 를 얹어 in-cluster 경로를 단순화할 수 있다(선택).

---

## 6. 게이트 — Alertmanager 수신자 (0-17)

🔴 **이 레포에 수신자 정본이 없다.** kube-prometheus-stack 의 alertmanager 설정은
Helm values(**앱 레포 Ansible**)가 만든 Secret 이고, `AlertmanagerConfig` CR 은 0개다(실측 2026-08-10).
이 브랜치는 **Slack 하나만** 열어 뒀다.

```bash
# 어떤 수신자가 설정돼 있는지 (값이 아니라 키 이름만 본다 — 시크릿 덤프 금지)
kubectl -n observability get secret alertmanager-kube-prometheus-stack-alertmanager \
  -o go-template='{{range $k,$v := .data}}{{$k}}{{"\n"}}{{end}}'
# 라우팅 트리는 UI 로 (내부 GW 경유)
#   https://alertmanager.<도메인>/#/status  → Config 섹션
```

SMTP·PagerDuty·범용 webhook 이 하나라도 있으면 `netpol-observability-egress.yaml` 의
alertmanager 정책에 목적지를 추가해야 한다. **안 하면 알림이 조용히 사라진다** —
관측 경로의 고장은 알람이 안 울리는 형태로 온다.

---

## 7. 롤백

전부 **정책 오브젝트 추가/수정**이라 롤백이 단순하다.

```bash
# 가장 급한 복구: 잠금을 켠 정책만 지운다 (base 의 기존 정책은 그대로 산다)
kubectl -n data delete cnp mp-kafka-broker-egress mp-kafka-entity-operator-egress mp-kafka-exporter-egress
kubectl -n observability delete cnp \
  mp-observability-egress-core mp-observability-egress-operator \
  mp-observability-egress-gw-internal mp-observability-egress-alertmanager \
  mp-observability-egress-loki mp-observability-egress-tempo
```

⚠️ ArgoCD 가 되돌려 놓으므로, 원인을 고치기 전에는 해당 Application 을 수동 sync 하지 말 것
(정책 트랙 5개는 전부 manual sync 라 자동으로 되살아나지는 않는다).

🔴 **IMDS deny 는 롤백 대상이 아니다.** `enableDefaultDeny: false` 라 다른 것을 끊지 않는다.
이걸 지우고 싶어지면, 진짜 원인은 다른 곳에 있다.

---

## 8. 남은 일 — 온프렘 승격 (별건)

이 브랜치가 **일부러 안 한 것**들이다. 전부 라이브 검증이 필요해 한 브랜치에 못 담는다.

1. **온프렘의 LAN ipBlock 6건 걷어내기** (§1). 지금 그 규칙들이 여는 것은
   "LAN 임의 장비 → frontend·backend·pg·es·redis, 포트 무제한" 이다.
   걷어내기 전에 `cilium-dbg bpf ct list global` 로 **그 대역에서 실제로 들어오는 것이 있는지**
   전수 확인할 것 — #532② 가 쓴 방법 그대로다. probe 는 `host` 라 영향이 없어야 하지만,
   *"관측 창에 없던 :80 리다이렉트를 놓친 적이 있다"* 는 교훈대로 **트래픽을 직접 만들어 재측정**한다.
2. **온프렘 Kafka·observability egress 잠금 승격.** EKS 판이 검증되면
   `overlays/eks/` 에서 `base/` 로 올리고, 온프렘 전용 차이(MinIO·호스트 C 스크레이프)만
   `overlays/onprem` 에 남긴다.
3. **온프렘 내부 GW 좁히기.** `ingress: [- {}]` → 최소 포트 절단(443·80)은 EKS 판과 같은 논리가
   온프렘에도 성립한다. 다만 MetalLB SNAT 가 살아 있으므로 **포트만** 자르고 소스는 건드리지 않는다.
4. **`*.kurly.com` 열거화.** 온프렘에 남는 유일한 진짜 와일드카드다.
   관측된 서브도메인을 주기적으로 뽑아 `matchName` 목록으로 바꾸는 작업.

## 9. 이 레포 밖에서 같이 가야 하는 것

| 항목 | 어디 | 왜 여기 적나 |
|---|---|---|
| **IMDSv2 강제 + hop limit 1** | Terraform 노드그룹 `metadata_options` | netpol 은 2차 방어선이다. 1차가 없으면 Karpenter NodePool(C-29)이 새 노드를 띄울 때 구멍이 난다 |
| Loki·Tempo 의 **MinIO 엔드포인트 → S3** | `platform/argocd/{loki,tempo}.yaml` 인라인 `valuesObject` (0-4 트랙) | 여기만 고치면 "정책은 S3 를 여는데 앱은 MinIO 로 간다". 한 묶음으로 나가야 한다 |
| 크롤 워크로드 EKS 제외 | `pipelines` 트랙 | 정책만 빼고 파드가 남으면 그 파드는 egress default-deny 에 걸려 조용히 죽는다 |
| 공개 GW Service `externalTrafficPolicy` | `gateway` 트랙 | `Local` 이면 `policies-ingress/overlays/eks` 의 `remote-node` 규칙을 지울 수 있다(§ kustomization 주석) |
| Redis 클라이언트 Sentinel 설정 제거 | 앱 설정(`common/`·서비스 오버레이) | C-14 로 26379 가 사라진다. netpol 만 바꾸면 클라이언트가 Sentinel 을 찾다 실패한다 |
