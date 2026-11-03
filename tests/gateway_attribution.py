#!/usr/bin/env python3
"""Every model call a turn makes reaches the gateway saying which project and session it is for
(DAT-314 / DAT-312).

The SDK subprocess gets the project id through `ClaudeAgentOptions.env` (Claude Code's
`ANTHROPIC_CUSTOM_HEADERS`) and sends the session id itself. The agent's own direct calls - title,
satisfaction, environment intent and the activity classifier - send all three headers themselves;
before this they sent none, so a per-session view of the trail left them out.

Offline: a stub stands in for the gateway and records the headers of every call, and a fake SDK
stands in for the Claude Code subprocess.

    python tests/gateway_attribution.py
"""
import asyncio
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
_STATE = tempfile.mkdtemp(prefix="gw-attribution-")
os.environ["DATAFYE_AGENT_STATE_DIR"] = _STATE
os.environ["DATAFYE_AGENT_WORKSPACE"] = os.path.join(_STATE, "workspace")
os.environ.pop("ANTHROPIC_BASE_URL", None)
os.environ.pop("ANTHROPIC_CUSTOM_HEADERS", None)
os.environ["ANTHROPIC_API_KEY"] = "gwt1.dTM.yukti.test-secret"

import activity  # noqa: E402
import main  # noqa: E402
from claude_agent_sdk import ResultMessage, SystemMessage  # noqa: E402

SEEN = []          # (path, {header: value}) for every POST that reached the stub
_DELAY = {"seconds": 0.0}


class Stub(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length", 0)))
        SEEN.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
        if _DELAY["seconds"]:
            import time
            time.sleep(_DELAY["seconds"])
        body = {"id": "msg_%d" % len(SEEN), "type": "message", "model": "claude-haiku-4-5",
                "stop_reason": "end_turn", "content": [{"type": "text", "text": "{\"rank\": 4}"}],
                "usage": {"input_tokens": 3, "output_tokens": 2}}
        if self.path == "/gateway/label":
            body = {"matched": 0, "unmatched": []}
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def main_():
    server = HTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    gw = f"http://127.0.0.1:{server.server_port}"
    failures = []

    def check(ok, what):
        print(("PASS " if ok else "FAIL ") + what)
        if not ok:
            failures.append(what)

    P, S, A = activity.PROJECT_HEADER, activity.SESSION_HEADER, activity.AGENT_HEADER

    # -- the context on its own --------------------------------------------------------------
    off = activity.CallContext("proj-a", "sess-a", on_gateway=False)
    check(off.headers("title") == {} and off.sdk_env() == {},
          "off the gateway nothing is sent: the ids are the gateway's business, not the provider's")
    on = activity.CallContext("proj-a", None, on_gateway=True)
    check(on.headers("title") == {A: "sidecar:title", P: "proj-a"}, "no session yet: project + agent only")
    on.set_session("sess-a")
    on.set_session(None)
    check(on.headers("title").get(S) == "sess-a", "a later empty session id does not erase a known one")
    check(on.sdk_env() == {"ANTHROPIC_CUSTOM_HEADERS": f"{P}: proj-a"}, "the SDK gets the project header")
    evil = activity.CallContext("proj-a\nx-api-key: stolen", "se\r\nss", on_gateway=True)
    check(evil.sdk_env() == {} and P not in evil.headers("t") and S not in evil.headers("t"),
          "an id with a newline is dropped, not sent: ANTHROPIC_CUSTOM_HEADERS is newline-separated")
    check(activity.CallContext(None, None, on_gateway=True).headers("t") == {A: "sidecar:t"},
          "a call outside any project still goes, with no project")

    # -- each direct call carries the turn's ids (AC1 of DAT-312) ----------------------------
    main._apply_gateway_url(gw)
    ctx = activity.CallContext("proj-a", "sess-a", on_gateway=True)
    asyncio.run(main.generate_title("Backtest a momentum strategy on SPY", None, ctx))
    asyncio.run(main.analyze_satisfaction("User: thanks", None, ctx))
    asyncio.run(main.classify_environment_intent("User: shut it down", None, ctx))
    collector = activity.Collector(gateway_url=gw, token="tok", model="claude-haiku-4-5",
                                   base_url=gw, call_context=ctx)
    asyncio.run(collector._ask([("msg_x", {"tools": ["Bash"], "texts": [], "thinking": []})]))
    sides = [h for path, h in SEEN if path == "/v1/messages"]
    agents = [h.get(A) for h in sides]
    check(agents == ["sidecar:title", "sidecar:satisfaction", "sidecar:environment-intent",
                     "sidecar:activity"], f"all four direct calls reached the gateway, named ({agents})")
    check(all(h.get(S) == "sess-a" and h.get(P) == "proj-a" for h in sides),
          "every direct call carries the turn's session and project")

    # -- two turns on different projects at once (AC3 of DAT-314) ----------------------------
    SEEN.clear()
    _DELAY["seconds"] = 0.2         # each call is still open when the other one starts

    async def both():
        a = activity.CallContext("proj-a", "sess-a", on_gateway=True)
        b = activity.CallContext("proj-b", "sess-b", on_gateway=True)
        await asyncio.gather(main.generate_title("one", None, a), main.generate_title("two", None, b))
        return a, b

    a, b = asyncio.run(both())
    _DELAY["seconds"] = 0.0
    pairs = sorted((h.get(P), h.get(S)) for _, h in SEEN)
    check(pairs == [("proj-a", "sess-a"), ("proj-b", "sess-b")],
          f"concurrent calls each carry their OWN project and session ({pairs})")
    check(a.sdk_env() != b.sdk_env() and "ANTHROPIC_CUSTOM_HEADERS" not in os.environ,
          "the SDK header is per invocation: the process environment is never touched")

    # -- a real turn, through stream_agent_response, with the SDK faked ----------------------
    # Proves the wiring rather than the helper: the options carry the project env, the session id
    # learnt from the SDK's init reaches the side-calls made after the stream.
    seen_options = []

    async def fake_query(prompt, options):
        seen_options.append(options)
        yield SystemMessage(subtype="init", data={"session_id": "sess-turn"})
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                            num_turns=1, session_id="sess-turn", total_cost_usd=0.0,
                            usage={"input_tokens": 1, "output_tokens": 1}, result="done")

    async def no_state():
        return None

    main.query = fake_query
    main._fetch_deployment_state = no_state
    main.AGENT_USERNAME = "u1"
    main.credentials = {}           # loaded at startup, which this test does not run
    SEEN.clear()

    async def turn():
        async for _ in main.stream_agent_response("Build me a dashboard", "proj-turn", None, "jwt"):
            pass

    asyncio.run(turn())
    env = seen_options[0].env if seen_options else {}
    check(env == {"ANTHROPIC_CUSTOM_HEADERS": f"{P}: proj-turn"},
          f"the turn hands the SDK its project header ({env})")
    sides = [h for path, h in SEEN if path == "/v1/messages"]
    check(bool(sides) and all(h.get(P) == "proj-turn" and h.get(S) == "sess-turn" for h in sides),
          f"the turn's side-calls carry its project and the session from init ({len(sides)} call(s))")

    main._apply_gateway_url(None)
    seen_options.clear()
    SEEN.clear()
    asyncio.run(turn())
    check(seen_options and seen_options[0].env == {}, "off the gateway the SDK gets no project header")

    server.shutdown()
    print(f"\n{len(failures)} failure(s)" if failures else "\nall checks pass")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main_()
