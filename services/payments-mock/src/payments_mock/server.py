"""HTTP server for the payments mock (stdlib only).

    uv run python -m payments_mock.server [--host 127.0.0.1] [--port 8081]   (make payments-mock)

Env: ``PAYMENTS_HOST`` / ``PAYMENTS_PORT`` (defaults 127.0.0.1:8081).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from payments_mock.ledger import IdempotencyConflictError, RefundLedger

log = logging.getLogger("payments-mock")
_REFUND_PATH = re.compile(r"^/refunds/(R-\d+)$")


class PaymentsServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], ledger: RefundLedger | None = None) -> None:
        super().__init__(address, _Handler)
        self.ledger = ledger or RefundLedger()


class _Handler(BaseHTTPRequestHandler):
    server: PaymentsServer
    server_version = "payments-mock/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        log.info("%s %s", self.address_string(), format % args)

    def _send(self, status: HTTPStatus, body: Any) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._send(HTTPStatus.OK, {"status": "ok"})
        elif self.path == "/refunds":
            self._send(HTTPStatus.OK, [r.to_json() for r in self.server.ledger.all()])
        elif match := _REFUND_PATH.match(self.path):
            refund = self.server.ledger.get(match.group(1))
            if refund is None:
                self._send(HTTPStatus.NOT_FOUND, {"error": "no such refund"})
            else:
                self._send(HTTPStatus.OK, refund.to_json())
        else:
            self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/refunds":
            self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        key = self.headers.get("Idempotency-Key", "")
        try:
            body = json.loads(
                self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}"
            )
            order_id = str(body["order_id"])
            amount = float(body["amount_usd"])
        except (ValueError, KeyError, TypeError) as err:
            self._send(HTTPStatus.BAD_REQUEST, {"error": f"bad request body: {err}"})
            return
        if not key:
            self._send(HTTPStatus.BAD_REQUEST, {"error": "Idempotency-Key header is required"})
            return
        try:
            refund, replayed = self.server.ledger.issue(
                idempotency_key=key, order_id=order_id, amount_usd=amount
            )
        except IdempotencyConflictError as err:
            self._send(HTTPStatus.CONFLICT, {"error": str(err)})
            return
        except ValueError as err:
            self._send(HTTPStatus.BAD_REQUEST, {"error": str(err)})
            return
        log.info(
            "refund %s order=%s $%.2f key=%s%s",
            refund.refund_id,
            refund.order_id,
            refund.amount_usd,
            key,
            " (replayed: same key, no new refund)" if replayed else "",
        )
        self._send(
            HTTPStatus.OK if replayed else HTTPStatus.CREATED,
            {**refund.to_json(), "replayed": replayed},
        )


def serve_in_thread(host: str = "127.0.0.1", port: int = 0) -> tuple[PaymentsServer, str]:
    """Start a server on a background thread (tests); returns it and its base URL."""
    server = PaymentsServer((host, port))
    threading.Thread(target=server.serve_forever, name="payments-mock", daemon=True).start()
    return server, f"http://{host}:{server.server_address[1]}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock payments system (idempotent refunds)")
    parser.add_argument("--host", default=os.environ.get("PAYMENTS_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PAYMENTS_PORT", "8081")))
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    server = PaymentsServer((args.host, args.port))
    log.info("payments-mock listening on http://%s:%d", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
