from __future__ import annotations

import pathlib
import unittest

import yaml


REPO = pathlib.Path(__file__).resolve().parents[1]


def load_documents(path: pathlib.Path) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")) if doc]


class DataTierAlertContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        docs = load_documents(REPO / "monitoring" / "base" / "rules-data-tier.yaml")
        cls.rule = next(doc for doc in docs if doc["kind"] == "PrometheusRule")
        cls.groups = {
            group["name"]: group["rules"] for group in cls.rule["spec"]["groups"]
        }
        cls.alerts = {
            rule["alert"]: rule
            for rules in cls.groups.values()
            for rule in rules
            if "alert" in rule
        }

    def test_required_t4_alerts_exist(self) -> None:
        self.assertEqual(
            {
                "MpElasticsearchMetricsUnavailable",
                "MpESClusterYellow",
                "MpESClusterRed",
                "MpESVolumeMetricsUnavailable",
                "MpESDiskHigh",
                "MpKafkaMetricsUnavailable",
                "MpKafkaBrokerDown",
                "MpKafkaISRShrink",
                "MpMinIOVolumeMetricsUnavailable",
                "MpMinIODiskHigh",
                "MpPGReplicationSlotRetainedWALWarning",
                "MpPGReplicationSlotWALGrowing",
            },
            set(self.alerts),
        )

    def test_alerts_use_live_metric_contracts(self) -> None:
        expressions = {name: rule["expr"] for name, rule in self.alerts.items()}
        self.assertIn("elasticsearch_cluster_health_status", expressions["MpESClusterRed"])
        self.assertIn("elasticsearch_cluster_health_status", expressions["MpESClusterYellow"])
        self.assertIn("elasticsearch_cluster_health_status", expressions["MpElasticsearchMetricsUnavailable"])
        self.assertIn("kafka_brokers", expressions["MpKafkaBrokerDown"])
        self.assertIn("kafka_brokers", expressions["MpKafkaMetricsUnavailable"])
        self.assertIn(
            "kafka_topic_partition_under_replicated_partition",
            expressions["MpKafkaMetricsUnavailable"],
        )
        self.assertIn(
            "kafka_topic_partition_under_replicated_partition",
            expressions["MpKafkaISRShrink"],
        )
        self.assertIn("persistentvolumeclaim=~\"elasticsearch-data-es-.*\"", expressions["MpESDiskHigh"])
        self.assertIn("persistentvolumeclaim=\"minio\"", expressions["MpMinIODiskHigh"])
        self.assertIn("absent_over_time", expressions["MpESVolumeMetricsUnavailable"])
        self.assertIn("absent_over_time", expressions["MpMinIOVolumeMetricsUnavailable"])
        self.assertIn(
            'job="mp-elasticsearch-exporter"',
            expressions["MpElasticsearchMetricsUnavailable"],
        )
        self.assertNotIn(
            'job="data/mp-elasticsearch-exporter"',
            expressions["MpElasticsearchMetricsUnavailable"],
        )

    def test_existing_pipeline_lag_alert_is_not_duplicated(self) -> None:
        self.assertNotIn("MpKafkaConsumerLagHigh", self.alerts)
        pipeline = (REPO / "pipelines" / "base" / "monitoring.yaml").read_text(encoding="utf-8")
        self.assertIn("alert: MpConsumerBacklogStuck", pipeline)
        self.assertIn("alert: MpConsumerIdleWithBacklog", pipeline)

    def test_critical_is_reserved_for_data_or_user_impact(self) -> None:
        self.assertEqual("critical", self.alerts["MpESClusterRed"]["labels"]["severity"])
        self.assertEqual("critical", self.alerts["MpKafkaISRShrink"]["labels"]["severity"])
        for name in (
            "MpElasticsearchMetricsUnavailable",
            "MpESClusterYellow",
            "MpESVolumeMetricsUnavailable",
            "MpESDiskHigh",
            "MpKafkaMetricsUnavailable",
            "MpKafkaBrokerDown",
            "MpMinIOVolumeMetricsUnavailable",
            "MpMinIODiskHigh",
            "MpPGReplicationSlotRetainedWALWarning",
            "MpPGReplicationSlotWALGrowing",
        ):
            self.assertEqual("warning", self.alerts[name]["labels"]["severity"])


class ElasticsearchExporterContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        docs = load_documents(REPO / "platform" / "es" / "base" / "monitoring.yaml")
        cls.by_kind = {doc["kind"]: doc for doc in docs}
        cls.deployment = cls.by_kind["Deployment"]
        cls.container = cls.deployment["spec"]["template"]["spec"]["containers"][0]

    def test_image_is_immutable_and_runtime_uses_monitor_only_credentials(self) -> None:
        self.assertEqual(
            "quay.io/prometheuscommunity/elasticsearch-exporter:v1.11.0@"
            "sha256:a056739b095df4baaa076f0b31321394233da9a240eeada2623d1b028b7ee7a6",
            self.container["image"],
        )
        env = {item["name"]: item for item in self.container["env"]}
        self.assertEqual("mp_elasticsearch_exporter", env["ES_USERNAME"]["value"])
        self.assertEqual(
            {"name": "mp-elasticsearch-exporter-auth", "key": "ES_PASSWORD"},
            env["ES_PASSWORD"]["valueFrom"]["secretKeyRef"],
        )
        self.assertNotIn("envFrom", self.container)
        self.assertNotIn("elastic:", " ".join(self.container["args"]))
        self.assertIn("--es.uri=http://es-es-http.data.svc:9200", self.container["args"])
        self.assertNotIn("--es.aliases=false", self.container["args"])
        self.assertNotIn("initContainers", self.deployment["spec"]["template"]["spec"])
        job = self.by_kind["Job"]
        bootstrap = job["spec"]["template"]["spec"]["containers"][0]
        bootstrap_env = {item["name"]: item for item in bootstrap["env"]}
        self.assertEqual(
            {"name": "es-es-elastic-user", "key": "elastic"},
            bootstrap_env["ELASTICSEARCH_PASSWORD"]["valueFrom"]["secretKeyRef"],
        )
        self.assertEqual(
            {"name": "mp-elasticsearch-exporter-auth", "key": "ES_PASSWORD"},
            bootstrap_env["EXPORTER_PASSWORD"]["valueFrom"]["secretKeyRef"],
        )
        script = self.by_kind["ConfigMap"]["data"]["bootstrap.py"]
        self.assertIn('"cluster": ["monitor"]', script)
        self.assertIn('"indices": []', script)
        self.assertIn('"roles": [ROLE]', script)
        self.assertNotIn("/_security/api_key", script)
        self.assertNotIn("kubernetes.default.svc", script)
        self.assertEqual("Sync", job["metadata"]["annotations"]["argocd.argoproj.io/hook"])

    def test_exporter_is_restricted_and_scraped_conservatively(self) -> None:
        pod = self.deployment["spec"]["template"]["spec"]
        self.assertTrue(pod["securityContext"]["runAsNonRoot"])
        self.assertEqual("RuntimeDefault", pod["securityContext"]["seccompProfile"]["type"])
        security = self.container["securityContext"]
        self.assertEqual(1000, security["runAsUser"])
        self.assertFalse(security["allowPrivilegeEscalation"])
        self.assertTrue(security["readOnlyRootFilesystem"])
        self.assertEqual(["ALL"], security["capabilities"]["drop"])
        bootstrap_security = self.by_kind["Job"]["spec"]["template"]["spec"]["containers"][0]["securityContext"]
        self.assertFalse(bootstrap_security["allowPrivilegeEscalation"])
        self.assertTrue(bootstrap_security["readOnlyRootFilesystem"])
        self.assertEqual(["ALL"], bootstrap_security["capabilities"]["drop"])
        monitor = self.by_kind["ServiceMonitor"]
        endpoint = monitor["spec"]["endpoints"][0]
        self.assertEqual("60s", endpoint["interval"])
        self.assertEqual("15s", endpoint["scrapeTimeout"])
        self.assertEqual("/metrics", endpoint["path"])
        self.assertNotIn("params", endpoint)

    def test_exporter_network_policy_is_least_privilege(self) -> None:
        policy = yaml.safe_load(
            (REPO / "platform" / "policies-data" / "base" / "netpol-es-exporter.yaml").read_text()
        )
        self.assertEqual(
            {"matchLabels": {"app": "elasticsearch-exporter"}},
            policy["spec"]["podSelector"],
        )
        self.assertEqual(["Ingress", "Egress"], policy["spec"]["policyTypes"])
        self.assertEqual(1, len(policy["spec"]["ingress"]))
        self.assertEqual(2, len(policy["spec"]["egress"]))
        bootstrap_docs = load_documents(
            REPO / "platform" / "policies-data" / "base" / "netpol-es-exporter-bootstrap.yaml"
        )
        self.assertEqual(1, len(bootstrap_docs))
        bootstrap_policy = bootstrap_docs[0]
        self.assertEqual([], bootstrap_policy["spec"]["ingress"])

    def test_credentials_are_eso_generated_once(self) -> None:
        password = self.by_kind["Password"]
        self.assertEqual(["ES_PASSWORD"], password["spec"]["secretKeys"])
        self.assertEqual(48, password["spec"]["length"])
        external_secret = self.by_kind["ExternalSecret"]
        self.assertEqual("CreatedOnce", external_secret["spec"]["refreshPolicy"])
        self.assertEqual(
            "mp-elasticsearch-exporter-auth",
            external_secret["spec"]["target"]["name"],
        )
        generator = external_secret["spec"]["dataFrom"][0]["sourceRef"]["generatorRef"]
        self.assertEqual(
            {
                "apiVersion": "generators.external-secrets.io/v1alpha1",
                "kind": "Password",
                "name": "mp-elasticsearch-exporter-password",
            },
            generator,
        )


if __name__ == "__main__":
    unittest.main()
