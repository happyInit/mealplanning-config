#!/usr/bin/env bash
# Manual PGSync lifecycle controller. Git/Argo desired state always remains PARKED.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
NAMESPACE="data"
EXPECTED_CONTEXT="${PGSYNC_CONTEXT:-mp-k8s}"
ROLE="mp-pgsync-bootstrap"
SECRET="mp-pgsync-bootstrap-db"
BOOTSTRAP_JOB="mp-pgsync-bootstrap"
MAINTENANCE_JOB="mp-pgsync-maintenance"
ES_MAINTENANCE_JOB="mp-pgsync-es-maintenance"
CRUD_JOB="mp-pgsync-crud-verify"
SCRIPT_CONFIGMAP="mp-pgsync-maintenance-script"
LOCK="mp-pgsync-stable-alias-lock"
LOCK_DURATION_SECONDS=2100
LIVE_ALIAS="recipes_live"
LIVE_SLOT="foodbudget_recipes_live"
LEGACY_SLOT="foodbudget_recipes_pgsync"
PASSWORD_FILE=""
RENDERED_JOB=""
LEASE_FILE=""
CURRENT_LEASE_FILE=""
RUN_ID=""
LOCK_HELD=0
LOCK_RENEW_PID=""
ROLE_RECOVERY=0
EPHEMERAL_CLEANUP=0
FINALIZING=0

die() { echo "ERROR: $*" >&2; exit 1; }
note() { echo "==> $*"; }

remove_temp_files() {
  [[ -z "$PASSWORD_FILE" ]] || rm -f -- "$PASSWORD_FILE"
  [[ -z "$RENDERED_JOB" ]] || rm -f -- "$RENDERED_JOB"
  [[ -z "$LEASE_FILE" ]] || rm -f -- "$LEASE_FILE"
  [[ -z "$CURRENT_LEASE_FILE" ]] || rm -f -- "$CURRENT_LEASE_FILE"
}

stop_lock_renewal() {
  if [[ -n "$LOCK_RENEW_PID" ]]; then
    kill "$LOCK_RENEW_PID" >/dev/null 2>&1 || true
    wait "$LOCK_RENEW_PID" >/dev/null 2>&1 || true
    LOCK_RENEW_PID=""
  fi
}

release_lock() {
  local rc=0
  [[ "$LOCK_HELD" == "1" ]] || return 0
  stop_lock_renewal
  renew_lock_once || rc=$?
  if [[ "$rc" == "0" ]]; then
    kubectl -n "$NAMESPACE" get lease "$LOCK" -o json >"$CURRENT_LEASE_FILE" || rc=$?
  fi
  if [[ "$rc" == "0" ]]; then
    python3 - "$RUN_ID" "$CURRENT_LEASE_FILE" >"$LEASE_FILE" <<'PY' || rc=$?
import json, sys
holder, path = sys.argv[1:]
with open(path, encoding="utf-8") as stream:
    lease = json.load(stream)
if (lease.get("spec") or {}).get("holderIdentity") != holder:
    raise SystemExit("Lease ownership changed before release")
metadata = lease["metadata"]
json.dump({
    "apiVersion": "v1",
    "kind": "DeleteOptions",
    "preconditions": {
        "uid": metadata["uid"],
        "resourceVersion": metadata["resourceVersion"],
    },
}, sys.stdout)
PY
  fi
  if [[ "$rc" == "0" ]]; then
    kubectl delete --raw "/apis/coordination.k8s.io/v1/namespaces/$NAMESPACE/leases/$LOCK" \
      -f "$LEASE_FILE" >/dev/null || rc=$?
  fi
  LOCK_HELD=0
  if [[ "$rc" != "0" ]]; then
    echo "ERROR: lifecycle Lease release failed or ownership changed" >&2
    return "$rc"
  fi
}

finalize() {
  local original_rc=$? cleanup_rc=0
  [[ "$FINALIZING" == "0" ]] || exit "$original_rc"
  FINALIZING=1
  trap - EXIT INT TERM
  set +e
  if [[ "$ROLE_RECOVERY" == "1" ]]; then
    recover_parked || cleanup_rc=$?
  elif [[ "$EPHEMERAL_CLEANUP" == "1" ]]; then
    delete_ephemeral || cleanup_rc=$?
  fi
  if [[ "$cleanup_rc" == "0" ]]; then
    release_lock || cleanup_rc=$?
  else
    # Keep the fencing Lease until expiry when child cleanup could not be proven.
    stop_lock_renewal
    echo "ERROR: lifecycle Lease retained because cleanup was incomplete" >&2
  fi
  remove_temp_files
  set -e
  if [[ "$original_rc" == "0" && "$cleanup_rc" != "0" ]]; then
    original_rc="$cleanup_rc"
  fi
  exit "$original_rc"
}

install_traps() {
  trap finalize EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
}

require_tools() {
  local tool
  for tool in kubectl python3 openssl tr date hostname; do
    command -v "$tool" >/dev/null || die "required tool missing: $tool"
  done
  [[ "$(kubectl config current-context)" == "$EXPECTED_CONTEXT" ]] ||
    die "kubectl context must be $EXPECTED_CONTEXT"
}

renew_lock_once() {
  local now
  [[ "$LOCK_HELD" == "1" ]] || return 1
  now="$(date -u '+%Y-%m-%dT%H:%M:%S.000000Z')"
  kubectl -n "$NAMESPACE" patch lease "$LOCK" --type=json -p "[{\"op\":\"test\",\"path\":\"/spec/holderIdentity\",\"value\":\"$RUN_ID\"},{\"op\":\"replace\",\"path\":\"/spec/renewTime\",\"value\":\"$now\"}]" >/dev/null
}

assert_lock_owned() {
  [[ "$LOCK_HELD" == "1" ]] || die "lifecycle Lease was not acquired"
  if ! renew_lock_once; then
    # Never let a stale process clean up resources now owned by a successor.
    LOCK_HELD=0
    ROLE_RECOVERY=0
    EPHEMERAL_CLEANUP=0
    die "lifecycle Lease ownership was lost; refusing further mutation or cleanup"
  fi
}

start_lock_renewal() {
  local parent_pid="$$"
  (
    while kill -0 "$parent_pid" >/dev/null 2>&1; do
      sleep 30
      kill -0 "$parent_pid" >/dev/null 2>&1 || exit 0
      renew_lock_once || echo "WARNING: lifecycle Lease renewal failed; retrying" >&2
    done
  ) &
  LOCK_RENEW_PID=$!
}

acquire_lock() {
  local attempt rc
  [[ "$LOCK_HELD" == "0" ]] || die "lifecycle Lease is already held by this process"
  RUN_ID="$(hostname)-$$-$(openssl rand -hex 8)"
  LEASE_FILE="$(mktemp)"
  CURRENT_LEASE_FILE="$(mktemp)"

  for attempt in $(seq 1 5); do
    if ! kubectl -n "$NAMESPACE" get lease "$LOCK" -o json >"$CURRENT_LEASE_FILE" 2>/dev/null; then
      python3 - "$NAMESPACE" "$LOCK" "$RUN_ID" "$LOCK_DURATION_SECONDS" >"$LEASE_FILE" <<'PY'
import datetime, json, sys
namespace, name, holder, duration = sys.argv[1:]
now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
json.dump({
    "apiVersion": "coordination.k8s.io/v1",
    "kind": "Lease",
    "metadata": {"name": name, "namespace": namespace},
    "spec": {
        "holderIdentity": holder,
        "leaseDurationSeconds": int(duration),
        "acquireTime": now,
        "renewTime": now,
        "leaseTransitions": 0,
    },
}, sys.stdout)
PY
      if kubectl create -f "$LEASE_FILE" >/dev/null; then
        LOCK_HELD=1
        start_lock_renewal
        note "acquired lifecycle Lease as $RUN_ID"
        return 0
      fi
    else
      if python3 - "$RUN_ID" "$LOCK_DURATION_SECONDS" "$CURRENT_LEASE_FILE" >"$LEASE_FILE" <<'PY'
import datetime, json, sys
holder, duration, path = sys.argv[1:]
with open(path, encoding="utf-8") as stream:
    current = json.load(stream)
spec = current.get("spec") or {}
old_holder = spec.get("holderIdentity")
stamp = spec.get("renewTime") or spec.get("acquireTime")
seconds = int(spec.get("leaseDurationSeconds") or duration)
now = datetime.datetime.now(datetime.timezone.utc)
if old_holder and not stamp:
    print(f"malformed active Lease held by {old_holder}: no acquire/renew time", file=sys.stderr)
    raise SystemExit(4)
if old_holder:
    renewed = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    expires = renewed + datetime.timedelta(seconds=seconds)
    if expires > now:
        print(f"lifecycle already running: holder={old_holder}, expires={expires.isoformat()}", file=sys.stderr)
        raise SystemExit(3)
stamp_now = now.isoformat(timespec="microseconds").replace("+00:00", "Z")
json.dump({
    "apiVersion": "coordination.k8s.io/v1",
    "kind": "Lease",
    "metadata": {
        "name": current["metadata"]["name"],
        "namespace": current["metadata"]["namespace"],
        "resourceVersion": current["metadata"]["resourceVersion"],
    },
    "spec": {
        "holderIdentity": holder,
        "leaseDurationSeconds": int(duration),
        "acquireTime": stamp_now,
        "renewTime": stamp_now,
        "leaseTransitions": int(spec.get("leaseTransitions") or 0) + 1,
    },
}, sys.stdout)
PY
      then
        if kubectl replace -f "$LEASE_FILE" >/dev/null; then
          LOCK_HELD=1
          start_lock_renewal
          note "acquired lifecycle Lease as $RUN_ID"
          return 0
        fi
      else
        rc=$?
        [[ "$rc" != "3" ]] || die "another PGSync lifecycle owns $NAMESPACE/$LOCK"
        die "existing lifecycle Lease is malformed; inspect $NAMESPACE/$LOCK"
      fi
    fi
    sleep 1
  done
  die "could not atomically acquire $NAMESPACE/$LOCK after retries"
}

wait_role_applied() {
  local generation observed applied i
  generation="$(kubectl -n "$NAMESPACE" get databaserole "$ROLE" -o jsonpath='{.metadata.generation}')"
  for i in $(seq 1 60); do
    observed="$(kubectl -n "$NAMESPACE" get databaserole "$ROLE" -o jsonpath='{.status.observedGeneration}' 2>/dev/null || true)"
    applied="$(kubectl -n "$NAMESPACE" get databaserole "$ROLE" -o jsonpath='{.status.applied}' 2>/dev/null || true)"
    [[ "$observed" == "$generation" && "$applied" == "true" ]] && return 0
    sleep 1
  done
  echo "ERROR: DatabaseRole did not reconcile generation $generation" >&2
  return 1
}

assert_local_role_parked() {
  python3 - "$REPO_ROOT/platform/pg/bootstrap-role.yaml" <<'PY'
import sys, yaml
doc = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
s = doc["spec"]
expected = {
    "login": False, "replication": False, "disablePassword": True,
    "connectionLimit": 1, "inRoles": [], "superuser": False,
    "createdb": False, "createrole": False, "bypassrls": False,
}
bad = {k: (s.get(k), v) for k, v in expected.items() if s.get(k) != v}
if bad or "passwordSecret" in s or "validUntil" in s:
    raise SystemExit(f"tracked DatabaseRole is not PARKED: {bad}")
PY
}

park_role() {
  assert_lock_owned
  note "PARK role and remove transient password reference"
  # Remove passwordSecret in the same admission request that enables
  # disablePassword; CNPG declares the two fields mutually exclusive.
  kubectl -n "$NAMESPACE" patch databaserole "$ROLE" --type merge -p \
    '{"spec":{"login":false,"replication":false,"connectionLimit":1,"inRoles":[],"disablePassword":true,"passwordSecret":null,"validUntil":null}}' >/dev/null
  assert_lock_owned
  kubectl apply -f "$REPO_ROOT/platform/pg/bootstrap-role.yaml" >/dev/null
  wait_role_applied
}

delete_ephemeral() {
  local rc=0
  assert_lock_owned
  kubectl -n "$NAMESPACE" delete job "$BOOTSTRAP_JOB" "$MAINTENANCE_JOB" "$ES_MAINTENANCE_JOB" "$CRUD_JOB" --ignore-not-found --cascade=foreground --wait=true --timeout=2m >/dev/null || rc=$?
  assert_lock_owned
  kubectl -n "$NAMESPACE" delete configmap "$SCRIPT_CONFIGMAP" --ignore-not-found >/dev/null || rc=$?
  assert_lock_owned
  kubectl -n "$NAMESPACE" delete secret "$SECRET" --ignore-not-found >/dev/null || rc=$?
  return "$rc"
}

recover_parked() {
  local park_rc=0 delete_rc=0 daemon_rc=0
  assert_lock_owned
  set +e
  park_role || park_rc=$?
  delete_ephemeral || delete_rc=$?
  # Recovery is a fresh process after SIGKILL too; never rely on in-memory state.
  if [[ "$delete_rc" == "0" ]]; then
    assert_lock_owned
    kubectl -n "$NAMESPACE" scale deployment mp-pgsync --replicas=1 >/dev/null || daemon_rc=$?
    kubectl -n "$NAMESPACE" rollout status deployment/mp-pgsync --timeout=5m >/dev/null || daemon_rc=$?
  fi
  remove_temp_files
  set -e
  if [[ "$park_rc" != "0" ]]; then
    echo "ERROR: PARK reconciliation failed; validUntil still limits the password lifetime" >&2
    return "$park_rc"
  fi
  if [[ "$delete_rc" != "0" ]]; then
    echo "ERROR: one or more ephemeral PGSync resources could not be deleted" >&2
    return "$delete_rc"
  fi
  if [[ "$daemon_rc" != "0" ]]; then
    echo "ERROR: mp-pgsync did not return healthy after cleanup" >&2
    return "$daemon_rc"
  fi
}

create_basic_auth_secret() {
  kubectl -n "$NAMESPACE" get secret "$SECRET" >/dev/null 2>&1 &&
    die "$NAMESPACE/$SECRET already exists; run cleanup and inspect first"
  umask 077
  PASSWORD_FILE="$(mktemp)"
  chmod 0600 "$PASSWORD_FILE"
  openssl rand -base64 32 | tr -d '\n' >"$PASSWORD_FILE"
  assert_lock_owned
  kubectl -n "$NAMESPACE" create secret generic "$SECRET" \
    --type=kubernetes.io/basic-auth \
    --from-literal=username="$ROLE" \
    --from-file=password="$PASSWORD_FILE" >/dev/null
  rm -f -- "$PASSWORD_FILE"
  PASSWORD_FILE=""
}

activate_role() {
  local deadline patch
  create_basic_auth_secret
  deadline="$(date -u -d '+30 minutes' '+%Y-%m-%dT%H:%M:%SZ')"
  patch="$(python3 -c 'import json,sys; print(json.dumps({"spec":{"login":True,"replication":True,"connectionLimit":10,"inRoles":["fbapp","pgsync"],"disablePassword":None,"passwordSecret":{"name":"mp-pgsync-bootstrap-db"},"validUntil":sys.argv[1]}}))' "$deadline")"
  note "activate role until $deadline (password expiry fail-safe)"
  assert_lock_owned
  kubectl -n "$NAMESPACE" patch databaserole "$ROLE" --type merge -p "$patch" >/dev/null
  wait_role_applied || die "active DatabaseRole reconciliation failed"
}

primary_pod() {
  local pods
  mapfile -t pods < <(kubectl -n "$NAMESPACE" get pods \
    -l cnpg.io/cluster=pg,role=primary -o name)
  [[ "${#pods[@]}" == "1" ]] || die "expected exactly one CNPG primary, found ${#pods[@]}"
  echo "${pods[0]#pod/}"
}

pg_scalar() {
  local pod
  pod="$(primary_pod)"
  kubectl -n "$NAMESPACE" exec "$pod" -c postgres -- \
    psql -X -U postgres -d foodbudget -Atqc "$1"
}

assert_live_pgsync_egress() {
  local live_policy rc=0
  live_policy="$(mktemp)"
  kubectl -n "$NAMESPACE" get networkpolicy mp-pgsync-egress -o json >"$live_policy" || rc=$?
  if [[ "$rc" == "0" ]]; then
    python3 - "$REPO_ROOT" "$live_policy" <<'PY' || rc=$?
import json, pathlib, sys
repo, path = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
sys.path.insert(0, str(repo / "scripts"))
import validate
result = validate.Result()
validate.check_pgsync_egress_policy(result, json.loads(path.read_text()))
if result.failures:
    raise SystemExit("; ".join(result.failures))
PY
  fi
  rm -f -- "$live_policy"
  [[ "$rc" == "0" ]] || die "live mp-pgsync-egress NetworkPolicy is missing or differs from exact contract"
}

assert_active_role() {
  local state memberships role_json
  role_json="$(kubectl -n "$NAMESPACE" get databaserole "$ROLE" -o json)"
  python3 -c '
import datetime,json,sys
d=json.load(sys.stdin); s=d["spec"]; st=d.get("status",{})
want={"login":True,"replication":True,"inherit":True,"connectionLimit":10,"inRoles":["fbapp","pgsync"],"superuser":False,"createdb":False,"createrole":False,"bypassrls":False}
bad={k:(s.get(k),v) for k,v in want.items() if s.get(k)!=v}
if bad or "disablePassword" in s or s.get("passwordSecret",{}).get("name")!="mp-pgsync-bootstrap-db": raise SystemExit(f"live DatabaseRole not ACTIVE: {bad}")
deadline=datetime.datetime.fromisoformat(s["validUntil"].replace("Z","+00:00"))
if deadline <= datetime.datetime.now(datetime.timezone.utc): raise SystemExit("DatabaseRole validUntil already expired")
if st.get("applied") is not True or st.get("observedGeneration") != d["metadata"]["generation"]: raise SystemExit("DatabaseRole status is stale/not applied")' <<<"$role_json"
  state="$(pg_scalar "SELECT rolcanlogin||'|'||rolreplication||'|'||(rolpassword IS NOT NULL)||'|'||(rolvaliduntil > now()) FROM pg_authid WHERE rolname='$ROLE'")"
  [[ "$state" == "true|true|true|true" || "$state" == "t|t|t|t" ]] || die "database role is not active: $state"
  memberships="$(pg_scalar "SELECT string_agg(r.rolname,',' ORDER BY r.rolname) FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.roleid JOIN pg_roles u ON u.oid=m.member WHERE u.rolname='$ROLE'")"
  [[ "$memberships" == "fbapp,pgsync" ]] || die "unexpected role memberships: $memberships"
}

alias_write_explicit() {
  [[ "${PGSYNC_CONFIRM:-}" == "SET_RECIPES_LIVE_WRITE_INDEX" ]] ||
    die "set PGSYNC_CONFIRM=SET_RECIPES_LIVE_WRITE_INDEX"
  assert_lock_owned
  EPHEMERAL_CLEANUP=1
  run_es_alias_job
  EPHEMERAL_CLEANUP=0
}

prepare_index() {
  local index="$1"
  assert_lock_owned
  [[ "$index" =~ ^recipes_v[0-9]+$ ]] || die "generation index must match recipes_v<digits>"
  [[ "$index" != "$LIVE_ALIAS" ]] || die "never create a concrete index named $LIVE_ALIAS"
  kubectl -n "$NAMESPACE" exec -i deployment/mp-pgsync -c pgsync -- \
    python3 -c '
import base64, json, os, sys, urllib.error, urllib.request
index=sys.argv[1]; contract=json.load(sys.stdin); base="http://es-es-http:9200"
auth=base64.b64encode((os.environ.get("ELASTICSEARCH_USER","elastic")+":"+os.environ["ELASTICSEARCH_PASSWORD"]).encode()).decode()
def call(path, method="GET", body=None):
    data=None if body is None else json.dumps(body).encode()
    req=urllib.request.Request(base+path,data=data,method=method,headers={"Authorization":"Basic "+auth,"Content-Type":"application/json"})
    with urllib.request.urlopen(req,timeout=20) as response:return json.load(response)
try:
    result=call("/"+index,"PUT",contract)
except urllib.error.HTTPError as exc:
    if exc.code == 400 and b"resource_already_exists_exception" in exc.read():
        result={"acknowledged":True}
        created=False
    else: raise
else:
    created=True
if not result.get("acknowledged"): raise SystemExit("index creation not acknowledged")
if call("/"+index+"/_count")["count"] != 0: raise SystemExit("existing generation is not empty")
aliases=call("/"+index+"/_alias")
if aliases.get(index,{}).get("aliases"): raise SystemExit("generation is already attached to an alias")
mapping=call("/"+index+"/_mapping")[index]["mappings"]["properties"]
if mapping != contract["mappings"]["properties"]: raise SystemExit("mapping differs from canonical exact contract")
settings=call("/"+index+"/_settings")[index]["settings"]["index"]
if settings.get("number_of_replicas")!="1": raise SystemExit("index must have one replica")
if settings.get("number_of_shards")!="1": raise SystemExit("index must have one primary shard")
if settings.get("analysis") != contract["settings"]["analysis"]: raise SystemExit("analysis differs from canonical exact contract")
tokens=[x["token"] for x in call("/"+index+"/_analyze","POST",{"analyzer":"korean","text":"김치찌개"})["tokens"]]
if not {"김치찌개","김치","찌개"}.issubset(tokens): raise SystemExit("nori contract failed")
print("OK: "+("created " if created else "verified existing ")+index+" from canonical contract")' "$index" <"$SCRIPT_DIR/recipes-index.json"
}

create_script_configmap() {
  assert_lock_owned
  kubectl -n "$NAMESPACE" create configmap "$SCRIPT_CONFIGMAP" \
    --from-file=maintenance.py="$SCRIPT_DIR/maintenance.py" >/dev/null
}

run_job() {
  local name="$1" file="$2" timeout="${3:-15m}"
  assert_lock_owned
  kubectl create -f "$file" >/dev/null
  assert_lock_owned
  kubectl -n "$NAMESPACE" patch job "$name" --type merge -p '{"spec":{"suspend":false}}' >/dev/null
  if ! kubectl -n "$NAMESPACE" wait --for=condition=complete "job/$name" --timeout="$timeout"; then
    kubectl -n "$NAMESPACE" logs "job/$name" --all-containers=true --tail=200 || true
    assert_lock_owned
    return 1
  fi
  kubectl -n "$NAMESPACE" logs "job/$name" --all-containers=true --tail=200
  assert_lock_owned
}

run_maintenance() {
  local action="$1"
  [[ "$action" == "restore-acl" || "$action" == "retire-legacy" ]] || die "invalid maintenance action"
  RENDERED_JOB="$(mktemp)"
  sed "s/__ACTION__/$action/g" "$SCRIPT_DIR/maintenance-job.yaml" >"$RENDERED_JOB"
  run_job "$MAINTENANCE_JOB" "$RENDERED_JOB" 5m
}

run_es_alias_job() {
  local run_rc=0 cleanup_rc=0
  create_script_configmap || return $?
  run_job "$ES_MAINTENANCE_JOB" "$SCRIPT_DIR/es-maintenance-job.yaml" 3m || run_rc=$?
  assert_lock_owned
  kubectl -n "$NAMESPACE" delete job "$ES_MAINTENANCE_JOB" --ignore-not-found --cascade=foreground --wait=true --timeout=2m >/dev/null || cleanup_rc=$?
  assert_lock_owned
  kubectl -n "$NAMESPACE" delete configmap "$SCRIPT_CONFIGMAP" --ignore-not-found >/dev/null || cleanup_rc=$?
  [[ "$run_rc" == "0" ]] || return "$run_rc"
  return "$cleanup_rc"
}

run_crud_job() {
  local run_rc=0 cleanup_rc=0
  assert_lock_owned
  EPHEMERAL_CLEANUP=1
  create_script_configmap || return $?
  run_job "$CRUD_JOB" "$SCRIPT_DIR/crud-verify-job.yaml" 3m || run_rc=$?
  assert_lock_owned
  kubectl -n "$NAMESPACE" delete job "$CRUD_JOB" --ignore-not-found --cascade=foreground --wait=true --timeout=2m >/dev/null || cleanup_rc=$?
  assert_lock_owned
  kubectl -n "$NAMESPACE" delete configmap "$SCRIPT_CONFIGMAP" --ignore-not-found >/dev/null || cleanup_rc=$?
  [[ "$run_rc" == "0" ]] || return "$run_rc"
  [[ "$cleanup_rc" == "0" ]] || return "$cleanup_rc"
  EPHEMERAL_CLEANUP=0
}

wait_daemon_stopped() {
  local pods i
  for i in $(seq 1 120); do
    pods="$(kubectl -n "$NAMESPACE" get pods -l app=pgsync -o name)"
    [[ -z "$pods" ]] && return 0
    sleep 1
  done
  die "mp-pgsync pod did not terminate; refusing bootstrap while a slot consumer may be alive"
}

verify_live() {
  local legacy_policy="${1:-absent}"
  local app status pg_count es_count role_state membership legacy_count legacy_ok recorded_deadline view_bad view_shape expected_backing slot_ok role_json matched i
  [[ "$legacy_policy" == "absent" || "$legacy_policy" == "allow-retirement" ]] ||
    die "invalid legacy verification policy: $legacy_policy"
  for app in pg pgsync mp-recipe mp-policies-data; do
    status="$(kubectl -n argocd get application "$app" -o jsonpath='{.status.sync.status}{"|"}{.status.health.status}')"
    [[ "$status" == "Synced|Healthy" ]] || die "Argo application $app is $status"
  done
  assert_live_pgsync_egress
  [[ "$(kubectl -n "$NAMESPACE" get configmap mp-pgsync-schema -o jsonpath='{.data.schema\.json}' | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["index"])')" == "$LIVE_ALIAS" ]] || die "live PGSync schema is not recipes_live"
  [[ "$(kubectl -n app get rollout mp-recipe -o json | python3 -c 'import json,sys; d=json.load(sys.stdin); print(next(e["value"] for c in d["spec"]["template"]["spec"]["containers"] if c["name"]=="recipe" for e in c["env"] if e["name"]=="ES_INDEX"))')" == "$LIVE_ALIAS" ]] || die "recipe ES_INDEX is not recipes_live"

  role_json="$(kubectl -n "$NAMESPACE" get databaserole "$ROLE" -o json)"
  python3 -c '
import json,sys
d=json.load(sys.stdin); s=d["spec"]; st=d.get("status",{})
want={"login":False,"replication":False,"inherit":True,"disablePassword":True,"superuser":False,"createdb":False,"createrole":False,"bypassrls":False,"connectionLimit":1,"inRoles":[]}
bad={k:(s.get(k),v) for k,v in want.items() if s.get(k)!=v}
if bad or "passwordSecret" in s or "validUntil" in s: raise SystemExit(f"live DatabaseRole not PARKED: {bad}")
if st.get("applied") is not True or st.get("observedGeneration") != d["metadata"]["generation"]: raise SystemExit("DatabaseRole status is stale/not applied")' <<<"$role_json"
  role_state="$(pg_scalar "SELECT rolcanlogin||'|'||rolreplication||'|'||rolsuper||'|'||rolcreatedb||'|'||rolcreaterole||'|'||rolbypassrls||'|'||rolconnlimit||'|'||(rolpassword IS NULL) FROM pg_authid WHERE rolname='$ROLE'")"
  [[ "$role_state" == "false|false|false|false|false|false|1|true" || "$role_state" == "f|f|f|f|f|f|1|t" ]] || die "bootstrap role is not exact PARK: $role_state"
  membership="$(pg_scalar "SELECT count(*) FROM pg_auth_members m JOIN pg_roles u ON u.oid=m.member WHERE u.rolname='$ROLE'")"
  [[ "$membership" == "0" ]] || die "bootstrap role still has memberships"
  kubectl -n "$NAMESPACE" get secret "$SECRET" >/dev/null 2>&1 && die "orphan bootstrap Secret exists"
  kubectl -n "$NAMESPACE" get job "$BOOTSTRAP_JOB" >/dev/null 2>&1 && die "orphan bootstrap Job exists"
  kubectl -n "$NAMESPACE" get job "$MAINTENANCE_JOB" >/dev/null 2>&1 && die "orphan maintenance Job exists"
  kubectl -n "$NAMESPACE" get job "$ES_MAINTENANCE_JOB" >/dev/null 2>&1 && die "orphan ES maintenance Job exists"
  kubectl -n "$NAMESPACE" get job "$CRUD_JOB" >/dev/null 2>&1 && die "orphan CRUD Job exists"
  kubectl -n "$NAMESPACE" get configmap "$SCRIPT_CONFIGMAP" >/dev/null 2>&1 && die "orphan maintenance ConfigMap exists"

  slot_ok="$(pg_scalar "SELECT database='foodbudget' AND plugin='test_decoding' AND slot_type='logical' AND confirmed_flush_lsn IS NOT NULL AND restart_lsn IS NOT NULL AND wal_status IN ('reserved','extended') AND pg_wal_lsn_diff(pg_current_wal_lsn(),restart_lsn) < 1024::bigint*1024*1024 AND pg_wal_lsn_diff(pg_current_wal_lsn(),confirmed_flush_lsn) < 1024::bigint*1024*1024 FROM pg_replication_slots WHERE slot_name='$LIVE_SLOT'")"
  [[ "$slot_ok" == "t" ]] || die "live slot contract/lag check failed"
  legacy_count="$(pg_scalar "SELECT count(*) FROM pg_replication_slots WHERE slot_name='$LEGACY_SLOT'")"
  [[ "$legacy_count" == "0" || "$legacy_count" == "1" ]] || die "legacy slot query returned an impossible count: $legacy_count"
  if [[ "$legacy_count" != "0" ]]; then
    legacy_ok="$(pg_scalar "SELECT database='foodbudget' AND plugin='test_decoding' AND slot_type='logical' AND active=false AND restart_lsn IS NOT NULL FROM pg_replication_slots WHERE slot_name='$LEGACY_SLOT'")"
    [[ "$legacy_ok" == "t" ]] || die "legacy slot differs from the exact inactive logical-slot contract"
    if [[ "$legacy_policy" == "allow-retirement" ]]; then
      echo "WARNING: legacy slot accepted only for the locked pre-retirement gate" >&2
    else
      [[ -n "${ALLOW_LEGACY_SLOT_UNTIL:-}" ]] || die "legacy slot exists without bounded rollback deadline"
      recorded_deadline="$(kubectl -n "$NAMESPACE" get configmap mp-pgsync-schema -o json | python3 -c 'import json,sys; print((json.load(sys.stdin).get("metadata",{}).get("annotations",{})).get("operations.mealplanning.io/legacy-slot-retire-after", ""))')"
      [[ "$ALLOW_LEGACY_SLOT_UNTIL" == "$recorded_deadline" ]] ||
        die "ALLOW_LEGACY_SLOT_UNTIL must equal the reviewed Git/live deadline: $recorded_deadline"
      [[ "$(date -u +%s)" -lt "$(date -u -d "$ALLOW_LEGACY_SLOT_UNTIL" +%s)" ]] || die "legacy slot rollback deadline expired"
      echo "WARNING: legacy slot retained only until $ALLOW_LEGACY_SLOT_UNTIL" >&2
    fi
  fi
  if [[ "$legacy_count" == "0" ]]; then
    view_bad="$(pg_scalar "SELECT count(*)=2 AND array_agg(table_name ORDER BY table_name)=ARRAY['recipe','recipe_ingredient']::text[] AND bool_and(COALESCE(cardinality(indices)=1 AND indices @> ARRAY['recipes_live']::text[],false)) FROM public._view")"
    [[ "$view_bad" == "t" ]] || die "public._view is not exact two-row recipes_live metadata"
  elif [[ "$legacy_policy" == "allow-retirement" ]]; then
    view_bad="$(pg_scalar "SELECT count(*)=2 AND array_agg(table_name ORDER BY table_name)=ARRAY['recipe','recipe_ingredient']::text[] AND (bool_and(COALESCE(cardinality(indices)=1 AND indices @> ARRAY['recipes_live']::text[],false)) OR bool_and(COALESCE(cardinality(indices)=2 AND indices @> ARRAY['recipes_live','recipes_pgsync']::text[],false))) FROM public._view")"
    [[ "$view_bad" == "t" ]] || die "public._view is neither exact retirement state A nor resumable state B"
  else
    view_bad="$(pg_scalar "SELECT count(*)=2 AND array_agg(table_name ORDER BY table_name)=ARRAY['recipe','recipe_ingredient']::text[] AND bool_and(COALESCE(cardinality(indices)=2 AND indices @> ARRAY['recipes_live','recipes_pgsync']::text[],false)) FROM public._view")"
    [[ "$view_bad" == "t" ]] || die "public._view rollback metadata is not exact two-row live+legacy state"
  fi
  view_shape="$(pg_scalar "SELECT bool_and(COALESCE(CASE table_name WHEN 'recipe' THEN primary_keys=ARRAY['id']::text[] AND foreign_keys IS NULL AND cardinality(columns)=13 AND columns @> ARRAY['id','source','name','category','cook_method','cooking_time','level_nm','serving','kcal','carb_g','protein_g','fat_g','image_url']::text[] WHEN 'recipe_ingredient' THEN primary_keys=ARRAY['id']::text[] AND cardinality(foreign_keys)=2 AND foreign_keys @> ARRAY['item_id','recipe_id']::text[] AND cardinality(columns)=5 AND columns @> ARRAY['id','recipe_id','ingredient_name','item_id','is_non_ingredient']::text[] ELSE false END,false)) FROM public._view")"
  [[ "$view_shape" == "t" ]] || die "public._view PK/FK/column payload differs from the exact schema contract"
  [[ "$(pg_scalar "SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid='public._view'::regclass")" == "$ROLE" ]] || die "public._view owner changed"
  [[ "$(pg_scalar "SELECT array_agg(t.tgname ORDER BY t.tgname)=ARRAY['public_recipe_ingredient_notify','public_recipe_ingredient_truncate','public_recipe_notify','public_recipe_truncate']::name[] AND bool_and(t.tgenabled='O') FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relname IN ('recipe','recipe_ingredient') AND NOT t.tgisinternal")" == "t" ]] || die "PGSync trigger names/state differ from exact contract"
  [[ "$(pg_scalar "SELECT has_table_privilege('fbapp','public._view','SELECT') AND has_table_privilege('pgsync','public._view','SELECT')")" == "t" ]] || die "public._view ACL missing"
  [[ "$(pg_scalar "SELECT string_agg(relname||'='||pg_get_userbyid(relowner),',' ORDER BY relname) FROM pg_class WHERE oid IN ('public.recipe'::regclass,'public.recipe_ingredient'::regclass)")" == "recipe=fbapp,recipe_ingredient=fbapp" ]] || die "recipe table ownership changed"
  expected_backing="${EXPECTED_BACKING:-recipes_v2}"
  matched=0
  for i in $(seq 1 15); do
    pg_count="$(pg_scalar "SELECT count(*) FROM public.recipe")"
    es_count="$(kubectl -n "$NAMESPACE" exec -i deployment/mp-pgsync -c pgsync -- python3 -c '
import base64,json,os,sys,urllib.request
contract=json.load(sys.stdin); base="http://es-es-http:9200"; expected=sys.argv[1]
auth=base64.b64encode((os.environ.get("ELASTICSEARCH_USER","elastic")+":"+os.environ["ELASTICSEARCH_PASSWORD"]).encode()).decode()
def call(path,method="GET",body=None):
 data=None if body is None else json.dumps(body).encode(); req=urllib.request.Request(base+path,data=data,method=method,headers={"Authorization":"Basic "+auth,"Content-Type":"application/json"})
 with urllib.request.urlopen(req,timeout=15) as r:return json.load(r)
health=call("/_cluster/health");
if health["status"] != "green": raise SystemExit("ES cluster is not green")
aliases=call("/_alias/recipes_live")
if len(aliases)!=1: raise SystemExit("recipes_live must have exactly one backing")
backing=next(iter(aliases)); cfg=aliases[backing]["aliases"]["recipes_live"]
if backing!=expected: raise SystemExit(f"unexpected backing: {backing} != {expected}")
if cfg.get("is_write_index") is not True: raise SystemExit("recipes_live lacks explicit is_write_index=true")
mapping=call("/"+backing+"/_mapping")[backing]["mappings"]["properties"]
if mapping != contract["mappings"]["properties"]: raise SystemExit("mapping differs from canonical exact contract")
settings=call("/"+backing+"/_settings")[backing]["settings"]["index"]
if settings.get("number_of_replicas")!="1": raise SystemExit("backing must have one replica")
if settings.get("number_of_shards")!="1": raise SystemExit("backing must have one primary shard")
if settings.get("analysis") != contract["settings"]["analysis"]: raise SystemExit("analysis differs from canonical exact contract")
tokens=[x["token"] for x in call("/recipes_live/_analyze","POST",{"analyzer":"korean","text":"김치찌개"})["tokens"]]
if not {"김치찌개","김치","찌개"}.issubset(tokens): raise SystemExit("nori mixed analyzer contract failed")
print(call("/recipes_live/_count")["count"])' "$expected_backing" <"$SCRIPT_DIR/recipes-index.json")"
    if [[ "$pg_count" == "$es_count" ]]; then
      matched=1
      break
    fi
    sleep 2
  done
  [[ "$matched" == "1" ]] || die "PG/ES count mismatch after bounded retry: $pg_count != $es_count"
  note "verification passed: PG=ES=$pg_count, stable alias/mapping/analyzer/role/slot/ACL healthy"
  echo "Write gate: PGSYNC_CONFIRM=RUN_RECIPES_LIVE_CDC_CRUD_E2E ops/pgsync-stable-alias/ops.sh crud"
}

bootstrap_lifecycle() {
  [[ "${PGSYNC_CONFIRM:-}" == "STOCK_BOOTSTRAP_DROPS_TARGET_SLOT_AND_TRIGGERS" ]] ||
    die "set PGSYNC_CONFIRM=STOCK_BOOTSTRAP_DROPS_TARGET_SLOT_AND_TRIGGERS"
  acquire_lock
  [[ "$(pg_scalar "SELECT count(*) FROM pg_replication_slots WHERE slot_name='$LIVE_SLOT'")" == "0" ]] ||
    die "$LIVE_SLOT already exists; generation swaps must not rerun stock bootstrap (use resume-acl only for an interrupted ACL step)"
  PGSYNC_CONFIRM=SET_RECIPES_LIVE_WRITE_INDEX alias_write_explicit
  ROLE_RECOVERY=1
  park_role
  delete_ephemeral
  activate_role
  assert_active_role
  assert_lock_owned
  kubectl -n "$NAMESPACE" scale deployment mp-pgsync --replicas=0 >/dev/null
  wait_daemon_stopped
  run_job "$BOOTSTRAP_JOB" "$SCRIPT_DIR/bootstrap-job.yaml" 15m
  create_script_configmap
  run_maintenance restore-acl
  recover_parked
  ROLE_RECOVERY=0
  kubectl -n "$NAMESPACE" rollout status deployment/mp-pgsync --timeout=5m
  verify_live allow-retirement
  run_crud_job
  verify_live allow-retirement
}

resume_acl_lifecycle() {
  local slot_contract
  [[ "${PGSYNC_CONFIRM:-}" == "RESUME_POST_BOOTSTRAP_ACL_ONLY" ]] ||
    die "set PGSYNC_CONFIRM=RESUME_POST_BOOTSTRAP_ACL_ONLY"
  acquire_lock
  slot_contract="$(pg_scalar "SELECT database='foodbudget' AND plugin='test_decoding' AND slot_type='logical' AND confirmed_flush_lsn IS NOT NULL AND restart_lsn IS NOT NULL FROM pg_replication_slots WHERE slot_name='$LIVE_SLOT'")"
  [[ "$slot_contract" == "t" ]] || die "resume-acl requires the exact completed live slot"
  [[ "$(pg_scalar "SELECT pg_get_userbyid(relowner)='$ROLE' FROM pg_class WHERE oid='public._view'::regclass")" == "t" ]] ||
    die "resume-acl requires public._view owned by $ROLE"
  [[ "$(pg_scalar "SELECT array_agg(t.tgname ORDER BY t.tgname)=ARRAY['public_recipe_ingredient_notify','public_recipe_ingredient_truncate','public_recipe_notify','public_recipe_truncate']::name[] AND bool_and(t.tgenabled='O') FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relname IN ('recipe','recipe_ingredient') AND NOT t.tgisinternal")" == "t" ]] ||
    die "resume-acl refuses an unexpected trigger contract"
  PGSYNC_CONFIRM=SET_RECIPES_LIVE_WRITE_INDEX alias_write_explicit
  ROLE_RECOVERY=1
  park_role
  delete_ephemeral
  activate_role
  assert_active_role
  create_script_configmap
  run_maintenance restore-acl
  recover_parked
  ROLE_RECOVERY=0
  verify_live allow-retirement
  run_crud_job
  verify_live allow-retirement
}

assert_retire_deadline() {
  local deadline="$1" local_deadline live_deadline deadline_epoch
  assert_lock_owned
  local_deadline="$(python3 - "$REPO_ROOT/platform/pgsync/schema-configmap.yaml" <<'PY'
import sys, yaml
doc = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
print(doc["metadata"]["annotations"]["operations.mealplanning.io/legacy-slot-retire-after"])
PY
)"
  live_deadline="$(kubectl -n "$NAMESPACE" get configmap mp-pgsync-schema -o json | python3 -c 'import json,sys; print((json.load(sys.stdin).get("metadata",{}).get("annotations",{})).get("operations.mealplanning.io/legacy-slot-retire-after", ""))')"
  [[ "$deadline" == "$local_deadline" && "$deadline" == "$live_deadline" ]] ||
    die "deadline must match CLI/local Git/live values: cli=$deadline local=$local_deadline live=$live_deadline"
  deadline_epoch="$(python3 - "$deadline" <<'PY'
import datetime, re, sys
value = sys.argv[1]
if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value):
    raise SystemExit("deadline must be strict UTC RFC3339")
print(int(datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()))
PY
)" || die "invalid strict UTC RFC3339 deadline"
  [[ "$(date -u +%s)" -ge "$deadline_epoch" ]] || die "rollback deadline has not elapsed"
}

retire_lifecycle() {
  local deadline="$1"
  [[ "${PGSYNC_CONFIRM:-}" == "RETIRE_RECIPES_PGSYNC_SLOT_AND_VIEW_ROUTE" ]] ||
    die "set PGSYNC_CONFIRM=RETIRE_RECIPES_PGSYNC_SLOT_AND_VIEW_ROUTE"
  acquire_lock
  assert_retire_deadline "$deadline"
  PGSYNC_CONFIRM=SET_RECIPES_LIVE_WRITE_INDEX alias_write_explicit
  ROLE_RECOVERY=1
  park_role
  delete_ephemeral
  # The old slot is non-transactional to delete. Prove the complete live read
  # path and a committed CDC CRUD cycle before granting the retirement identity.
  verify_live allow-retirement
  run_crud_job
  verify_live allow-retirement
  assert_lock_owned
  assert_retire_deadline "$deadline"
  activate_role
  assert_active_role
  create_script_configmap
  run_maintenance retire-legacy
  recover_parked
  ROLE_RECOVERY=0
  verify_live absent
  run_crud_job
  verify_live absent
}

crud_lifecycle() {
  [[ "${PGSYNC_CONFIRM:-}" == "RUN_RECIPES_LIVE_CDC_CRUD_E2E" ]] ||
    die "set PGSYNC_CONFIRM=RUN_RECIPES_LIVE_CDC_CRUD_E2E"
  acquire_lock
  kubectl -n "$NAMESPACE" get configmap "$SCRIPT_CONFIGMAP" >/dev/null 2>&1 &&
    die "stale maintenance ConfigMap exists; run cleanup and inspect first"
  kubectl -n "$NAMESPACE" get job "$CRUD_JOB" >/dev/null 2>&1 &&
    die "stale CRUD Job exists; run cleanup and inspect first"
  verify_live absent
  run_crud_job
  verify_live absent
}

alias_write_lifecycle() {
  acquire_lock
  PGSYNC_CONFIRM=SET_RECIPES_LIVE_WRITE_INDEX alias_write_explicit
}

prepare_index_lifecycle() {
  local index="$1"
  acquire_lock
  [[ "$(kubectl -n "$NAMESPACE" get deployment mp-pgsync -o jsonpath='{.status.readyReplicas}')" == "1" ]] ||
    die "prepare-index requires one ready PGSync daemon"
  prepare_index "$index"
}

cleanup_lifecycle() {
  acquire_lock
  ROLE_RECOVERY=1
  recover_parked
  ROLE_RECOVERY=0
}

usage() {
  cat <<'EOF'
usage: ops.sh <command>
  prepare-index <recipes_vN>  create an immutable generation from recipes-index.json
  alias-write                idempotently set explicit is_write_index=true
  bootstrap                  first/schema bootstrap only; refuses an existing stable slot
  resume-acl                 resume only the post-bootstrap _view ACL step
  retire <rollback-deadline> resumable two-phase old slot + _view retirement
  cleanup                    recovery: PARK role, delete exact ephemeral resources, restore daemon
  crud                       executable fbapp INSERT/UPDATE/DELETE CDC E2E
  verify                     read-only final gate
EOF
}

main() {
  install_traps
  require_tools
  assert_local_role_parked
  case "${1:-}" in
    prepare-index) [[ $# == 2 ]] || die "prepare-index needs recipes_vN"; prepare_index_lifecycle "$2" ;;
    alias-write) [[ $# == 1 ]] || die "alias-write takes no argument"; alias_write_lifecycle ;;
    bootstrap) [[ $# == 1 ]] || die "bootstrap takes no argument"; bootstrap_lifecycle ;;
    resume-acl) [[ $# == 1 ]] || die "resume-acl takes no argument"; resume_acl_lifecycle ;;
    retire) [[ $# == 2 ]] || die "retire needs an RFC3339/UTC deadline"; retire_lifecycle "$2" ;;
    cleanup) [[ $# == 1 ]] || die "cleanup takes no argument"; cleanup_lifecycle ;;
    crud) [[ $# == 1 ]] || die "crud takes no argument"; crud_lifecycle ;;
    verify) [[ $# == 1 ]] || die "verify takes no argument"; verify_live ;;
    *) usage; exit 2 ;;
  esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
