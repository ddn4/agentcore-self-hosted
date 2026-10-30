"""mTLS from Secrets Manager into envconfig variables (runbook 5.3; v0.7: no-op locally)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from temporalio.envconfig import ClientConfig

from support_agent import mtls

pytestmark = pytest.mark.unit

# Not real PEM material: the scrub check rejects private-key headers on purpose.
SECRET = {"tls_crt": "client-cert-pem", "tls_key": "client-key-pem", "ca_crt": "ca-pem"}


class FakeSecrets:
    def __init__(self, value: dict[str, str]) -> None:
        self.value = value
        self.asked: list[str] = []

    def get_secret_value(self, *, SecretId: str) -> dict[str, Any]:
        self.asked.append(SecretId)
        return {"SecretString": json.dumps(self.value)}


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [mtls.SECRET_ID_ENV, "TEMPORAL_TLS", *mtls.ENV_FROM_SECRET.values()]:
        monkeypatch.delenv(name, raising=False)


def test_no_secret_id_is_a_no_op() -> None:
    fake = FakeSecrets(SECRET)
    assert mtls.load_mtls_into_env(fake) is False
    assert fake.asked == []


def test_secret_lands_in_the_envconfig_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(mtls.SECRET_ID_ENV, "temporal/agentcore-worker-mtls")
    monkeypatch.setenv("TEMPORAL_ADDRESS", "frontend.example.internal:7233")
    monkeypatch.delenv("TEMPORAL_PROFILE", raising=False)  # make exports "local"
    fake = FakeSecrets(SECRET)
    assert mtls.load_mtls_into_env(fake) is True
    assert fake.asked == ["temporal/agentcore-worker-mtls"]
    # envconfig turns the variables into the client's TLS config, like any other setting.
    cfg = ClientConfig.load_client_connect_config(config_file="/dev/null")
    tls = cfg.get("tls")
    assert tls and not isinstance(tls, bool)
    assert tls.client_cert == b"client-cert-pem"
    assert tls.client_private_key == b"client-key-pem"
    assert tls.server_root_ca_cert == b"ca-pem"


def test_incomplete_secret_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(mtls.SECRET_ID_ENV, "temporal/agentcore-worker-mtls")
    with pytest.raises(ValueError, match="ca_crt"):
        mtls.load_mtls_into_env(FakeSecrets({"tls_crt": "c", "tls_key": "k"}))
