import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import httpx

from pmc.provider_gateway import ProviderGateway


def test_gateway_rotates_lane_after_rate_limit_and_hides_provider_keys(monkeypatch):
    upstream_authorizations: list[str] = []

    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, _format, *_args):
            return

        def do_POST(self):
            length = int(self.headers["Content-Length"])
            payload = json.loads(self.rfile.read(length))
            upstream_authorizations.append(self.headers["Authorization"])
            if len(upstream_authorizations) == 1:
                body = json.dumps({"error": {"message": "slow down"}}).encode()
                self.send_response(429)
                self.send_header("retry-after", "60")
            else:
                body = json.dumps(
                    {
                        "id": "provider-request",
                        "choices": [
                            {"message": {"role": "assistant", "content": "ok"}}
                        ],
                        "usage": {"prompt_tokens": 3, "completion_tokens": 1},
                        "model": payload["model"],
                    }
                ).encode()
                self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    upstream = HTTPServer(("127.0.0.1", 0), Upstream)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()

    class Accounting:
        def __init__(self):
            self.turns = []
            self.failures = []
            self.successes = []

        def reserve(self, turn, _messages, _max_output):
            self.turns.append(turn)
            return SimpleNamespace(api_key_env="LANE_ONE" if turn == 1 else "LANE_TWO")

        def fail(self, ticket, error):
            self.failures.append((ticket.api_key_env, error.status_code))

        def succeed(self, ticket, reply):
            self.successes.append((ticket.api_key_env, reply.input_tokens))

    monkeypatch.setenv("LANE_ONE", "secret-one")
    monkeypatch.setenv("LANE_TWO", "secret-two")
    accounting = Accounting()
    try:
        with ProviderGateway(
            bind_host="127.0.0.1",
            public_host="127.0.0.1",
            upstream_base_url=f"http://127.0.0.1:{upstream.server_port}/v1",
            upstream_model="provider/exact-model",
            accounting=accounting,
            max_failovers=1,
        ) as gateway:
            response = httpx.post(
                gateway.base_url + "/chat/completions",
                headers={"Authorization": f"Bearer {gateway.token}"},
                json={
                    "model": "transport/model",
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 16,
                },
                timeout=10,
            )
        assert response.status_code == 200
        assert response.json()["model"] == "provider/exact-model"
        assert accounting.turns == [1, 2]
        assert accounting.failures == [("LANE_ONE", 429)]
        assert accounting.successes == [("LANE_TWO", 3)]
        assert upstream_authorizations == ["Bearer secret-one", "Bearer secret-two"]
        assert "secret-one" not in response.text
        assert "secret-two" not in response.text
    finally:
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=5)
