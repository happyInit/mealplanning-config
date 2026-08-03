from __future__ import annotations

import copy
import unittest

from scripts.validate import Result, check_cnpg_failsafe_netpol


INSTANCE_SELECTOR = {
    "matchLabels": {
        "cnpg.io/cluster": "pg",
        "cnpg.io/podRole": "instance",
    },
}
INSTANCE_PEER = {"podSelector": INSTANCE_SELECTOR}
OPERATOR_PEER = {
    "namespaceSelector": {
        "matchLabels": {"kubernetes.io/metadata.name": "cnpg-system"},
    },
}
KUBELET_PEER = {"ipBlock": {"cidr": "192.168.0.0/24"}}


def tcp_port(port: int) -> dict:
    return {"protocol": "TCP", "port": port}


def cilium_tcp_port(port: int) -> dict:
    return {"protocol": "TCP", "port": str(port)}


def good_network_policy() -> dict:
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": "mp-pg-instance", "namespace": "data"},
        "spec": {
            "podSelector": copy.deepcopy(INSTANCE_SELECTOR),
            "policyTypes": ["Ingress", "Egress"],
            "ingress": [
                {
                    "from": [copy.deepcopy(INSTANCE_PEER)],
                    "ports": [tcp_port(5432), tcp_port(8000)],
                },
                {
                    "from": [copy.deepcopy(OPERATOR_PEER)],
                    "ports": [tcp_port(5432), tcp_port(8000)],
                },
                {"from": [copy.deepcopy(KUBELET_PEER)]},
            ],
            "egress": [
                {
                    "to": [copy.deepcopy(INSTANCE_PEER)],
                    "ports": [tcp_port(5432), tcp_port(8000)],
                },
            ],
        },
    }


def good_cilium_policy() -> dict:
    return {
        "apiVersion": "cilium.io/v2",
        "kind": "CiliumNetworkPolicy",
        "metadata": {"name": "mp-pg-instance-egress", "namespace": "data"},
        "spec": {
            "endpointSelector": copy.deepcopy(INSTANCE_SELECTOR),
            "egress": [
                {
                    "toEntities": ["kube-apiserver"],
                    "toPorts": [{"ports": [cilium_tcp_port(443), cilium_tcp_port(6443)]}],
                },
            ],
        },
    }


def good_docs() -> dict[str, list[dict]]:
    return {"fixture": [good_network_policy(), good_cilium_policy()]}


def service_account_bypass(name: str, field: str) -> dict:
    rule = {
        "endpointSelector": {
            "matchLabels": {"io.cilium.k8s.policy.serviceaccount": "pg"},
        },
        "egress": [{"toEntities": ["world"]}],
    }
    policy = {
        "apiVersion": "cilium.io/v2",
        "kind": "CiliumNetworkPolicy",
        "metadata": {"name": name, "namespace": "data"},
    }
    policy[field] = rule if field == "spec" else [rule]
    return policy


class CnpgFailsafeNetworkPolicyTests(unittest.TestCase):
    def failures(self, docs: dict[str, list[dict]]) -> list[str]:
        result = Result()
        check_cnpg_failsafe_netpol(result, docs)
        return result.failures

    def assert_passes(self, docs: dict[str, list[dict]]) -> None:
        failures = self.failures(docs)
        self.assertEqual([], failures, "\n".join(failures))

    def assert_fails_with(self, docs: dict[str, list[dict]], text: str) -> None:
        failures = self.failures(docs)
        self.assertTrue(any(text in failure for failure in failures), "\n".join(failures))

    def test_exact_manifest_passes(self) -> None:
        self.assert_passes(good_docs())

    def test_role_primary_additive_network_policy_fails(self) -> None:
        docs = good_docs()
        docs["fixture"].append({
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": "primary-bypass", "namespace": "data"},
            "spec": {
                "podSelector": {"matchLabels": {"role": "primary"}},
                "policyTypes": ["Ingress"],
                "ingress": [{"from": [{"namespaceSelector": {}}]}],
            },
        })
        self.assert_fails_with(docs, "primary-bypass.ingress[0]")

    def test_cilium_spec_ports_omitted_fails(self) -> None:
        docs = good_docs()
        docs["fixture"][1]["spec"]["egress"].append({"toEntities": ["world"]})
        self.assert_fails_with(docs, "mp-pg-instance-egress.spec.egress[1]")

    def test_cilium_specs_ports_omitted_fails(self) -> None:
        docs = good_docs()
        docs["fixture"].append({
            "apiVersion": "cilium.io/v2",
            "kind": "CiliumNetworkPolicy",
            "metadata": {"name": "specs-bypass", "namespace": "data"},
            "specs": [{
                "endpointSelector": {"matchLabels": {"role": "primary"}},
                "egress": [{"toEntities": ["world"]}],
            }],
        })
        self.assert_fails_with(docs, "specs-bypass.specs[0].egress[0]")

    def test_cilium_service_account_spec_ports_omitted_fails(self) -> None:
        docs = good_docs()
        docs["fixture"].append(service_account_bypass("service-account-spec-bypass", "spec"))
        self.assert_fails_with(docs, "service-account-spec-bypass.spec.egress[0]")

    def test_cilium_service_account_specs_ports_omitted_fails(self) -> None:
        docs = good_docs()
        docs["fixture"].append(service_account_bypass("service-account-specs-bypass", "specs"))
        self.assert_fails_with(docs, "service-account-specs-bypass.specs[0].egress[0]")

    def test_intra_end_port_fails(self) -> None:
        docs = good_docs()
        docs["fixture"][0]["spec"]["ingress"][0]["ports"][1]["endPort"] = 8001
        self.assert_fails_with(docs, "추가 key/range 없이")

    def test_unrelated_additive_policies_do_not_false_positive(self) -> None:
        docs = good_docs()
        docs["fixture"].extend([
            {
                "apiVersion": "networking.k8s.io/v1",
                "kind": "NetworkPolicy",
                "metadata": {"name": "unrelated-k8s", "namespace": "data"},
                "spec": {
                    "podSelector": {"matchLabels": {"app": "unrelated"}},
                    "policyTypes": ["Ingress"],
                    "ingress": [{"from": [{"namespaceSelector": {}}]}],
                },
            },
            {
                "apiVersion": "cilium.io/v2",
                "kind": "CiliumNetworkPolicy",
                "metadata": {"name": "unrelated-cilium", "namespace": "data"},
                "specs": [{
                    "endpointSelector": {"matchLabels": {"app": "unrelated"}},
                    "egress": [{"toEntities": ["world"]}],
                }],
            },
        ])
        self.assert_passes(docs)

    def test_exact_named_cilium_specs_manifest_passes(self) -> None:
        docs = good_docs()
        cilium = docs["fixture"][1]
        cilium["specs"] = [cilium.pop("spec")]
        self.assert_passes(docs)

    def test_cilium_spec_and_specs_is_rejected(self) -> None:
        docs = good_docs()
        cilium = docs["fixture"][1]
        cilium["specs"] = [copy.deepcopy(cilium["spec"])]
        self.assert_fails_with(docs, "spec 과 specs 를 동시에")


if __name__ == "__main__":
    unittest.main()
