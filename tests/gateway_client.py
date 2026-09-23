#!/usr/bin/env python3
"""The agent follows the metering gateway it is told about at bootstrap (DAT-295).

A hosted box's bootstrap token carries `llm_gateway_url`, and its Anthropic
"key" is then a per-user gateway token. Every model call has to go there: the
SDK subprocess (it reads ANTHROPIC_BASE_URL itself), the key check, and the
four direct Haiku side-calls, which used to hard-code api.anthropic.com and so
would have sent the gateway token to Anthropic and failed.

Offline: a stub stands in for the gateway and records what reaches it.

    python tests/gateway_client.py
"""
import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ.pop("ANTHROPIC_BASE_URL", None)
os.environ["ANTHROPIC_API_KEY"] = "gwt1.dTM.yukti.test-secret"

import main  # noqa: E402

SEEN = []


class Stub(BaseHTTPRequestHandler):
    def _reply(self, body):
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        SEEN.append(("GET", self.path, self.headers.get("x-api-key")))
        self._reply({"data": []})

    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length", 0)))
        SEEN.append(("POST", self.path, self.headers.get("x-api-key")))
        self._reply({"id": "m", "type": "message", "model": "claude-haiku-4-5", "stop_reason": "end_turn",
                     "content": [{"type": "text", "text": "A title"}],
                     "usage": {"input_tokens": 3, "output_tokens": 2}})

    def log_message(self, *a):
        pass


def main_():
    server = HTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    gw = f"http://127.0.0.1:{server.server_port}/"
    failures = []

    def check(ok, what):
        print(("PASS " if ok else "FAIL ") + what)
        if not ok:
            failures.append(what)

    check(main._anthropic_base() == "https://api.anthropic.com", "no claim: direct to Anthropic")

    main._apply_gateway_url(gw)
    check(os.environ.get("ANTHROPIC_BASE_URL") == gw.rstrip("/"), "claim sets ANTHROPIC_BASE_URL for the SDK subprocess")

    status = main._validate_anthropic_key(os.environ["ANTHROPIC_API_KEY"])
    check(status == "ok" and SEEN[-1][:2] == ("GET", "/v1/models"), f"key check goes to the gateway ({status})")

    title = asyncio.run(main.generate_title("Backtest a momentum strategy on SPY"))
    check(bool(SEEN) and SEEN[-1][:2] == ("POST", "/v1/messages"), "title side-call goes to the gateway")
    check(SEEN[-1][2] == os.environ["ANTHROPIC_API_KEY"], "side-call presents the gateway token")
    check(title is not None, f"side-call result is used ({title!r})")

    main._apply_gateway_url(None)
    check(main._anthropic_base() == "https://api.anthropic.com", "a bootstrap without the claim goes back to direct")
    server.shutdown()
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main_()
