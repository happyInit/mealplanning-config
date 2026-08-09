#!/usr/bin/env python3
"""config 레포 검증 — 병합 전에 "git 에는 있는데 클러스터엔 없는" 부류를 잡는다.

왜 있는가
---------
이 레포에는 CI 가 없었고 13개 Application 이 automated sync 다. 즉 잘못된 매니페스트가
아무 관문 없이 클러스터에 도달했다. 2026-08-02 감사에서 실제로 그렇게 새어나간 것 3부류:

  1. pipelines/base/kustomization.yaml 의 JSON-Patch `op: add` 가 merge 가 아니라 **replace** 라
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
import json
import pathlib
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from datetime import datetime

import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
BASELINE = REPO / "scripts" / "policy-baseline.txt"

# 사이트 결합 값의 단일 선언점(0-10). 이 스크립트는 **읽기만** 한다 — 온프렘 LAN 값을
# 코드에 리터럴로 두면 사이트가 늘 때마다 검증기를 고쳐야 한다. 근거·갱신 절차는 그 파일 머리말.
SITES_FILE = REPO / "scripts" / "sites.yaml"

# 실제로 배포되는 것만 본다. base/ 와 overlays/eks 는 ArgoCD 가 안 쓴다(온프렘 오버레이만 쓴다).
SKIP_KUSTOMIZE_RE = re.compile(r"/(base|overlays/eks)$")

# kustomization 없이 매니페스트를 그대로 두는 디렉터리형 ArgoCD 앱
#
# 🔴 2026-08-09(0-1) 로 6개가 여기서 빠졌다 — platform/{es,kafka,pg,pgsync,pooler,redis} 는
#    사이트 분기(base/ + overlays/{onprem,eks})를 받으면서 kustomize 트랙이 됐다.
#    이제 그 매니페스트는 kustomize_dirs() 의 `<트랙>/overlays/onprem` 렌더로 들어온다.
#    남은 셋은 성격이 다르다:
#      argocd/applications · platform/argocd = **ArgoCD 뿌리**. 뿌리 Application 2개가 IaC 밖이라
#        (체크리스트 0-4) 경로를 config 레포 커밋으로 못 바꾼다 → 재구성 대상에서 의도적으로 뺐다.
#      pipelines/jobs = ArgoCD 비대상(1회성 kubectl). desired state 가 아니라 분기할 것도 없다.
DIRECTORY_APPS = [
    "argocd/applications", "pipelines/jobs", "platform/argocd",
]

# 사이트 분기 골격(0-1). ArgoCD 가 실제로 쓰는 것은 overlays/onprem 뿐이고,
# overlays/eks 는 Wave B 에서 채운다 — 다만 **렌더는 지금부터 깨지지 않아야** 한다.
SITE_OVERLAYS = ("onprem", "eks")

# 우리가 CI 로 굽는 이미지의 이름 규칙(CLAUDE.md §명명 규칙 — 신규는 전부 `mp-`).
# 3rd-party(quay.io·ghcr.io·docker.io)는 레지스트리 이관 결정이 따로라 검사 대상이 아니다.
OUR_IMAGE_RE = re.compile(r"^mp-[A-Za-z0-9._-]+$")

# CRD 가 이미지를 담는 필드는 kind 마다 다르다 — 컨테이너 경로만 보면 놓친다.
#   Elasticsearch(ECK) = spec.image · Cluster(CNPG) = spec.imageName
# 그래서 파싱된 문서를 통째로 걸어 이 키들의 **문자열** 값을 전부 모은다.
IMAGE_KEYS = ("image", "imageName")


def load_sites() -> dict:
    """scripts/sites.yaml 로드. 없거나 깨졌으면 검증을 세운다 — 조용히 빈 값으로 통과시키면
    kubelet 예외 CIDR 이 사라져 CNPG failsafe 검사가 **더 엄격해진 척** 오탐한다."""
    if not SITES_FILE.is_file():
        print(f"::error:: {SITES_FILE.relative_to(REPO)} 가 없다 — 사이트 결합 값의 정본이다",
              file=sys.stderr)
        sys.exit(2)
    data = yaml.safe_load(SITES_FILE.read_text()) or {}
    for site in SITE_OVERLAYS:
        if site not in data:
            print(f"::error:: sites.yaml 에 `{site}` 항목이 없다", file=sys.stderr)
            sys.exit(2)
    return data


SITES = load_sites()


def node_cidr_peers() -> list[dict]:
    """kubelet probe 예외로 허용되는 ipBlock peer 목록 — 선언된 전 사이트의 노드 대역."""
    peers = []
    for site in SITE_OVERLAYS:
        for cidr in SITES[site].get("node_cidrs") or []:
            peers.append({"ipBlock": {"cidr": cidr}})
    return peers


def iter_images(node: object):
    """파싱된 문서를 재귀로 걸어 이미지 문자열을 전부 뽑는다(컨테이너·CRD 필드·Helm values 포함)."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k in IMAGE_KEYS and isinstance(v, str) and v.strip():
                yield v
            else:
                yield from iter_images(v)
    elif isinstance(node, list):
        for v in node:
            yield from iter_images(v)


def image_repo_name(image: str) -> str:
    """`<registry>/<path>/<repo>:<tag>` 또는 `...@sha256:...` 에서 `<repo>` 만 뽑는다."""
    ref = image.split("@", 1)[0]
    last = ref.rsplit("/", 1)[-1]
    return last.split(":", 1)[0]

WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"}

# 스캔에서 통째로 뺄 디렉터리. `.pdv` 는 PDV(에이전트 위임) 가 만드는 git worktree 자리로,
# 레포 전체의 사본이라 여기까지 세면 같은 오브젝트가 worktree 수만큼 중복 계수된다
# (실제로 mp-pg-instance NetworkPolicy 가 1개 → 3개로 잡혀 cnpg-failsafe 검사가 오탐했다).
# git-ignored 임시 작업본이고 배포되지 않으므로 검사 대상이 아니다.
SCAN_EXCLUDE_PARTS = {".git", ".pdv"}

# 이 스크립트 자신과 문서는 FQDN 린트 대상에서 제외한다(설명하려면 그 문자열을 써야 한다).
# AGENTS.md 도 같은 이유 — 하위 모델에게 "4-dot 금지" 규칙을 알려주려면 그 문자열을 적어야 한다.
FQDN_LINT_EXCLUDE = re.compile(r"^(scripts/|README\.md$|AGENTS\.md$|\.github/)")


class Result:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.warnings: list[str] = []

    def fail(self, msg: str) -> None:
        self.failures.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)


RFC3339_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


def is_rfc3339_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not RFC3339_TIMESTAMP.fullmatch(value):
        return False
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def check_manual_job_security(res: Result, name: str, job: dict) -> None:
    """Argo 밖에서 create하는 수동 Job에도 restricted baseline을 강제한다."""
    pod = (((job.get("spec") or {}).get("template") or {}).get("spec") or {})
    pod_security = pod.get("securityContext") or {}
    expected_pod_security = {
        "runAsNonRoot": True,
        "runAsUser": 10001,
        "runAsGroup": 10001,
        "fsGroup": 10001,
        "fsGroupChangePolicy": "OnRootMismatch",
    }
    pod_bad = {
        key: (pod_security.get(key), value)
        for key, value in expected_pod_security.items()
        if pod_security.get(key) != value
    }
    if (pod_security.get("seccompProfile") or {}).get("type") != "RuntimeDefault":
        pod_bad["seccompProfile.type"] = (
            (pod_security.get("seccompProfile") or {}).get("type"),
            "RuntimeDefault",
        )
    if pod_bad:
        res.fail(f"[pgsync-lifecycle] manual {name} Job pod securityContext 불일치: {pod_bad}")

    volumes = pod.get("volumes") or []
    tmp_volumes = [volume for volume in volumes
                   if volume.get("name") == "tmp" and "emptyDir" in volume]
    if len(tmp_volumes) != 1:
        res.fail(f"[pgsync-lifecycle] manual {name} Job은 writable /tmp emptyDir 하나가 필요하다")

    if (job.get("metadata") or {}).get("name") == "mp-pgsync-bootstrap":
        empty_dirs = {
            volume.get("name") for volume in volumes if "emptyDir" in volume
        }
        expected_empty_dirs = {"plugins-dir", "checkpoint", "tmp"}
        if not expected_empty_dirs.issubset(empty_dirs):
            res.fail(
                f"[pgsync-lifecycle] bootstrap writable volumes 불일치: "
                f"actual={sorted(empty_dirs, key=repr)}, "
                f"expected={sorted(expected_empty_dirs)}"
            )

        containers_by_name = {
            container.get("name"): container for container in pod.get("containers") or []
        }
        init_by_name = {
            container.get("name"): container for container in pod.get("initContainers") or []
        }
        expected_mounts = (
            (containers_by_name.get("pgsync") or {}, {
                ("plugins-dir", "/app/plugins"),
                ("checkpoint", "/app/checkpoint"),
                ("tmp", "/tmp"),
            }),
            (init_by_name.get("copy-plugins") or {}, {
                ("plugins-dir", "/app/plugins"),
                ("tmp", "/tmp"),
            }),
        )
        for container, expected in expected_mounts:
            actual = {
                (mount.get("name"), mount.get("mountPath"))
                for mount in container.get("volumeMounts") or []
                if mount.get("readOnly") is not True
            }
            if not expected.issubset(actual):
                res.fail(
                    f"[pgsync-lifecycle] bootstrap writable mounts 불일치: "
                    f"actual={sorted(actual, key=repr)}, expected={sorted(expected)}"
                )

    expected_container_security = {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
    }
    allowed_container_overrides = {
        "runAsNonRoot": True,
        "runAsUser": 10001,
        "runAsGroup": 10001,
    }
    groups = (("container", pod.get("containers") or []),
              ("initContainer", pod.get("initContainers") or []))
    for kind, containers in groups:
        for container in containers:
            container_name = container.get("name", "<unnamed>")
            security = container.get("securityContext") or {}
            bad = {
                key: (security.get(key), value)
                for key, value in expected_container_security.items()
                if security.get(key) != value
            }
            for key, value in allowed_container_overrides.items():
                if key in security and security.get(key) != value:
                    bad[key] = (security.get(key), value)
            seccomp = security.get("seccompProfile")
            if seccomp is not None and (seccomp or {}).get("type") != "RuntimeDefault":
                bad["seccompProfile.type"] = (
                    (seccomp or {}).get("type"), "RuntimeDefault"
                )
            capabilities = security.get("capabilities") or {}
            if "ALL" not in (capabilities.get("drop") or []):
                bad["capabilities.drop"] = (
                    capabilities.get("drop"), ["ALL"]
                )
            if capabilities.get("add"):
                bad["capabilities.add"] = (capabilities.get("add"), [])
            if bad:
                res.fail(
                    f"[pgsync-lifecycle] manual {name} Job {kind} securityContext "
                    f"불일치({container_name}): {bad}"
                )
            mounts = container.get("volumeMounts") or []
            if not any(mount.get("name") == "tmp" and mount.get("mountPath") == "/tmp"
                       and mount.get("readOnly") is not True
                       for mount in mounts):
                res.fail(
                    f"[pgsync-lifecycle] manual {name} Job {kind}에 writable /tmp mount 누락: "
                    f"{container_name}"
                )
            image = container.get("image", "")
            if image.endswith(":latest") or ":" not in image.rsplit("/", 1)[-1]:
                res.fail(
                    f"[pgsync-lifecycle] manual {name} Job image는 :sha pin이어야 한다: {image}"
                )


def check_pgsync_egress_policy(res: Result, policy: dict) -> None:
    """Manual identities and the daemon may reach only their four data dependencies."""
    spec = policy.get("spec") or {}
    if policy.get("kind") != "NetworkPolicy":
        res.fail("[pgsync-lifecycle] PGSync egress boundary는 NetworkPolicy여야 한다")
    if spec.get("podSelector") != {"matchLabels": {"app": "pgsync"}}:
        res.fail("[pgsync-lifecycle] PGSync egress selector는 exact app=pgsync여야 한다")
    if spec.get("policyTypes") != ["Egress"]:
        res.fail("[pgsync-lifecycle] PGSync policyTypes는 Egress-only여야 한다")

    def canonical(rule: dict):
        destinations = rule.get("to")
        ports = rule.get("ports")
        if not isinstance(destinations, list) or len(destinations) != 1:
            return None
        if not isinstance(ports, list) or not ports:
            return None
        normalized_ports = []
        for port in ports:
            if set(port) != {"protocol", "port"}:
                return None
            normalized_ports.append((port["protocol"], port["port"]))
        return (
            json.dumps(destinations[0], sort_keys=True, separators=(",", ":")),
            tuple(sorted(normalized_ports, key=repr)),
        )

    expected = {
        (json.dumps({
            "namespaceSelector": {"matchLabels": {
                "kubernetes.io/metadata.name": "kube-system",
            }},
            "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
        }, sort_keys=True, separators=(",", ":")), (("TCP", 53), ("UDP", 53))),
        (json.dumps({"podSelector": {"matchLabels": {
            "cnpg.io/cluster": "pg", "cnpg.io/podRole": "instance",
        }}}, sort_keys=True, separators=(",", ":")), (("TCP", 5432),)),
        (json.dumps({"podSelector": {"matchLabels": {
            "elasticsearch.k8s.elastic.co/cluster-name": "es",
        }}}, sort_keys=True, separators=(",", ":")), (("TCP", 9200),)),
        (json.dumps({"podSelector": {"matchLabels": {
            "app": "redis-pgsync",
        }}}, sort_keys=True, separators=(",", ":")), (("TCP", 6379),)),
    }
    actual_list = [canonical(rule) for rule in (spec.get("egress") or [])]
    if None in actual_list or len(actual_list) != len(set(actual_list)) or set(actual_list) != expected:
        res.fail("[pgsync-lifecycle] PGSync egress는 exact DNS/PG/ES/Redis 규칙만 허용해야 한다")


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
        # 🔴 REPO 상대 경로로 판정한다. 절대 경로(k.parts)를 쓰면, 이 레포를 `.pdv/worktrees/<x>`
        #    안에 체크아웃한 상태(= PDV worktree 안에서 검증을 돌릴 때) 자기 자신이 통째로
        #    제외돼 "kustomization 0개" 가 된다.
        if SCAN_EXCLUDE_PARTS & set(k.relative_to(REPO).parts):
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


def check_site_overlays(res: Result) -> dict[str, list[dict]]:
    """사이트 분기 골격(0-1)이 살아 있나.

    왜 있는가 — 골격은 **쓰지 않는 쪽이 조용히 썩는다**. overlays/eks 는 ArgoCD 가 보지도
    않고 다른 검사에서도 제외(SKIP_KUSTOMIZE_RE)되므로, 누가 base 의 파일명을 바꾸면
    eks 오버레이만 렌더 불능이 되고 **아무도 모른 채 이관 당일에 발견**된다.

    보는 것 셋:
      ① base/ 가 있으면 overlays/onprem 도 있어야 한다 (= 배포 경로가 실재하나)
      ② 있는 overlays/eks 는 렌더가 성공해야 한다 (= 골격이 안 썩었나)
      ③ eks 오버레이가 없는 트랙은 경고로 남긴다 — "AWS 에 안 올린다"는 판단일 수도 있어
         실패로 두지 않는다(예: services/cloudflared = C-5 로 온프렘 DR 전용).

    반환값 = eks 렌더 결과(`<트랙>/overlays/eks` -> 문서 목록). 어차피 한 번 떠 본 것이라
    두 번 렌더하지 않으려고 돌려준다 — check_registry_split() 가 이걸 받아 쓴다.
    """
    cmd = renderer()
    missing_eks = []
    eks_docs: dict[str, list[dict]] = {}
    for base in sorted(REPO.rglob("base")):
        if not base.is_dir() or SCAN_EXCLUDE_PARTS & set(base.relative_to(REPO).parts):
            continue
        track = base.parent
        rel = str(track.relative_to(REPO))
        onprem = track / "overlays" / "onprem" / "kustomization.yaml"
        if not onprem.is_file():
            res.fail(f"[site] {rel}: base/ 는 있는데 overlays/onprem 이 없다 — 배포 경로가 사라진다")
        eks = track / "overlays" / "eks"
        if not (eks / "kustomization.yaml").is_file():
            missing_eks.append(rel)
            continue
        p = subprocess.run(cmd + [str(eks)], capture_output=True, text=True)
        if p.returncode != 0:
            res.fail(f"[site] {rel}/overlays/eks 렌더 실패 — 골격이 썩었다\n{p.stderr.strip()[:400]}")
            continue
        try:
            eks_docs[str(eks.relative_to(REPO))] = [x for x in yaml.safe_load_all(p.stdout) if x]
        except yaml.YAMLError as e:
            res.fail(f"[site] {rel}/overlays/eks 렌더 결과가 YAML 로 안 읽힌다: {e}")
    if missing_eks:
        res.warn("[site] eks 오버레이가 없는 트랙 — 의도(그 사이트에 안 올림)면 무시, 아니면 0-1 누락:\n  "
                 + "\n  ".join(missing_eks))
    return eks_docs


def check_registry_split(res: Result, docs: dict[str, list[dict]],
                         eks_docs: dict[str, list[dict]]) -> None:
    """레지스트리가 사이트별로 갈렸나 (0-9).

    왜 있는가 — `overlays/eks` 는 ArgoCD 가 보지도 않고 다른 검사에서도 제외된다
    (SKIP_KUSTOMIZE_RE). 즉 **레지스트리가 온프렘 Harbor 그대로여도 아무도 안 잡는다.**
    실제로 0-1 직후 상태가 그랬다 — services 13종만 ECR 매핑이 있었고 pipelines·es·pgsync·
    video 는 렌더가 `192.168.0.10/...` 을 그대로 가리켰다. 이관 당일에 ImagePullBackOff 로
    발견되는 부류라 지금 기계가 잡는다.

    보는 것 셋 (대상 = 우리가 CI 로 굽는 `mp-*` 이미지만. 3rd-party 는 이관 결정이 따로다):
      ① eks 렌더에 온프렘 레지스트리(Harbor LAN IP)가 남아 있으면 실패
      ② eks 렌더의 `mp-*` 이미지는 전부 sites.yaml `eks.registry` 접두사여야 한다
         → 계정 ID 가 정해졌을 때 **덜 고친 오버레이를 전수로 열거**해 준다
      ③ 온프렘 렌더의 `mp-*` 이미지는 전부 `onprem.registry` 여야 한다
         (= 누가 base 에 ECR 주소를 박아 온프렘으로 새는 반대 방향 사고)

    ⚠️ 한계 = **`platform/argocd` 는 못 본다.** Helm 소스 Application 12종의 값은 인라인
       valuesObject 라 base/overlays 가 아직 없다(0-4 컷오버 선행). 그래서 `rollouts` 의
       initContainer 이미지(`mp-rollouts-gatewayapi-plugin`)는 여기서 걸리지 않는다 — 0-9 잔여.
    """
    onprem_reg = str(SITES["onprem"]["registry"]).rstrip("/")
    eks_reg = str(SITES["eks"]["registry"]).rstrip("/")

    leaked, mismatched, onprem_wrong = [], [], []
    for src, ds in eks_docs.items():
        for image in {i for d in ds for i in iter_images(d)}:
            if image.startswith(onprem_reg + "/"):
                leaked.append(f"{src} -> {image}")
            elif OUR_IMAGE_RE.match(image_repo_name(image)) and not image.startswith(eks_reg + "/"):
                mismatched.append(f"{src} -> {image}")
    for src, ds in docs.items():
        for image in {i for d in ds for i in iter_images(d)}:
            if OUR_IMAGE_RE.match(image_repo_name(image)) and not image.startswith(onprem_reg + "/"):
                onprem_wrong.append(f"{src} -> {image}")

    if leaked:
        res.fail(f"[registry] eks 렌더가 온프렘 레지스트리({onprem_reg})를 가리킨다 — "
                 "해당 overlays/eks 에 images 매핑을 넣을 것\n  " + "\n  ".join(sorted(leaked)))
    if mismatched:
        res.fail(f"[registry] eks 렌더의 mp-* 이미지가 sites.yaml eks.registry({eks_reg}) 와 다르다 — "
                 "sites.yaml 을 바꿨으면 아래 오버레이도 같이 맞출 것\n  "
                 + "\n  ".join(sorted(mismatched)))
    if onprem_wrong:
        res.fail(f"[registry] 온프렘 렌더의 mp-* 이미지가 {onprem_reg} 가 아니다 — "
                 "base 에 사이트 전용 주소가 박혔을 수 있다\n  " + "\n  ".join(sorted(onprem_wrong)))


# 온프렘 ESO 스토어의 이름. eks 렌더에 이게 남아 있으면 그 ExternalSecret 은 AWS 에서 NotReady 다.
ONPREM_SECRET_STORE = "fb-kubernetes"


def check_eks_secret_store(res: Result) -> None:
    """eks 렌더에 온프렘 스토어(`fb-kubernetes`)가 남아 있나 (0-2 · C-23).

    왜 있는가 — **이 실수는 조용하다.** `secretStoreRef` 가 온프렘 스토어를 가리켜도 kustomize 는
    잘 렌더되고 kubeconform 도 통과한다. AWS 에는 `fb-secrets` ns 도 `eso-reader` SA 도 없으므로
    ExternalSecret 이 `SecretSyncedError` 로 앉고, 그 Secret 을 `envFrom` 으로 받는 파드는
    **CreateContainerConfigError 로 영원히 안 뜬다.** 이관 당일에 30개가 한꺼번에 그렇게 된다.

    특히 잘 썩는 경로 = **base 에 ExternalSecret 을 새로 추가했을 때**. 온프렘은 즉시 동작하니
    아무도 eks 오버레이에 패치를 더해야 한다는 걸 모른다. 그 창을 이 검사가 닫는다.

    ⚠️ eks 오버레이가 **없는** 트랙은 보지 않는다 — `services/cloudflared` 처럼 "그 사이트에
       안 올린다"가 정답인 경우가 있고, 그건 check_site_overlays 의 경고가 이미 담당한다.
    """
    cmd = renderer()
    for base in sorted(REPO.rglob("base")):
        if not base.is_dir() or SCAN_EXCLUDE_PARTS & set(base.relative_to(REPO).parts):
            continue
        track = base.parent
        eks = track / "overlays" / "eks"
        if not (eks / "kustomization.yaml").is_file():
            continue
        p = subprocess.run(cmd + [str(eks)], capture_output=True, text=True)
        if p.returncode != 0:
            continue  # 렌더 실패는 check_site_overlays 가 이미 실패로 잡는다
        stale = []
        for d in yaml.safe_load_all(p.stdout):
            if not d or d.get("kind") != "ExternalSecret":
                continue
            ref = (d.get("spec") or {}).get("secretStoreRef") or {}
            if ref.get("name") == ONPREM_SECRET_STORE:
                stale.append(d.get("metadata", {}).get("name"))
        if stale:
            res.fail(
                f"[site] {track.relative_to(REPO)}/overlays/eks: ExternalSecret 이 온프렘 스토어"
                f" `{ONPREM_SECRET_STORE}` 를 그대로 가리킨다 → {', '.join(stale)}\n"
                "        AWS 에는 그 스토어가 없다. eks 오버레이에 secretStoreRef 패치를 더할 것"
                " (본보기 = services/account/overlays/eks/kustomization.yaml · 설명 = bootstrap/eso/README.md)"
            )


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
        if not f.is_file():
            continue
        # 위 kustomize_dirs() 와 같은 이유로 REPO 상대 경로로 판정한다.
        if SCAN_EXCLUDE_PARTS & set(f.relative_to(REPO).parts):
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
    # 🔴 0-10(2026-08-10): 여기 `192.168.0.0/24` 가 리터럴로 박혀 있었다. 검증기가 한 사이트의
    #    물리 주소를 알고 있으면 사이트가 늘 때 **매니페스트가 아니라 스크립트를** 고치게 된다.
    #    이제 정본은 scripts/sites.yaml 이고, 선언된 전 사이트의 노드 대역이 허용된다.
    #    (eks 는 VPC CIDR 미정이라 `node_cidrs: []` — 정해지면 거기만 채운다.)
    kubelet_peers = node_cidr_peers()
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
                allowed_peers.append(operator_peer)
                allowed_peers.extend(kubelet_peers)
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
def check_es_maintenance_job(res: Result, job: dict) -> None:
    """Alias 보정 Job에 Elasticsearch 외 credential/실행 경로가 없음을 강제한다."""
    pod = (((job.get("spec") or {}).get("template") or {}).get("spec") or {})
    containers = pod.get("containers") or []
    init_containers = pod.get("initContainers") or []
    if len(containers) != 1:
        res.fail("[pgsync-lifecycle] ES maintenance Job은 단일 main container여야 한다")
    if init_containers:
        res.fail("[pgsync-lifecycle] ES maintenance Job에 initContainer를 둘 수 없다")
    container = containers[0] if len(containers) == 1 else {}
    if container.get("command") != ["python3", "/ops/maintenance.py", "alias-write"]:
        res.fail("[pgsync-lifecycle] ES maintenance Job은 DB 없는 alias-write action만 실행해야 한다")

    expected_env_names = {
        "ELASTICSEARCH_HOST",
        "ELASTICSEARCH_PORT",
        "ELASTICSEARCH_SCHEME",
        "ELASTICSEARCH_USER",
        "ELASTICSEARCH_PASSWORD",
    }
    env = container.get("env") or []
    env_names = [item.get("name") for item in env]
    if len(env_names) != len(expected_env_names) or set(env_names) != expected_env_names:
        res.fail(f"[pgsync-lifecycle] ES maintenance Job 환경변수 계약 불일치: {env_names}")

    secret_refs = []
    all_containers = containers + init_containers
    for item in all_containers:
        if item.get("envFrom"):
            res.fail(
                f"[pgsync-lifecycle] ES maintenance Job envFrom 금지: "
                f"{item.get('name', '<unnamed>')}"
            )
        for variable in item.get("env") or []:
            env_name = str(variable.get("name", ""))
            if env_name.startswith("PG") or env_name == "DATABASE_URL":
                res.fail("[pgsync-lifecycle] ES maintenance Job에 DB 환경변수가 들어가면 안 된다")
            secret_ref = (((variable.get("valueFrom") or {}).get("secretKeyRef")) or {})
            if secret_ref:
                secret_refs.append(
                    (env_name, secret_ref.get("name"), secret_ref.get("key"))
                )
    if secret_refs != [("ELASTICSEARCH_PASSWORD", "es-es-elastic-user", "elastic")]:
        res.fail(f"[pgsync-lifecycle] ES maintenance Job Secret 계약 불일치: {secret_refs}")

    secret_volumes = []
    for volume in pod.get("volumes") or []:
        if volume.get("secret"):
            secret_volumes.append(volume.get("name"))
        for source in ((volume.get("projected") or {}).get("sources") or []):
            if source.get("secret"):
                secret_volumes.append(volume.get("name"))
    if secret_volumes:
        res.fail(
            f"[pgsync-lifecycle] ES maintenance Job Secret volume 금지: {secret_volumes}"
        )


def check_pgsync_stable_alias(res: Result, docs: dict[str, list[dict]]) -> None:
    """T-3 lifecycle 불변조건: Git은 PARK, 수동 Job은 inert, runtime은 stable alias."""
    # 🔴 경로에 `base/` 가 들어간 것은 0-1(2026-08-09) 사이트 분기 골격 때문이다.
    #    파일 내용은 그대로고 자리만 <트랙>/ → <트랙>/base/ 로 내려갔다.
    role_path = REPO / "platform/pg/base/bootstrap-role.yaml"
    schema_path = REPO / "platform/pgsync/base/schema-configmap.yaml"
    rollout_path = REPO / "services/recipe/base/rollout.yaml"
    pgsync_netpol_path = REPO / "platform/policies-data/base/netpol-pgsync.yaml"
    policies_kustomization_path = REPO / "platform/policies-data/base/kustomization.yaml"
    ops_dir = REPO / "ops/pgsync-stable-alias"
    required = [
        ops_dir / "README.md", ops_dir / "ops.sh", ops_dir / "maintenance.py",
        ops_dir / "bootstrap-job.yaml", ops_dir / "maintenance-job.yaml",
        ops_dir / "crud-verify-job.yaml", ops_dir / "es-maintenance-job.yaml",
        ops_dir / "recipes-index.json",
    ]
    missing = [str(p.relative_to(REPO)) for p in required if not p.is_file()]
    if missing:
        res.fail("[pgsync-lifecycle] 운영 번들 파일 누락\n  " + "\n  ".join(missing))
        return

    try:
        role = yaml.safe_load(role_path.read_text())["spec"]
        schema = yaml.safe_load(schema_path.read_text())
        rollout = yaml.safe_load(rollout_path.read_text())
        bootstrap_job = yaml.safe_load((ops_dir / "bootstrap-job.yaml").read_text())
        maintenance_job = yaml.safe_load((ops_dir / "maintenance-job.yaml").read_text())
        crud_job = yaml.safe_load((ops_dir / "crud-verify-job.yaml").read_text())
        es_job = yaml.safe_load((ops_dir / "es-maintenance-job.yaml").read_text())
        pgsync_netpol = yaml.safe_load(pgsync_netpol_path.read_text())
        policies_kustomization = yaml.safe_load(policies_kustomization_path.read_text())
        contract = json.loads((ops_dir / "recipes-index.json").read_text())
    except (KeyError, TypeError, yaml.YAMLError, json.JSONDecodeError) as e:
        res.fail(f"[pgsync-lifecycle] 운영 번들 파싱 실패: {e}")
        return

    parked = {
        "login": False,
        "replication": False,
        "inherit": True,
        "disablePassword": True,
        "superuser": False,
        "createdb": False,
        "createrole": False,
        "bypassrls": False,
        "connectionLimit": 1,
        "inRoles": [],
    }
    bad = [f"{key}={role.get(key)!r} (expected {value!r})"
           for key, value in parked.items() if role.get(key) != value]
    if "passwordSecret" in role or "validUntil" in role:
        bad.append("passwordSecret/validUntil must not exist in tracked PARK state")
    if bad:
        res.fail("[pgsync-lifecycle] bootstrap DatabaseRole은 Git에서 항상 PARK여야 한다\n  "
                 + "\n  ".join(bad))

    retire_annotation = "operations.mealplanning.io/legacy-slot-retire-after"
    retire_after = (((schema.get("metadata") or {}).get("annotations") or {}).get(
        retire_annotation
    ))
    if not is_rfc3339_timestamp(retire_after):
        res.fail(
            f"[pgsync-lifecycle] schema ConfigMap {retire_annotation}는 timezone이 있는 "
            f"RFC3339 timestamp여야 한다: {retire_after!r}"
        )

    try:
        schema_doc = json.loads(schema["data"]["schema.json"])
        schema_index = schema_doc[0]["index"]
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as e:
        res.fail(f"[pgsync-lifecycle] schema ConfigMap을 읽을 수 없다: {e}")
        schema_index = None
    if schema_index != "recipes_live":
        res.fail(f"[pgsync-lifecycle] PGSync index는 recipes_live여야 한다: {schema_index!r}")

    try:
        containers = rollout["spec"]["template"]["spec"]["containers"]
        recipe = next(c for c in containers if c["name"] == "recipe")
        es_index = next(e["value"] for e in recipe["env"] if e["name"] == "ES_INDEX")
    except (KeyError, StopIteration, TypeError) as e:
        res.fail(f"[pgsync-lifecycle] recipe ES_INDEX를 읽을 수 없다: {e}")
        es_index = None
    if es_index != "recipes_live":
        res.fail(f"[pgsync-lifecycle] recipe ES_INDEX는 recipes_live여야 한다: {es_index!r}")

    check_pgsync_egress_policy(res, pgsync_netpol)
    if "netpol-pgsync.yaml" not in (policies_kustomization.get("resources") or []):
        res.fail("[pgsync-lifecycle] PGSync egress NetworkPolicy가 policies-data 렌더에 포함되지 않았다")

    jobs = (
        ("bootstrap", "mp-pgsync-bootstrap", bootstrap_job),
        ("maintenance", "mp-pgsync-maintenance", maintenance_job),
        ("crud", "mp-pgsync-crud-verify", crud_job),
        ("es-maintenance", "mp-pgsync-es-maintenance", es_job),
    )
    for name, expected_name, job in jobs:
        spec = job.get("spec") or {}
        labels = (((spec.get("template") or {}).get("metadata") or {}).get("labels") or {})
        if (job.get("metadata") or {}).get("name") != expected_name:
            res.fail(f"[pgsync-lifecycle] manual {name} Job 고정 이름은 {expected_name}이어야 한다")
        if job.get("kind") != "Job" or spec.get("suspend") is not True:
            res.fail(f"[pgsync-lifecycle] manual {name} Job은 suspend:true여야 한다")
        if spec.get("backoffLimit") != 0:
            res.fail(f"[pgsync-lifecycle] manual {name} Job은 backoffLimit:0이어야 한다")
        if labels.get("app") != "pgsync":
            res.fail(f"[pgsync-lifecycle] manual {name} Job label app=pgsync 누락(NetworkPolicy 계약)")
        check_manual_job_security(res, name, job)

    check_es_maintenance_job(res, es_job)

    settings = contract.get("settings") or {}
    props = ((contract.get("mappings") or {}).get("properties") or {})
    expected_fields = {
        "name": ("text", "korean"),
        "ingredient_names": ("text", "korean"),
        "category": ("keyword", None),
        "source": ("keyword", None),
        "servable": ("boolean", None),
    }
    if settings.get("number_of_replicas") != 1:
        res.fail("[pgsync-lifecycle] canonical recipe index는 replica=1이어야 한다")
    for field, (field_type, analyzer) in expected_fields.items():
        actual = props.get(field) or {}
        if actual.get("type") != field_type or (analyzer and actual.get("analyzer") != analyzer):
            res.fail(f"[pgsync-lifecycle] canonical mapping 불일치: {field} -> {actual}")

    # Argo desired-state roots에 destructive bootstrap Job/credential이 들어오면 안 된다.
    for src, ds in docs.items():
        if not (src.startswith("platform/") or src.startswith("argocd/")):
            continue
        for doc in ds:
            meta = doc.get("metadata") or {}
            if doc.get("kind") == "Application":
                source_path = (((doc.get("spec") or {}).get("source") or {}).get("path") or "")
                if source_path == "ops" or source_path.startswith("ops/"):
                    res.fail(f"[pgsync-lifecycle] ops/는 Argo Application source가 될 수 없다: {src}")
            if doc.get("kind") == "Job" and meta.get("name") in {
                "mp-pgsync-bootstrap", "mp-pgsync-maintenance", "mp-pgsync-crud-verify",
                "mp-pgsync-es-maintenance",
            }:
                res.fail(f"[pgsync-lifecycle] manual Job이 Argo desired state에 포함됨: {src}")
            if doc.get("kind") == "Secret" and meta.get("name") == "mp-pgsync-bootstrap-db":
                res.fail(f"[pgsync-lifecycle] bootstrap credential을 Git에 넣으면 안 된다: {src}")

    ops_text = (ops_dir / "ops.sh").read_text()
    for marker in ("validUntil", "kubernetes.io/basic-auth", "is_write_index", "recover_parked"):
        if marker not in ops_text:
            res.fail(f"[pgsync-lifecycle] ops.sh fail-safe 누락: {marker}")


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

    eks_docs = check_site_overlays(res)
    check_eks_secret_store(res)
    check_registry_split(res, docs, eks_docs)
    check_fqdn(res)
    check_image_tags(res, docs)
    check_tsc_duplicate_key(res, docs)
    check_cnpg_failsafe_netpol(res, docs)
    check_pgsync_stable_alias(res, docs)
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
