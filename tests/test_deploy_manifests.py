"""Invariants of the v0.9 deploy files that kubeconform cannot see (no cluster, no AWS).

kubeconform (``make infra-static``) checks the schema; these tests check the design
decisions the runbook and the plan pin, so a later edit that breaks one fails ``make
check`` instead of a demo: the WCI stays constrained to ``agents``, images are 1.32.1,
internode certificates carry both EKUs, every client certificate has its own CN and
only ``client auth``, the frontend certificate covers every name a client dials, the
NetworkPolicies select the labels chart 1.7.0 actually puts on the frontend pods, and
the kind envconfig profile checks the Route 53 name.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
ROUTE53_NAME = "temporal-frontend.temporal-customer-support.internal"
SVC_NAME = "temporal-frontend.temporal.svc.cluster.local"


def _docs(path: Path) -> list[dict[str, Any]]:
    return [d for d in yaml.safe_load_all(path.read_text()) if d]


def _values(name: str) -> dict[str, Any]:
    return yaml.safe_load((DEPLOY / "temporal" / name).read_text())


def _certificates() -> dict[str, dict[str, Any]]:
    certs: dict[str, dict[str, Any]] = {}
    for path in (DEPLOY / "k8s" / "base").rglob("*.yaml"):
        for doc in _docs(path):
            if doc.get("kind") == "Certificate":
                certs[doc["metadata"]["name"]] = doc
    return certs


def test_wci_is_constrained_to_agents_only() -> None:
    dc = _values("values.base.yaml")["server"]["dynamicConfig"]
    assert dc["workercontroller.enabled"] == [
        {"value": True, "constraints": {"namespace": "agents"}}
    ]
    assert dc["workercontroller.compute_providers.enabled"] == [{"value": ["aws-agentcore"]}]
    assert dc["workercontroller.scaling_algorithms.enabled"] == [{"value": ["no-sync"]}]
    # Removed in 1.31 / 1.32: must never come back (runbook Phase 3).
    assert "system.enableNexus" not in dc
    assert "component.nexusoperations.useSystemCallbackURL" not in dc


def test_images_and_namespaces_come_from_values() -> None:
    base = _values("values.base.yaml")
    assert base["server"]["image"]["tag"] == "1.32.1"
    assert base["admintools"]["image"]["tag"] == "1.32.1"
    assert base["web"]["image"]["tag"] == "2.54.1"
    names = [n["name"] for n in base["server"]["config"]["namespaces"]["namespace"]]
    assert {"agents", "agent-services"} <= set(names)


def test_kind_shards_and_tls_values() -> None:
    assert _values("values.kind.yaml")["server"]["config"]["persistence"]["numHistoryShards"] == 4
    tls = _values("values.tls.yaml")
    for section in ("internode", "frontend"):
        assert tls["server"]["tls"][section]["enabled"] is True
        assert tls["server"]["tls"][section]["requireClientAuth"] is True
    assert tls["server"]["tls"]["frontend"]["serverName"] == ROUTE53_NAME
    assert tls["web"]["tls"] == {
        "enabled": True,
        "secretName": "temporal-web-client-tls",
        "serverName": ROUTE53_NAME,
        "enableHostVerification": True,
    }
    # The chart's frontend clientCaFiles is <mount>/ca.crt of temporal-frontend-tls only
    # (runbook 2.5): every certificate must come from the one temporal-ca issuer.
    for name, cert in _certificates().items():
        if name == "temporal-root-ca":
            continue
        assert cert["spec"]["issuerRef"] == {
            "name": "temporal-ca",
            "kind": "ClusterIssuer",
            "group": "cert-manager.io",
        }, name


def test_root_ca_and_issuers_follow_runbook_2_6() -> None:
    docs = _docs(DEPLOY / "k8s" / "base" / "cert-issuers" / "issuers.yaml")
    kinds = {(d["kind"], d["metadata"]["name"]): d for d in docs}
    assert kinds[("ClusterIssuer", "selfsigned-bootstrap")]["spec"] == {"selfSigned": {}}
    root = kinds[("Certificate", "temporal-root-ca")]
    assert root["metadata"]["namespace"] == "cert-manager"
    assert root["spec"]["isCA"] is True
    assert root["spec"]["issuerRef"]["name"] == "selfsigned-bootstrap"
    assert kinds[("ClusterIssuer", "temporal-ca")]["spec"] == {
        "ca": {"secretName": "temporal-root-ca"}
    }


def test_server_certificates() -> None:
    certs = _certificates()
    frontend = certs["temporal-frontend-tls"]["spec"]
    assert {ROUTE53_NAME, SVC_NAME, "temporal-frontend.temporal.svc"} <= set(frontend["dnsNames"])
    # Chart 1.7.0's own CLI pods present the frontend cert as a client cert (ledger 34).
    assert {"server auth", "client auth"} <= set(frontend["usages"])
    internode = certs["temporal-internode-tls"]["spec"]
    assert {"server auth", "client auth"} <= set(internode["usages"])  # runbook 2.5
    assert internode["dnsNames"] == [
        _values("values.tls.yaml")["server"]["tls"]["internode"]["serverName"]
    ]


def test_client_certificates_have_distinct_cns_and_client_auth_only() -> None:
    certs = _certificates()
    expected = {
        "temporal-web-client-tls": "temporal-ui",
        "temporal-operator-tls": "operator",
        "eks-nexus-worker-tls": "eks-nexus-worker",
        "agentcore-worker-tls": "agentcore-worker",
        "laptop-support-tls": "laptop-support",
    }
    for name, cn in expected.items():
        spec = certs[name]["spec"]
        assert spec["commonName"] == cn
        assert "dnsNames" not in spec
        assert "server auth" not in spec["usages"]
        assert "client auth" in spec["usages"]
    assert len(set(expected.values())) == len(expected)


def test_network_policies_select_chart_frontend_labels() -> None:
    # Chart 1.7.0 labels: app.kubernetes.io/name=temporal, .../component=frontend.
    ingress = _docs(DEPLOY / "k8s" / "base" / "temporal-network" / "frontend-ingress.yaml")[0]
    assert (
        ingress["spec"]["podSelector"]["matchLabels"]["app.kubernetes.io/component"] == "frontend"
    )
    egress = _docs(DEPLOY / "k8s" / "base" / "temporal-workers" / "networkpolicy.yaml")[0]
    to_frontend = egress["spec"]["egress"][0]["to"][0]
    assert to_frontend["podSelector"]["matchLabels"] == {"app.kubernetes.io/component": "frontend"}
    # The Finance pods carry the label the ingress policy admits.
    worker = _docs(DEPLOY / "k8s" / "base" / "temporal-workers" / "finance-worker.yaml")[0]
    labels = worker["spec"]["template"]["metadata"]["labels"]
    allowed = ingress["spec"]["ingress"][1]["from"][0]["podSelector"]["matchLabels"]
    assert allowed.items() <= labels.items()


def test_finance_worker_uses_envconfig_tls_by_service_name() -> None:
    worker = _docs(DEPLOY / "k8s" / "base" / "temporal-workers" / "finance-worker.yaml")[0]
    env = {
        e["name"]: e.get("value")
        for e in worker["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert env["TEMPORAL_ADDRESS"] == f"{SVC_NAME}:7233"
    assert env["TEMPORAL_TLS_SERVER_NAME"] == SVC_NAME
    assert env["TEMPORAL_NAMESPACE"] == "agent-services"
    for var in (
        "TEMPORAL_TLS_CLIENT_CERT_PATH",
        "TEMPORAL_TLS_CLIENT_KEY_PATH",
        "TEMPORAL_TLS_SERVER_CA_CERT_PATH",
    ):
        assert env[var].startswith("/etc/temporal/tls/")
    assert worker["spec"]["replicas"] == 2


def test_kind_profile_checks_the_route53_name() -> None:
    profiles = tomllib.loads((ROOT / "config" / "temporal.toml").read_text())["profile"]
    kind = profiles["kind"]
    assert kind["address"] == "127.0.0.1:7233"
    assert kind["tls"]["server_name"] == ROUTE53_NAME
    assert kind["tls"]["client_cert_path"] == ".certs/operator/tls.crt"
    assert profiles["kind-plaintext"]["tls"] == {"disabled": True}


def test_kind_profile_loads_through_envconfig(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SDK reads the profile's relative cert paths from the working directory."""
    from temporalio.envconfig import ClientConfig
    from temporalio.service import TLSConfig

    for rel in (".certs/ca.crt", ".certs/operator/tls.crt", ".certs/operator/tls.key"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_bytes(f"fake {rel}".encode())
    monkeypatch.chdir(tmp_path)
    for var in ("TEMPORAL_ADDRESS", "TEMPORAL_TLS", "TEMPORAL_NAMESPACE"):
        monkeypatch.delenv(var, raising=False)
    config = ClientConfig.load_client_connect_config(
        profile="kind", config_file=str(ROOT / "config" / "temporal.toml"), disable_env=True
    )
    tls = config.get("tls")
    assert isinstance(tls, TLSConfig)
    assert tls.domain == ROUTE53_NAME
    assert tls.server_root_ca_cert == b"fake .certs/ca.crt"
    assert tls.client_cert == b"fake .certs/operator/tls.crt"
    assert config.get("target_host") == "127.0.0.1:7233"
