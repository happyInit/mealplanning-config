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
