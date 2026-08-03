#!/usr/bin/env python3
"""config 레포 검증 — 병합 전에 "git 에는 있는데 클러스터엔 없는" 부류를 잡는다.

왜 있는가
---------
이 레포에는 CI 가 없었고 13개 Application 이 automated sync 다. 즉 잘못된 매니페스트가
아무 관문 없이 클러스터에 도달했다. 2026-08-02 감사에서 실제로 그렇게 새어나간 것 3부류:

  1. pipelines/kustomization.yaml 의 JSON-Patch `op: add` 가 merge 가 아니라 **replace** 라
     base 에 선언된 runAsNonRoot·readOnlyRootFilesystem 이 렌더 단계에서 증발했다.
     git 만 읽으면 7개 워크로드가 하드닝된 것으로 보이지만 라이브는 uid=0(root) 였다.
  2. 4-dot FQDN(`<svc>.<ns>.svc.cluster.local`)이 파드 search 의 `local` 때문에 ISP 로 새어
     공인 IP 로 해석됐다(실측 21.7%). tempo 가 닷새간 420회 재시작한 근인.
  3. topologySpreadConstraints 의 patchMergeKey 가 topologyKey 라, 같은 축 항목이 둘이면
     병합이 깨져 제약 하나가 조용히 사라진다(API 검증은 통과한다).

셋 다 **적용은 성공했고 아무 에러도 없었다.** 그래서 이 스크립트는 "문법이 맞나"가 아니라
"렌더 결과가 의도대로인가"를 본다.

쓰는 법
-------
  python3 scripts/validate.py            # 전체
  python3 scripts/validate.py --list     # 위반 목록만 (베이스라인 갱신용)

의존성: python3 + PyYAML. 렌더러는 `kustomize` 가 있으면 그걸, 없으면 `kubectl kustomize`.
kubeconform 은 있으면 쓰고 없으면 건너뛴다(경고).
"""
from __future__ import annotations

import argparse
import pathlib
import re
import shutil
import subprocess
import sys
from collections import defaultdict

import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
BASELINE = REPO / "scripts" / "policy-baseline.txt"

# 실제로 배포되는 것만 본다. base/ 와 overlays/eks 는 ArgoCD 가 안 쓴다(온프렘 오버레이만 쓴다).
SKIP_KUSTOMIZE_RE = re.compile(r"/(base|overlays/eks)$")

# kustomization 없이 매니페스트를 그대로 두는 디렉터리형 ArgoCD 앱
DIRECTORY_APPS = [
    "argocd/applications", "pipelines/jobs", "platform/argocd", "platform/es",
    "platform/kafka", "platform/pg", "platform/pgsync", "platform/pooler", "platform/redis",
]

WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"}

# 이 스크립트 자신과 문서는 FQDN 린트 대상에서 제외한다(설명하려면 그 문자열을 써야 한다).
FQDN_LINT_EXCLUDE = re.compile(r"^(scripts/|README\.md$|\.github/)")


class Result:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.warnings: list[str] = []

    def fail(self, msg: str) -> None:
        self.failures.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)


def renderer() -> list[str]:
    if shutil.which("kustomize"):
        return ["kustomize", "build"]
    if shutil.which("kubectl"):
        return ["kubectl", "kustomize"]
    print("::error:: kustomize 도 kubectl 도 없다 — 렌더할 수 없다", file=sys.stderr)
    sys.exit(2)


def kustomize_dirs() -> list[pathlib.Path]:
    out = []
    for k in REPO.rglob("kustomization.yaml"):
        if ".git" in k.parts:
            continue
        d = k.parent
        if SKIP_KUSTOMIZE_RE.search(str(d.relative_to(REPO))):
            continue
        out.append(d)
    return sorted(out)


def render_all(res: Result) -> dict[str, list[dict]]:
    """모든 kustomization 을 렌더하고 디렉터리형 앱 매니페스트를 모은다."""
    cmd = renderer()
    docs: dict[str, list[dict]] = {}

    for d in kustomize_dirs():
        rel = str(d.relative_to(REPO))
        p = subprocess.run(cmd + [str(d)], capture_output=True, text=True)
        if p.returncode != 0:
            res.fail(f"[render] {rel} 렌더 실패\n{p.stderr.strip()[:600]}")
            continue
        try:
            docs[rel] = [x for x in yaml.safe_load_all(p.stdout) if x]
        except yaml.YAMLError as e:
            res.fail(f"[render] {rel} 렌더 결과가 YAML 로 안 읽힌다: {e}")

    for rel in DIRECTORY_APPS:
        d = REPO / rel
        if not d.is_dir():
            res.fail(f"[parse] 디렉터리형 앱 경로가 없다: {rel}")
            continue
        collected = []
        for f in sorted(d.glob("*.yaml")):
            try:
                collected += [x for x in yaml.safe_load_all(f.read_text()) if x]
            except yaml.YAMLError as e:
                res.fail(f"[parse] {f.relative_to(REPO)} YAML 파싱 실패: {e}")
        docs[rel] = collected

    return docs


def iter_workloads(docs: dict[str, list[dict]]):
    """(source, doc, podSpec) — CronJob 의 중첩 podSpec 까지 펴서 돌려준다."""
    for src, ds in docs.items():
        for d in ds:
            if d.get("kind") not in WORKLOAD_KINDS:
                continue
            spec = d.get("spec") or {}
            pod = (spec.get("jobTemplate", {}).get("spec", {}).get("template", {}).get("spec")
                   or spec.get("template", {}).get("spec"))
            if pod:
                yield src, d, pod


def wl_id(d: dict) -> str:
    m = d.get("metadata", {})
    return f"{m.get('namespace', '-')}/{d['kind']}/{m.get('name')}"


# ─────────────────────────── 검사들 ───────────────────────────

def check_fqdn(res: Result) -> None:
    """4-dot FQDN 금지.

    `<svc>.<ns>.svc.cluster.local` 은 점이 4개라 ndots:5 아래에서 절대이름 취급이 안 된다.
    search 확장 4번째 후보가 `...cluster.local.local` 이 되어 ISP 리졸버로 나가고, ISP 가
    NXDOMAIN 을 공인 IP 로 하이재킹하면 리졸버는 성공으로 보고 멈춘다(정답인 bare name 에
    도달하지 못한다). 실측 21.7%. 짧은 `<svc>.<ns>.svc` 는 `local` 차례 전에 매치되어 안전하다.

    주석 줄은 봐준다 — 이 함정을 설명하려면 그 문자열을 적어야 한다.
    """
    pat = re.compile(r"\.svc\.cluster\.local")
    comment = re.compile(r"^\s*(#|//)")
    hits = []
    for f in REPO.rglob("*"):
        if not f.is_file() or ".git" in f.parts:
            continue
        rel = str(f.relative_to(REPO))
        if FQDN_LINT_EXCLUDE.match(rel) or f.suffix not in (".yaml", ".yml", ".json", ".md"):
            continue
        try:
            lines = f.read_text().splitlines()
        except UnicodeDecodeError:
            continue
        for i, line in enumerate(lines, 1):
            if pat.search(line) and not comment.match(line):
                hits.append(f"{rel}:{i}: {line.strip()[:100]}")
    if hits:
        res.fail("[fqdn] 4-dot FQDN 발견 — `.svc` 단축형을 쓸 것 "
                 "(근거: platform/argocd/alloy.yaml 주석)\n  " + "\n  ".join(hits))


def check_image_tags(res: Result, docs: dict[str, list[dict]]) -> None:
    """렌더 결과에 :latest 금지. ArgoCD 가 변경을 감지 못 하고 롤백도 불가능해진다."""
    hits = []
    for src, d, pod in iter_workloads(docs):
        for c in pod.get("containers", []) + pod.get("initContainers", []):
            img = c.get("image", "")
            if img.endswith(":latest") or (":" not in img.rsplit("/", 1)[-1]):
                hits.append(f"{src} :: {wl_id(d)}:{c['name']} -> {img}")
    if hits:
        res.fail("[image] :latest 또는 태그 없는 이미지 — :sha 로 핀할 것\n  " + "\n  ".join(hits))


def check_tsc_duplicate_key(res: Result, docs: dict[str, list[dict]]) -> None:
    """topologySpreadConstraints 의 topologyKey 중복 금지.

    patchMergeKey 가 topologyKey 라 같은 축 항목이 둘이면 전략적 병합이 깨지고 제약 하나가
    조용히 사라진다. API 검증은 (topologyKey, whenUnsatisfiable) 쌍만 보므로 통과한다.
    2026-08-01 에 mp-account 가 라이브에서 hostname 제약을 통째로 잃은 적이 있다.
    """
    hits = []
    for src, d, pod in iter_workloads(docs):
        keys = [t.get("topologyKey") for t in (pod.get("topologySpreadConstraints") or [])]
        dup = {k for k in keys if keys.count(k) > 1}
        if dup:
            hits.append(f"{src} :: {wl_id(d)} -> 중복 topologyKey {sorted(dup)}")
    if hits:
        res.fail("[tsc] topologyKey 중복 — 워크로드당 축 하나씩만 둘 것\n  " + "\n  ".join(hits))


def check_cnpg_failsafe_netpol(res: Result, docs: dict[str, list[dict]]) -> None:
    """CNPG 인스턴스끼리 /failsafe(8000/tcp)를 양방향으로 열었는지 검사한다.

    primary 는 kube-apiserver 를 조회할 수 없을 때 replica 인스턴스매니저의 /failsafe 를
    fallback 으로 호출한다. intra-instance 5432 만 허용하면 평소에는 정상처럼 보이지만,
    apiserver 장애가 겹치는 순간 liveness 가 실패한다. 8000 의 peer 범위도 instance,
    operator, 기존 kubelet 노드 CIDR 예외로 고정해 data namespace 전체로 넓어지는 것을 막는다.
    """
    all_policies = [
        d for ds in docs.values() for d in ds
        if d.get("kind") == "NetworkPolicy"
        and d.get("metadata", {}).get("namespace") == "data"
    ]
    all_cilium_policies = [
        d for ds in docs.values() for d in ds
        if d.get("kind") == "CiliumNetworkPolicy"
        and d.get("metadata", {}).get("namespace") == "data"
    ]
    policies = [d for d in all_policies
                if d.get("metadata", {}).get("name") == "mp-pg-instance"]
    if len(policies) != 1:
        res.fail(f"[cnpg-failsafe] data/NetworkPolicy/mp-pg-instance 가 정확히 하나여야 한다 "
                 f"(현재 {len(policies)}개)")
        return

    spec = policies[0].get("spec") or {}
    instance_labels = {
        "cnpg.io/cluster": "pg",
        "cnpg.io/podRole": "instance",
    }
    # 2026-08-03 CNPG operator 생성 pg-1/pg-2 라벨 실측. None 은 값이 동적인 필수 라벨이고,
    # primary/replica 집합은 failover 뒤에도 일부 instance 를 고르는 selector 를 탐지하기 위한 domain 이다.
    # serviceaccount 는 Cilium endpoint identity 에 합성되므로 Kubernetes Pod labels 에 없어도 포함한다.
    instance_label_domains: dict[str, set[str] | None] = {
        "app.kubernetes.io/component": {"database"},
        "app.kubernetes.io/instance": {"pg"},
        "app.kubernetes.io/managed-by": {"cloudnative-pg"},
        "app.kubernetes.io/name": {"postgresql"},
        "app.kubernetes.io/version": None,
        "cnpg.io/cluster": {"pg"},
        "cnpg.io/instanceName": None,
        "cnpg.io/instanceRole": {"primary", "replica"},
        "cnpg.io/podRole": {"instance"},
        "io.cilium.k8s.policy.serviceaccount": {"pg"},
        "role": {"primary", "replica"},
    }
    instance_selector = {"matchLabels": instance_labels}
    instance_peer = {
        "podSelector": instance_selector,
    }
    operator_peer = {
        "namespaceSelector": {
            "matchLabels": {"kubernetes.io/metadata.name": "cnpg-system"},
        },
    }
    kubelet_peer = {
        "ipBlock": {"cidr": "192.168.0.0/24"},
    }
    expected_intra_ports = [
        {"protocol": "TCP", "port": 5432},
        {"protocol": "TCP", "port": 8000},
    ]

    def normalize_cilium_policy(policy: dict) -> list[tuple[str, dict]]:
        """CNP 의 단일 spec 과 specs 목록을 같은 logical-rule 목록으로 편다."""
        name = policy.get("metadata", {}).get("name", "<unnamed>")
        has_spec = policy.get("spec") is not None
        has_specs = policy.get("specs") is not None
        if has_spec and has_specs:
            # Cilium 문서는 단일 rule 또는 rule 목록 중 하나를 사용한다. 둘 다 있으면 해석이 모호하다.
            res.fail(f"[cnpg-failsafe] CiliumNetworkPolicy/{name} 는 spec 과 specs 를 동시에 쓸 수 없다")
        if not has_spec and not has_specs:
            res.fail(f"[cnpg-failsafe] CiliumNetworkPolicy/{name} 에 spec 또는 specs 가 필요하다")

        normalized = []
        if has_spec:
            if isinstance(policy["spec"], dict):
                normalized.append((f"{name}.spec", policy["spec"]))
            else:
                res.fail(f"[cnpg-failsafe] CiliumNetworkPolicy/{name} spec 은 object 여야 한다")
        if has_specs:
            if not isinstance(policy["specs"], list):
                res.fail(f"[cnpg-failsafe] CiliumNetworkPolicy/{name} specs 는 list 여야 한다")
            else:
                for index, rule in enumerate(policy["specs"]):
                    if isinstance(rule, dict):
                        normalized.append((f"{name}.specs[{index}]", rule))
                    else:
                        res.fail(f"[cnpg-failsafe] CiliumNetworkPolicy/{name} "
                                 f"specs[{index}] 는 object 여야 한다")
        return normalized

    normalized_cilium_rules = [
        (policy, label, rule)
        for policy in all_cilium_policies
        for label, rule in normalize_cilium_policy(policy)
    ]

    if spec.get("podSelector") != instance_selector:
        res.fail("[cnpg-failsafe] mp-pg-instance target selector 는 "
                 "cnpg.io/cluster=pg + cnpg.io/podRole=instance 로 정확히 고정해야 한다")
    policy_types = spec.get("policyTypes") or []
    if len(policy_types) != 2 or set(policy_types) != {"Ingress", "Egress"}:
        res.fail("[cnpg-failsafe] mp-pg-instance policyTypes 는 Ingress·Egress 둘 다 정확히 선언해야 한다")
    pg_cilium_policies = [d for d in all_cilium_policies
                          if d.get("metadata", {}).get("name") == "mp-pg-instance-egress"]
    if len(pg_cilium_policies) != 1:
        res.fail("[cnpg-failsafe] data/CiliumNetworkPolicy/mp-pg-instance-egress 가 "
                 f"정확히 하나여야 한다 (현재 {len(pg_cilium_policies)}개)")
    else:
        pg_cilium_rules = [rule for policy, _label, rule in normalized_cilium_rules
                           if policy is pg_cilium_policies[0]]
        if not pg_cilium_rules or any(rule.get("endpointSelector") != instance_selector
                                      for rule in pg_cilium_rules):
            res.fail("[cnpg-failsafe] mp-pg-instance-egress 의 모든 target selector 는 "
                     "cnpg.io/cluster=pg + cnpg.io/podRole=instance 로 정확히 고정해야 한다")

    def is_exact_intra_rule(rule: dict, peer_key: str) -> bool:
        if set(rule) != {peer_key, "ports"} or rule.get(peer_key) != [instance_peer]:
            return False
        unmatched = list(expected_intra_ports)
        for port in rule.get("ports") or []:
            if port not in unmatched:  # endPort·named port·추가 key·중복 port 모두 거부
                return False
            unmatched.remove(port)
        return not unmatched

    def allows_tcp_port(rule: dict, target: int) -> bool:
        declared = rule.get("ports")
        if not declared:
            return True  # NetworkPolicyRule.ports 생략/빈 목록은 모든 포트를 뜻한다.
        for p in declared:
            if str(p.get("protocol", "TCP")).upper() != "TCP":
                continue
            start = p.get("port")
            if start is None:
                return True  # protocol 만 지정하면 그 protocol 의 모든 포트를 허용한다.
            if isinstance(start, int):
                end = p.get("endPort")
                if start <= target <= (end if end is not None else start):
                    return True
            elif isinstance(start, str):
                # named port 의 실제 숫자는 operator 생성 Pod 에 있어 이 레포에서 알 수 없다.
                # additive 우회를 놓치지 않도록 TCP named port 는 보수적으로 8000 가능성이 있다고 본다.
                return True
        return False

    def cilium_rule_allows_tcp_port(rule: dict, target: int) -> bool:
        port_rules = rule.get("toPorts")
        if not port_rules:
            return True  # Cilium ingress/egress rule 의 toPorts 생략은 모든 포트를 뜻한다.
        for port_rule in port_rules:
            declared = port_rule.get("ports")
            if not declared:
                return True
            for port in declared:
                protocol = str(port.get("protocol", "ANY")).upper()
                if protocol not in {"TCP", "ANY"}:
                    continue
                raw_start = port.get("port")
                if raw_start is None:
                    return True
                try:
                    start = int(raw_start)
                except (TypeError, ValueError):
                    return True  # named port 는 operator 생성 Pod 에서 8000 으로 해석될 수 있다.
                raw_end = port.get("endPort")
                try:
                    end = int(raw_end) if raw_end is not None else start
                except (TypeError, ValueError):
                    return True
                if start <= target <= end:
                    return True
        return False

    def selector_can_select_instance(selector: dict) -> bool:
        """LabelSelector 가 현재 또는 failover 뒤 pg instance 일부와 양립 가능한지 계산한다."""
        domains = {key: (None if values is None else set(values))
                   for key, values in instance_label_domains.items()}
        excluded = {key: set() for key, values in domains.items() if values is None}
        requirements = [
            (key.removeprefix("k8s:"), "In", [value])
            for key, value in (selector.get("matchLabels") or {}).items()
        ]
        requirements.extend(
            (str(expr.get("key", "")).removeprefix("k8s:"),
             expr.get("operator"), expr.get("values") or [])
            for expr in selector.get("matchExpressions") or []
        )

        for key, op, raw_values in requirements:
            values = {str(value) for value in raw_values}
            if key not in domains:
                if op in {"NotIn", "DoesNotExist"}:  # canonical instance 에 없는 키
                    continue
                return False
            domain = domains[key]
            if op == "In":
                domain = ((values - excluded.get(key, set())) if domain is None
                          else domain & values)
            elif op == "NotIn":
                if domain is None:
                    excluded[key].update(values)
                else:
                    domain -= values
            elif op == "Exists":
                pass
            elif op == "DoesNotExist":
                return False
            else:
                return False
            if domain is not None and not domain:
                return False
            domains[key] = domain
        return True

    for direction, peer_key in (("ingress", "from"), ("egress", "to")):
        rules = spec.get(direction) or []
        intra = [r for r in rules if is_exact_intra_rule(r, peer_key)]
        if len(intra) != 1:
            res.fail(f"[cnpg-failsafe] mp-pg-instance {direction} 의 instance 전용 규칙은 "
                     "추가 key/range 없이 TCP 5432·8000 만 정확히 허용해야 한다")

    # NetworkPolicy 는 additive 다. mp-pg-instance 자체만 검사하면 같은 파드를 선택하는 두 번째
    # 정책이 TCP 8000 을 다시 넓게 열어도 놓치므로 data namespace 의 모든 선택 정책을 합쳐 본다.
    targeted = [d for d in all_policies
                if selector_can_select_instance((d.get("spec") or {}).get("podSelector") or {})]
    broadened = []
    for policy in targeted:
        policy_spec = policy.get("spec") or {}
        name = policy.get("metadata", {}).get("name", "<unnamed>")
        for direction, peer_key in (("ingress", "from"), ("egress", "to")):
            allowed_peers = [instance_peer]
            if direction == "ingress":
                # kubelet probe 는 노드 CIDR 에서 모든 파드 포트를 여는 기존의 명시적 예외다.
                allowed_peers.extend([operator_peer, kubelet_peer])
            for index, rule in enumerate(policy_spec.get(direction) or []):
                if not allows_tcp_port(rule, 8000):
                    continue
                peers = rule.get(peer_key)
                if not peers or any(peer not in allowed_peers for peer in peers):
                    broadened.append(f"{name}.{direction}[{index}] peers={peers}")
    if broadened:
        res.fail("[cnpg-failsafe] pg instance 를 선택하는 additive NetworkPolicy 가 TCP 8000 을 "
                 "instance/operator/kubelet 경계 밖으로 넓혔다:\n  " + "\n  ".join(broadened))

    # CiliumNetworkPolicy 도 표준 NetworkPolicy 와 같은 endpoint 에 additive 로 붙는다. 이 레포에서
    # failsafe 8000 은 표준 정책의 exact peer 규칙만 소유하므로, CNP 의 8000 grant 는 전부 우회다.
    cilium_broadened = []
    for _policy, label, policy_rule in normalized_cilium_rules:
        if not selector_can_select_instance(policy_rule.get("endpointSelector") or {}):
            continue
        for direction in ("ingress", "egress"):
            for index, rule in enumerate(policy_rule.get(direction) or []):
                if cilium_rule_allows_tcp_port(rule, 8000):
                    cilium_broadened.append(f"{label}.{direction}[{index}]")
    if cilium_broadened:
        res.fail("[cnpg-failsafe] pg instance 를 선택하는 additive CiliumNetworkPolicy 가 "
                 "TCP 8000 을 허용한다:\n  " + "\n  ".join(cilium_broadened))


SEC_CHECKS = {
    "runAsNonRoot": lambda csc, psc: (csc.get("runAsNonRoot") if "runAsNonRoot" in csc
                                      else psc.get("runAsNonRoot")) is True,
    "readOnlyRootFilesystem": lambda csc, psc: csc.get("readOnlyRootFilesystem") is True,
    "allowPrivilegeEscalation": lambda csc, psc: csc.get("allowPrivilegeEscalation") is False,
    "capabilities.drop=ALL": lambda csc, psc: "ALL" in ((csc.get("capabilities") or {}).get("drop") or []),
}


def collect_sec_violations(docs: dict[str, list[dict]]) -> set[str]:
    """렌더 결과 기준 securityContext 위반. pod-level 상속을 반영한다.

    🔴 container 만 보면 안 된다 — runAsNonRoot 는 pod securityContext 에서 상속되므로
    container 만 검사하면 정상 워크로드가 대량 오탐으로 잡힌다(작성 중 실제로 겪음).
    readOnlyRootFilesystem·allowPrivilegeEscalation·capabilities 는 container 전용이다.
    """
    out = set()
    for _src, d, pod in iter_workloads(docs):
        psc = pod.get("securityContext") or {}
        for c in pod.get("containers", []):
            csc = c.get("securityContext") or {}
            for name, ok in SEC_CHECKS.items():
                if not ok(csc, psc):
                    out.add(f"{name} {wl_id(d)}:{c['name']}")
    return out


def check_security_context(res: Result, docs: dict[str, list[dict]], list_only: bool) -> set[str]:
    """렌더 결과의 하드닝 — 베이스라인 대비 **새 위반만** 실패시킨다.

    지금 24건이 깨져 있는데(pipelines 의 op:add 치환 + data/mp-redis-pgsync) 그걸 전부
    고치기 전까지 CI 를 못 켜면 CI 가 영영 안 들어온다. 그래서 알려진 위반을 얼리고
    새로 생기는 것만 막는다. 베이스라인은 **줄어들기만 해야 한다**.
    """
    found = collect_sec_violations(docs)
    if list_only:
        return found

    baseline = set()
    if BASELINE.exists():
        for line in BASELINE.read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                baseline.add(line)

    new = found - baseline
    fixed = baseline - found

    if new:
        res.fail("[security] 새 securityContext 위반 — base 에 선언했는데 렌더 결과에 없다면 "
                 "오버레이 패치가 치환하고 있을 수 있다(JSON-Patch op:add 는 merge 가 아니다)\n  "
                 + "\n  ".join(sorted(new)))
    if fixed:
        res.warn(f"[security] 베이스라인 {len(fixed)}건이 해소됐다 — "
                 f"scripts/policy-baseline.txt 에서 지울 것:\n  " + "\n  ".join(sorted(fixed)))
    return found


def run_kubeconform(res: Result) -> None:
    if not shutil.which("kubeconform"):
        res.warn("[schema] kubeconform 없음 — 스키마 검증 건너뜀")
        return
    cmd = renderer()
    crd = ("https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/"
           "{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json")
    for d in kustomize_dirs():
        rel = str(d.relative_to(REPO))
        rendered = subprocess.run(cmd + [str(d)], capture_output=True, text=True)
        if rendered.returncode != 0:
            continue  # 렌더 실패는 check_render 가 이미 잡았다
        p = subprocess.run(
            ["kubeconform", "-strict", "-ignore-missing-schemas", "-summary",
             "-schema-location", "default", "-schema-location", crd, "-"],
            input=rendered.stdout, capture_output=True, text=True)
        if p.returncode != 0:
            res.fail(f"[schema] {rel}\n{(p.stdout + p.stderr).strip()[:800]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true",
                    help="securityContext 위반 목록만 출력(베이스라인 갱신용)")
    args = ap.parse_args()

    res = Result()
    docs = render_all(res)

    if args.list:
        for v in sorted(collect_sec_violations(docs)):
            print(v)
        return 0

    check_fqdn(res)
    check_image_tags(res, docs)
    check_tsc_duplicate_key(res, docs)
    check_cnpg_failsafe_netpol(res, docs)
    check_security_context(res, docs, list_only=False)
    run_kubeconform(res)

    rendered_docs = sum(len(v) for v in docs.values())
    print(f"검사 대상: kustomization {len(kustomize_dirs())}개 + "
          f"디렉터리형 앱 {len(DIRECTORY_APPS)}개 = 매니페스트 {rendered_docs}개")

    for w in res.warnings:
        print(f"\n⚠️  {w}")
    for f in res.failures:
        print(f"\n❌ {f}")

    if res.failures:
        print(f"\n실패 {len(res.failures)}건")
        return 1
    print("\n✅ 통과" + (f" (경고 {len(res.warnings)}건)" if res.warnings else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
