# monitoring — 사이트 분기 상태와 **앱 레포에 필요한 변경 명세**

> 신설 2026-08-10. 근거 = 앱 레포 `docs/mp_aws_prep_checklist.md` **0-3b · 0-3c · 0-26 · 1-27**.
> 구조 정본 = `SITES.md`. 이관 결정(C-1~C-31) 정본 = 그 체크리스트.
>
> **이 문서가 여기 있는 이유** — 관측의 사이트 분기는 **config 레포 안에서 끝나지 않는다.**
> 알람 규칙(PrometheusRule)만 여기 있고, **Prometheus 자신·스크레이프 job·Alertmanager·
> Grafana 데이터소스는 kube-prometheus-stack values = 앱 레포 Ansible** 소관이다.
> 🔴 앱 레포는 PR 리뷰 필수라 여기서 고칠 수 없다. 그래서 **명세만 남긴다.**

---

## 0. 지금 상태 한 장

| 항목 | 정본 위치 | 사이트 분기 | 상태 |
|---|---|---|---|
| PrometheusRule 10종 | config `monitoring/base` | kustomize overlay | ✅ 0-3c 완료(아래 §1) |
| Grafana 대시보드 13장 | config `monitoring/base/dashboards` | 없음 | ⚪ 불필요(§5-D) |
| **Prometheus CR**(externalLabels·retention·nodeSelector) | **앱 레포 Ansible** | 없음 | 🔴 §2 · §3 |
| **스크레이프 job 4종** | **앱 레포 Ansible** | 없음 | 🔴 §4 |
| **Alertmanager 라우팅** | **앱 레포 Ansible** | 없음 | 🔴 §2 부작용 |
| **Loki·Tempo·Alloy Application** | **이중 소유**(config + Ansible) | config 만 | 🔴 §6 (0-26) |
| **Grafana 데이터소스 CM** | **앱 레포 Ansible 단독** | 없음 | 🔴 §6-③ |

---

## 1. ✅ 0-3c — `mp-physical-layer` 를 온프렘 전용으로 내렸다 (config 레포, 완료)

`monitoring/base/rules-physical.yaml` → **`monitoring/overlays/onprem/rules-physical.yaml`**.

- **근거는 도달성이 아니라 장애 도메인이다.** 9룰의 타깃 = Proxmox 물리 2대(`job=hypervisor`) +
  호스트 C(`job=vm-node`·`vm-cadvisor`). AWS 에 **대응물이 없다** — 이관해 봐야 감시할 것이 없고,
  base 에 두면 EKS 오버레이가 상속해 **영구 TargetDown** 이 된다.
- 반대로 **지우면 안 된다** — C-3 으로 온프렘은 DR 대기 + 크롤 프로덕션으로 계속 살아 있고,
  15일 내 실발화가 4건(TempCritical·TempHigh·DiskReadBurst·MpHostCDown) 있었다.
- **검증**: `kubectl kustomize monitoring/overlays/onprem` 이 이동 전후로 **sha256 동일**
  (`c95cade3ab8e9fda…`, 문서 12개). eks 는 `mp-physical-layer` 가 **0건**으로 빠진다.

추가로 EKS 오버레이에서 **`mp-data-tier` 의 `mp-minio` 그룹을 제거**했다(C-18 — AWS 에 MinIO 없음).
`MpMinIOVolumeMetricsUnavailable` 이 `absent_over_time()` 기반이라 **대상이 없으면 발화**한다 —
0-3c 와 정확히 같은 실패 유형이다. 상세·가드는 `overlays/eks/kustomization.yaml` 주석.

---

## 2. 🔴 0-3b — `externalLabels: {site: …}` (앱 레포)

**실측(2026-08-10, `observability/kube-prometheus-stack-prometheus`)**: `externalLabels` **키 부재** ·
`remoteWrite` 부재 · `replicas: 1` · `retention: 15d` · `retentionSize` **부재** ·
`nodeSelector: {topology.kubernetes.io/zone: host-b}`.

### 명세 — `roles/k8s_observability/`

```yaml
# defaults/main.yml  (신규 변수)
observability_site: onprem        # EKS 인벤토리에서 aws 로 덮어쓴다

# templates/kube-prometheus-stack-values.yaml.j2  — prometheusSpec 아래, retention 옆
    externalLabels:
      site: "{{ observability_site }}"
```

`alertmanager.config` 쪽은 **동시에 결정해야 한다**(아래 부작용 ②).

### 🔴 체크리스트 서술 정정 — "그 전 데이터는 site 없이 남아 영원히 섞인다"는 **부정확하다**

Prometheus 의 `external_labels` 는 **로컬 TSDB 에 저장되지 않는다.** 외부와 말할 때
(remote_write · federation · **Alertmanager 로 보내는 알림**) 붙는 라벨이다.
지금 `remoteWrite` 도 부재이므로, **지금 이 값이 실제로 작용하는 곳은 알림 경로 하나뿐**이다.

그래도 **선행이라는 결론은 그대로 맞다.** 다만 진짜 이유는 이것이다:

1. **두 사이트가 같은 Slack 채널로 알린다** — `site` 가 없으면 `MpTempoDown` 이 왔을 때
   *어느 클러스터인지* 메시지만 보고 알 수 없다. 온콜이 잘못된 클러스터를 파는 사고로 직결된다.
2. **알림 신원(라벨 집합)이 바뀌는 변경**이라 나중에 넣을수록 비싸다 — 라벨이 하나 늘면
   Alertmanager 는 그것을 **새 알림**으로 본다. 이관 당일에 넣으면 **그날 firing 중이던 알림이
   전부 한 번씩 재통보**된다(가장 시끄러운 날에 소음을 얹는다).
3. 훗날 remote_write·federation·Thanos 로 갈 때 **소급이 불가능하다.**

### 부작용 — 같이 결정할 것 (사람)

① **일회성 재통보** — 적용 순간 firing 중이던 알림이 새 알림으로 다시 나간다. 조용한 시간대 권장.
② **`group_by` 를 손댈 것인가** — 현재 `group_by: ['alertname','service']`.
   두 사이트가 **같은** Alertmanager 를 보게 되면 같은 alertname 이 **한 메시지로 묶인다.**
   사이트별로 끊으려면 `group_by: ['alertname','service','site']`.
   🔴 C-22 는 "양 사이트 자체 유지"라 Alertmanager 도 각자다 → **당장은 불필요**하지만,
   같은 Slack 채널을 쓰면 사람 눈에는 섞여 보인다. **결정 필요.**
③ **`site` 값 표기** — `aws` 인가 `eks` 인가. 체크리스트는 `onprem|aws` 로 적었다(C-22).
   대시보드 변수·런북이 문자열로 물게 되므로 **한 번 정하면 바꾸기 비싸다.**

---

## 3. 🔴 1-27 — `retentionSize` (앱 레포)

**실측(2026-08-10, kubelet volume stats)**: `prometheus-…-db` **8.10 GiB used / 29.36 GiB cap**.
클러스터 나이 13일 · `retention: 15d` 라 **아직 첫 만료가 오지 않았다** — 즉 8.10 GiB 는
15일 정상상태의 **하한**이다. 일 증가 ≈ 0.62 GiB → 15일 ≈ **9.4 GiB**.
체크리스트의 "12일 최대 시계열 220,683(현재 대비 +28%)" 을 얹으면 최악 ≈ **12 GiB**.

C-16 이 PVC 를 **30 → 20 Gi** 로 줄이므로(사용 가능 ≈ 19.5 GiB) 상한이 필요하다.

```yaml
# templates/kube-prometheus-stack-values.yaml.j2 — retention 바로 아래
    retentionSize: {{ prometheus_retention_size }}
# defaults/main.yml
prometheus_retention_size: "14GiB"   # 20Gi PVC 의 ~72%
```

- 🔴 **`retention` 을 지우지 말 것.** 둘은 OR 이다 — 먼저 걸리는 쪽이 블록을 지운다.
  시간 상한이 없으면 카디널리티가 낮은 시기에 **몇 달치가 쌓여** 쿼리가 느려진다.
- 🔴 **`retentionSize` 는 WAL·head 를 포함하지 않는 "블록" 기준으로 동작하고, 삭제는
  블록 단위라 오버슈트가 있다.** PVC 를 꽉 채운 값을 넣으면 안 된다 — 20Gi 에 `19GiB` 는 금물.
- ⚠️ **온프렘·AWS 를 같은 값으로 둘 이유가 없다** — 온프렘은 DR 대기라 부하가 낮다.
  변수로 뽑았으므로 사이트별로 다르게 줄 수 있다. **값 확정은 사람 결정.**

---

## 4. 🔴 0-3c 의 나머지 — 스크레이프 job 4종 (앱 레포)

`templates/kube-prometheus-stack-values.yaml.j2` 의 `additionalScrapeConfigs` 에
`hypervisor` · `vm-node` · `vm-cadvisor` · `vm-alloy` 4개가 **무조건** 들어간다.
룰은 config 레포에서 갈랐지만(§1) **타깃은 아직 안 갈렸다** — EKS 에 그대로 나가면
알람은 안 울려도 **TargetDown(스택 빌트인)이 4건 영구 발화**한다.

```yaml
# 명세: 블록 전체를 사이트 조건으로 감싼다
    additionalScrapeConfigs:
{% raw %}{% if observability_site == 'onprem' %}{% endraw %}
      # ... 기존 job 4종 그대로 ...
{% raw %}{% endif %}{% endraw %}
```

🔴 **`groups['hypervisor']` · `hostvars['fb-ci-harbor']` 참조가 EKS 인벤토리에 없으면
템플릿 렌더 자체가 죽는다** — `| default([])` 가 있는 hypervisor 쪽은 버티지만
`hostvars['fb-ci-harbor']` 는 그대로 예외를 던진다. 조건으로 감싸는 것이 **필수**이지 선택이 아니다.

### 같은 파일의 나머지 온프렘 결합 값

| 값 | 지금 | EKS 에서 |
|---|---|---|
| `prometheusSpec.nodeSelector` | `topology.kubernetes.io/zone: host-b` | **제거**(0-5). AZ 는 EBS 토폴로지가 정한다 |
| `storageSpec…storageClassName` | `openebs-lvm` | `gp3`(0-8) |
| `grafana.nodeSelector`(라인 140 부근) | 확인 필요 | 같은 취급 |
| `prometheusSpec.replicas` | 1 | 잠정 1 — **D8-r 로 이관 전 재결정**(C-22) |

---

## 5. EKS 에서 **아직 못 고친** 관측 결함 (config 레포 · 사람 결정)

### C. 🔴 `PrometheusRule/mp-workload-spread` — EKS 에서 **오발화한다**

zone 을 노드 **이름 정규식**으로 파생한다(`k8s-worker-([ab])[0-9]+` · `k8s-master`).
EKS 노드 이름은 `ip-10-…` 이라 **하나도 안 맞는다**:

- `MpNodeZoneMapUnknown` → **전 노드에서 영구 발화**
- zone 라벨이 전부 빈 값으로 접혀 `mp:deployment_single_zone:bool` 이 항상 1 →
  `MpDeploymentPodsOnSingleZone` 이 **replica 2 이상인 전 Deployment 에서 오발화**

이건 0-3c 와 **같은 실패 유형인데 체크리스트에 항목이 없다.**
고칠 방향은 룰 주석이 이미 적어 뒀다 —
KSM 에 `--metric-labels-allowlist=nodes=[topology.kubernetes.io/zone]` 를 켜고
`kube_node_labels` 조인으로 교체. **KSM 설정 = 앱 레포**라 config 레포 단독으로는 못 끝낸다.

🔴 **EKS 에서는 이 교체가 오히려 쉽다** — 온프렘에서 이름 규칙을 쓴 이유가 "KSM 이
`kube_node_labels` 를 안 내보낸다"였는데, EKS 노드는 `topology.kubernetes.io/zone` 에
**진짜 AZ**(`ap-northeast-2a`…)가 붙어 있어 파생이 아니라 **실값**이 된다.
→ **선택지**: ⑴ KSM allowlist 를 켜고 룰을 조인식으로 재작성(양 사이트 공통·정답) /
⑵ eks 오버레이에서 이 PrometheusRule 을 통째로 제거(AZ 분산 감시를 AWS 에서 포기) /
⑶ eks 전용 룰을 따로 씀. **사람 결정.**

### D. `08-hypervisor.json` 대시보드 — 그대로 뒀다

EKS 에서 빈 패널이 되지만 **알람이 아니라 소음이 0** 이다.
빼려면 `configMapGenerator` 를 갈라야 하고 그러면 **onprem 렌더가 바뀐다**(0-1 안전기준 위반).
정리는 별건.

### E. `rules-backup.yaml` 의 런북 문자열

`ssh ubuntu@192.168.0.17` · `192.168.0.10` 이 `annotations.description` 에 박혀 있다.
`annotations` 는 발화·그룹핑·라우팅에 참여하지 않아 **오발화를 만들지 않는다** → 0-3c 대상이 아니다.
다만 AWS 온콜이 존재하지 않는 호스트로 안내받는다. 문구 정리는 별건.

---

## 6. 🔴 0-26 — `lgtm-apps.yaml.j2` 이중 소유 (앱 레포)

### 실측 (2026-08-10)

`roles/k8s_platform_apps/tasks/main.yml:41-63` 이 `lgtm-apps.yaml.j2` 를 **`kubectl apply`** 한다.
그 템플릿이 만드는 오브젝트 = **Application 3개**(`loki`·`tempo`·`alloy`) + **ConfigMap 1개**.
🔴 그런데 **Application 3개 다 config 레포 `platform/argocd/` 에도 있고** 라이브는
`platform-root`(`selfHeal: true`) 소유다. **체크리스트는 "loki·tempo" 2개라고 적었는데 `alloy` 도 포함해 3개다.**

두 소스는 **이미 갈라져 있다** (Ansible 템플릿 렌더 ↔ config 레포 파일, 잎노드 전수 비교):

| 오브젝트 | Ansible 템플릿 (구값) | config 레포 = 라이브 | 되돌아가면 |
|---|---|---|---|
| `loki` | `nodeSelector: zone=host-b` | `nodeSelector: hostname=k8s-worker-b1` | 볼륨이 b2 로 몰려 데이터 티어가 자리를 못 잡던 상태로 회귀(2026-07-31 사건) |
| `loki` | s3 endpoint **4-dot FQDN** | `.svc` 단축형 | 🔴 **DNS 하이재킹 재발**(실측 21.7%) |
| `tempo` | s3 endpoint **4-dot FQDN** | `.svc` 단축형 | 🔴 동일. tempo 는 store init 실패 → `exit 1`(2026-08-02 사건) |
| `tempo` | liveness/readinessProbe **없음** | probe 7+8 필드 | 기동 지연 시 크래시루프 복귀 |
| `alloy` | `resources.limits.memory: 256Mi` | `512Mi` | OOMKill |
| `alloy` | loki push URL **4-dot FQDN** | `.svc` 단축형 | 🔴 **평문 HTTP 로 전 클러스터 로그를 공인 IP 로** 보낼 수 있다 |

즉 **0-26 은 "정리 항목"이 아니라 무장된 지뢰다.** `ansible-playbook … --tags <이 롤>` 한 번이
**해결된 사고 3건을 동시에 되돌리고**, ArgoCD selfHeal 이 되돌리는 **왕복(flap)** 이 생긴다.
S3 컷오버(C-18)를 config 에서 하면 여기에 MinIO endpoint 복원까지 얹힌다.

### 명세 — 권고안

1. **`lgtm-apps.yaml.j2` 에서 Application 3개를 삭제**하고, 템플릿·apply 태스크를
   **ConfigMap `lgtm-grafana-datasources` 전용**으로 축소한다(파일명도 `lgtm-datasources.yaml.j2` 로).
   → Application 정본은 **config 레포 `platform/argocd/{loki,tempo,alloy}.yaml` 단독**.
2. **`Synced`·`Healthy` 대기 태스크는 남긴다.** 만드는 주체가 ArgoCD 로 바뀔 뿐 "떴는지 확인"은
   여전히 부트스트랩의 관문이다. 🔴 다만 **`k8s_argocd`(platform-root) 뒤라는 순서 전제가
   이제 진짜 의존이 된다** — 지금은 Ansible 이 직접 만들어서 순서가 어긋나도 떴다.
3. 🔴 **롤을 지우지 말 것** — `lgtm-minio-creds`·`minio` 시크릿의 **유일한 공급원**이다
   (CLAUDE.md 에도 명시). 이번 변경은 **Application 소유권만** 옮긴다.
4. 🔴 **적용 순서** — ① 앱 레포 PR 머지·적용 → ② 라이브 3개가 여전히 `Synced/Healthy` 확인 →
   ③ 그 다음에야 C-18(S3 컷오버)을 config 레포에서 한다. 순서를 뒤집으면 왕복이 그대로 난다.

### ③ 🔴 `lgtm-grafana-datasources` — 별건이지만 같은 뿌리

이 CM 은 **config 레포에 없다**(Ansible 단독). 그래서 config 레포의 `check_fqdn` 린트가
닿지 않고, **라이브 값이 아직 4-dot FQDN 이다**:

```
# 실측 2026-08-10, observability/lgtm-grafana-datasources
#   url: http://loki.observability.svc.cluster.local:3100
#   url: http://tempo.observability.svc.cluster.local:3200
```

같은 레포가 alloy 주석에 *"4-dot 금지 — 실측 21.7% 하이재킹"* 이라고 못박아 두고,
**Grafana 가 Loki·Tempo 를 부르는 바로 그 URL 은 4-dot 인 채로 남아 있다.**
→ 최소 조치 = 위 CM 의 두 URL 을 `.svc` 단축형으로. **이관과 무관하게 지금 고칠 값이다.**
(config 레포로 옮기는 것은 별건 — 옮기면 `monitoring/base` 의 `namespace: app` 때문에
네임스페이스가 갈려 온프렘 렌더가 바뀐다. 옮길 거면 전용 트랙이 필요하다.)

---

## 7. 검증 방법

```bash
# ① onprem 은 안 바뀌어야 한다 (0-3c 의 안전기준)
kubectl kustomize monitoring/overlays/onprem | sha256sum      # 이동 전후 동일

# ② eks 에서 온프렘 전용이 빠졌나
kubectl kustomize monitoring/overlays/eks | grep -c mp-physical-layer   # 0
kubectl kustomize monitoring/overlays/eks | grep -c 'name: mp-minio'    # 0

# ③ 골격이 안 썩었나
python3 scripts/validate.py        # check_site_overlays() 가 eks 렌더 실패를 잡는다
```
