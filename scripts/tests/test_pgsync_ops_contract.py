from __future__ import annotations

import pathlib
import re
import subprocess
import unittest


REPO = pathlib.Path(__file__).resolve().parents[2]
OPS_PATH = REPO / "ops" / "pgsync-stable-alias" / "ops.sh"
SOURCE = OPS_PATH.read_text(encoding="utf-8")


def function_body(name: str) -> str:
    start_match = re.search(rf"(?m)^{re.escape(name)}\(\) \{{\n", SOURCE)
    if start_match is None:
        raise AssertionError(f"missing bash function: {name}")
    next_match = re.search(r"(?m)^[a-zA-Z_][a-zA-Z0-9_]*\(\) \{\n", SOURCE[start_match.end():])
    end = len(SOURCE) if next_match is None else start_match.end() + next_match.start()
    return SOURCE[start_match.end():end]


def assert_in_order(test: unittest.TestCase, body: str, markers: list[str]) -> None:
    offset = 0
    for marker in markers:
        found = body.find(marker, offset)
        test.assertNotEqual(-1, found, f"missing/out-of-order marker: {marker}")
        offset = found + len(marker)


class LifecycleOrderingTests(unittest.TestCase):
    def test_retirement_proves_cdc_before_and_after_nontransactional_drop(self) -> None:
        body = function_body("retire_lifecycle")
        assert_in_order(self, body, [
            "acquire_lock",
            "alias_write_explicit",
            "park_role",
            "delete_ephemeral",
            "verify_live allow-retirement",
            "run_crud_job",
            "verify_live allow-retirement",
            "activate_role",
            "run_maintenance retire-legacy",
            "recover_parked",
            "verify_live absent",
            "run_crud_job",
            "verify_live absent",
        ])

    def test_bootstrap_locks_and_refuses_existing_slot_before_mutation(self) -> None:
        body = function_body("bootstrap_lifecycle")
        assert_in_order(self, body, [
            "acquire_lock",
            "SELECT count(*) FROM pg_replication_slots",
            "alias_write_explicit",
            "park_role",
            "activate_role",
            "scale deployment mp-pgsync --replicas=0",
        ])
        self.assertIn("use resume-acl", body)

    def test_every_mutating_entrypoint_acquires_the_lease(self) -> None:
        for name in (
            "bootstrap_lifecycle",
            "resume_acl_lifecycle",
            "retire_lifecycle",
            "crud_lifecycle",
            "alias_write_lifecycle",
            "prepare_index_lifecycle",
            "cleanup_lifecycle",
        ):
            with self.subTest(name=name):
                self.assertIn("acquire_lock", function_body(name))

    def test_namespace_is_fixed_and_not_partially_overridable(self) -> None:
        self.assertIn('NAMESPACE="data"', SOURCE)
        self.assertNotIn("PGSYNC_NAMESPACE", SOURCE)

    def test_alias_write_does_not_depend_on_the_daemon(self) -> None:
        body = function_body("alias_write_explicit")
        self.assertIn("run_es_alias_job", body)
        self.assertNotIn("exec deployment/mp-pgsync", body)

    def test_existing_generation_must_be_empty_and_unaliased(self) -> None:
        body = function_body("prepare_index")
        self.assertIn('call("/"+index+"/_count")["count"] != 0', body)
        self.assertIn('aliases.get(index,{}).get("aliases")', body)

    def test_retirement_deadline_must_equal_live_git_record(self) -> None:
        body = function_body("assert_retire_deadline")
        self.assertIn("operations.mealplanning.io/legacy-slot-retire-after", body)
        self.assertIn('[[ "$deadline" == "$local_deadline"', body)
        self.assertIn('&& "$deadline" == "$live_deadline"', body)

    def test_retirement_gate_accepts_exact_resumable_state_b(self) -> None:
        body = function_body("verify_live")
        self.assertIn('elif [[ "$legacy_policy" == "allow-retirement" ]]', body)
        self.assertIn("neither exact retirement state A nor resumable state B", body)
        self.assertIn("COALESCE(cardinality(indices)=1", body)

    def test_lease_uses_microtime_and_preconditioned_release(self) -> None:
        self.assertIn('timespec="microseconds"', function_body("acquire_lock"))
        body = function_body("release_lock")
        self.assertIn('"kind": "DeleteOptions"', body)
        self.assertIn('metadata["uid"]', body)
        self.assertIn('metadata["resourceVersion"]', body)
        self.assertIn("kubectl delete --raw", body)


class FailureCleanupTests(unittest.TestCase):
    def test_crud_job_failure_still_deletes_job_and_configmap(self) -> None:
        bash = r'''
source "$1"
set +e
assert_lock_owned() { return 0; }
create_script_configmap() { return 0; }
run_job() { return 42; }
KUBECTL_CALLS=""
kubectl() { KUBECTL_CALLS="${KUBECTL_CALLS}|$*"; return 0; }
run_crud_job
rc=$?
printf 'RC=%s EPHEMERAL=%s CALLS=%s\n' "$rc" "$EPHEMERAL_CLEANUP" "$KUBECTL_CALLS"
exit 0
'''
        result = subprocess.run(
            ["bash", "-c", bash, "bash", str(OPS_PATH)],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("delete job mp-pgsync-crud-verify --ignore-not-found", result.stdout)
        self.assertIn("delete configmap mp-pgsync-maintenance-script --ignore-not-found", result.stdout)
        self.assertIn("RC=42 EPHEMERAL=1", result.stdout)


if __name__ == "__main__":
    unittest.main()
