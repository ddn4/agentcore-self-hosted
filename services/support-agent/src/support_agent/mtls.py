"""mTLS material for the AgentCore Worker, fetched at startup (runbook 5.3, ADR-0007).

When ``TEMPORAL_MTLS_SECRET_ID`` is set, the Worker reads one Secrets Manager secret
``{"tls_crt", "tls_key", "ca_crt"}`` and exports it as the envconfig variables
``TEMPORAL_TLS_CLIENT_CERT_DATA``, ``TEMPORAL_TLS_CLIENT_KEY_DATA`` and
``TEMPORAL_TLS_SERVER_CA_CERT_DATA`` *before* the client connects, so
``temporalio.envconfig`` builds the TLS config like any other setting. Cert material
never goes into ``agentcore.json`` or git.

Locally (v0.7) the variable is unset and this is a no-op: the dev server has no TLS.
It is exercised against a real secret from v0.9 (optional) / v0.12.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Protocol

log = logging.getLogger(__name__)

SECRET_ID_ENV = "TEMPORAL_MTLS_SECRET_ID"
#: secret JSON key -> envconfig variable
ENV_FROM_SECRET: dict[str, str] = {
    "tls_crt": "TEMPORAL_TLS_CLIENT_CERT_DATA",
    "tls_key": "TEMPORAL_TLS_CLIENT_KEY_DATA",
    "ca_crt": "TEMPORAL_TLS_SERVER_CA_CERT_DATA",
}


class SecretsClient(Protocol):
    def get_secret_value(self, *, SecretId: str) -> dict[str, Any]: ...


def load_mtls_into_env(client: SecretsClient | None = None) -> bool:
    """Export the Worker's client cert, key and CA from Secrets Manager.

    Returns ``False`` (and does nothing) when ``TEMPORAL_MTLS_SECRET_ID`` is unset.
    ``client`` is injectable for tests; by default a boto3 Secrets Manager client in
    ``AWS_REGION`` (the AgentCore execution role's credentials).
    """
    secret_id = os.environ.get(SECRET_ID_ENV, "").strip()
    if not secret_id:
        return False
    if client is None:
        import boto3  # imported lazily: only the cloud path needs it

        sm: Any = boto3.client("secretsmanager", region_name=os.environ.get("AWS_REGION"))
    else:
        sm = client
    secret = json.loads(sm.get_secret_value(SecretId=secret_id)["SecretString"])
    missing = [k for k in ENV_FROM_SECRET if not secret.get(k)]
    if missing:
        raise ValueError(f"mTLS secret is missing {missing}")
    for key, env_name in ENV_FROM_SECRET.items():
        os.environ[env_name] = secret[key]
    os.environ.setdefault("TEMPORAL_TLS", "true")
    log.info("mTLS: client cert, key and CA loaded from Secrets Manager into env")
    return True
