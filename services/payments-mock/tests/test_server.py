from collections.abc import Iterator

import httpx
import pytest

from payments_mock.server import PaymentsServer, serve_in_thread

pytestmark = pytest.mark.unit


@pytest.fixture
def base_url() -> Iterator[str]:
    server: PaymentsServer
    server, url = serve_in_thread()
    try:
        yield url
    finally:
        server.shutdown()
        server.server_close()


def test_refund_is_idempotent_over_http(base_url: str) -> None:
    body = {"order_id": "1042", "amount_usd": 40.0}
    headers = {"Idempotency-Key": "a2a-task-support-1042"}
    first = httpx.post(f"{base_url}/refunds", json=body, headers=headers)
    second = httpx.post(f"{base_url}/refunds", json=body, headers=headers)
    assert first.status_code == 201 and first.json()["replayed"] is False
    assert second.status_code == 200 and second.json()["replayed"] is True
    assert first.json()["refund_id"] == second.json()["refund_id"]
    assert len(httpx.get(f"{base_url}/refunds").json()) == 1
    refund_id = first.json()["refund_id"]
    assert httpx.get(f"{base_url}/refunds/{refund_id}").json()["order_id"] == "1042"


def test_errors(base_url: str) -> None:
    body = {"order_id": "1042", "amount_usd": 40.0}
    assert httpx.post(f"{base_url}/refunds", json=body).status_code == 400  # no key
    assert (
        httpx.post(
            f"{base_url}/refunds", json={"x": 1}, headers={"Idempotency-Key": "k"}
        ).status_code
        == 400
    )
    httpx.post(f"{base_url}/refunds", json=body, headers={"Idempotency-Key": "k"})
    conflict = httpx.post(
        f"{base_url}/refunds", json={**body, "amount_usd": 99.0}, headers={"Idempotency-Key": "k"}
    )
    assert conflict.status_code == 409
    assert httpx.get(f"{base_url}/refunds/R-1").status_code == 404
    assert httpx.get(f"{base_url}/healthz").json() == {"status": "ok"}
