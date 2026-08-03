from __future__ import annotations

import copy
import importlib.util
import pathlib
import unittest

import yaml


REPO = pathlib.Path(__file__).resolve().parents[2]
VALIDATE_PATH = REPO / "scripts" / "validate.py"
OPS_DIR = REPO / "ops" / "pgsync-stable-alias"


def load_validate_module():
    spec = importlib.util.spec_from_file_location("mp_validate", VALIDATE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load scripts/validate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_job(name: str) -> dict:
    return yaml.safe_load((OPS_DIR / name).read_text(encoding="utf-8"))


class ManualJobSecurityContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.validate = load_validate_module()

    def assert_contract_passes(self, job: dict) -> None:
        result = self.validate.Result()
        self.validate.check_manual_job_security(result, "fixture", job)
        self.assertEqual([], result.failures)

    def assert_contract_fails(self, job: dict, expected: str) -> None:
        result = self.validate.Result()
        self.validate.check_manual_job_security(result, "fixture", job)
        self.assertTrue(
            any(expected in failure for failure in result.failures),
            result.failures,
        )

    def test_all_manual_jobs_are_restricted(self) -> None:
        for filename in (
            "bootstrap-job.yaml",
            "maintenance-job.yaml",
            "crud-verify-job.yaml",
            "es-maintenance-job.yaml",
        ):
            with self.subTest(filename=filename):
                self.assert_contract_passes(load_job(filename))

    def test_rejects_root_pod_identity(self) -> None:
        job = copy.deepcopy(load_job("maintenance-job.yaml"))
        job["spec"]["template"]["spec"]["securityContext"]["runAsUser"] = 0
        self.assert_contract_fails(job, "pod securityContext")

    def test_rejects_writable_container_root(self) -> None:
        job = copy.deepcopy(load_job("crud-verify-job.yaml"))
        container = job["spec"]["template"]["spec"]["containers"][0]
        container["securityContext"]["readOnlyRootFilesystem"] = False
        self.assert_contract_fails(job, "container securityContext")

    def test_rejects_container_identity_or_seccomp_override(self) -> None:
        for field, value in (
            ("runAsUser", 0),
            ("runAsGroup", 0),
            ("runAsNonRoot", False),
            ("seccompProfile", {"type": "Unconfined"}),
        ):
            with self.subTest(field=field):
                job = copy.deepcopy(load_job("crud-verify-job.yaml"))
                container = job["spec"]["template"]["spec"]["containers"][0]
                container["securityContext"][field] = value
                self.assert_contract_fails(job, "container securityContext")

    def test_rejects_added_capability(self) -> None:
        job = copy.deepcopy(load_job("crud-verify-job.yaml"))
        capabilities = job["spec"]["template"]["spec"]["containers"][0][
            "securityContext"
        ]["capabilities"]
        capabilities["add"] = ["SYS_ADMIN"]
        self.assert_contract_fails(job, "container securityContext")

    def test_rejects_unhardened_init_container(self) -> None:
        job = copy.deepcopy(load_job("bootstrap-job.yaml"))
        init = job["spec"]["template"]["spec"]["initContainers"][0]
        init.pop("securityContext")
        self.assert_contract_fails(job, "initContainer securityContext")

    def test_rejects_missing_tmp_emptydir_or_mount(self) -> None:
        job = copy.deepcopy(load_job("maintenance-job.yaml"))
        pod = job["spec"]["template"]["spec"]
        pod["volumes"] = [volume for volume in pod["volumes"] if volume["name"] != "tmp"]
        self.assert_contract_fails(job, "writable /tmp emptyDir")

        job = copy.deepcopy(load_job("maintenance-job.yaml"))
        mount = job["spec"]["template"]["spec"]["containers"][0]["volumeMounts"][-1]
        mount["readOnly"] = True
        self.assert_contract_fails(job, "writable /tmp mount")

    def test_rejects_missing_bootstrap_writable_workdirs(self) -> None:
        job = copy.deepcopy(load_job("bootstrap-job.yaml"))
        pod = job["spec"]["template"]["spec"]
        pod["volumes"] = [
            volume for volume in pod["volumes"] if volume["name"] != "checkpoint"
        ]
        self.assert_contract_fails(job, "bootstrap writable volumes")


class EsMaintenanceJobContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.validate = load_validate_module()

    def assert_contract_fails(self, job: dict, expected: str) -> None:
        result = self.validate.Result()
        self.validate.check_es_maintenance_job(result, job)
        self.assertTrue(
            any(expected in failure for failure in result.failures),
            result.failures,
        )

    def test_clean_es_job_passes(self) -> None:
        result = self.validate.Result()
        self.validate.check_es_maintenance_job(
            result,
            load_job("es-maintenance-job.yaml"),
        )
        self.assertEqual([], result.failures)

    def test_rejects_envfrom_secret_bypass(self) -> None:
        job = copy.deepcopy(load_job("es-maintenance-job.yaml"))
        container = job["spec"]["template"]["spec"]["containers"][0]
        container["envFrom"] = [{"secretRef": {"name": "pg-app"}}]
        self.assert_contract_fails(job, "envFrom")

    def test_rejects_init_container_secret_bypass(self) -> None:
        job = copy.deepcopy(load_job("es-maintenance-job.yaml"))
        pod = job["spec"]["template"]["spec"]
        pod["initContainers"] = [{
            "name": "secret-reader",
            "image": pod["containers"][0]["image"],
            "env": [{
                "name": "PG_PASSWORD",
                "valueFrom": {"secretKeyRef": {"name": "pg-app", "key": "password"}},
            }],
        }]
        self.assert_contract_fails(job, "initContainer")


class LegacyRetireAnnotationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.validate = load_validate_module()

    def test_rfc3339_timestamp_requires_timezone(self) -> None:
        self.assertTrue(self.validate.is_rfc3339_timestamp("2026-08-10T12:30:00Z"))
        self.assertTrue(self.validate.is_rfc3339_timestamp("2026-08-10T21:30:00+09:00"))
        self.assertFalse(self.validate.is_rfc3339_timestamp("2026-08-10T12:30:00"))
        self.assertFalse(self.validate.is_rfc3339_timestamp("2026-02-30T12:30:00Z"))


class PgsyncEgressPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.validate = load_validate_module()
        cls.policy = yaml.safe_load(
            (REPO / "platform" / "policies-data" / "netpol-pgsync.yaml").read_text()
        )

    def failures(self, policy: dict) -> list[str]:
        result = self.validate.Result()
        self.validate.check_pgsync_egress_policy(result, policy)
        return result.failures

    def test_exact_policy_passes(self) -> None:
        self.assertEqual([], self.failures(self.policy))

    def test_rejects_additive_world_egress(self) -> None:
        policy = copy.deepcopy(self.policy)
        policy["spec"]["egress"].append({"to": [{"ipBlock": {"cidr": "0.0.0.0/0"}}]})
        self.assertTrue(self.failures(policy))

    def test_rejects_all_ports_to_postgres(self) -> None:
        policy = copy.deepcopy(self.policy)
        policy["spec"]["egress"][1].pop("ports")
        self.assertTrue(self.failures(policy))


if __name__ == "__main__":
    unittest.main()
