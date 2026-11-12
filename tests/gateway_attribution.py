#!/usr/bin/env python3
"""Every model call a turn makes reaches the gateway saying which project and session it is for
(DAT-314 / DAT-312).

The SDK subprocess gets the project id through `ClaudeAgentOptions.env` (Claude Code's
`ANTHROPIC_CUSTOM_HEADERS`) and sends the session id itself. The agent's own direct calls - title,
satisfaction, environment intent and the activity classifier - send all three headers themselves;
before this they sent none, so a per-session view of the trail left them out.

Offline: a stub stands in for the gateway and records the headers of every call, and a fake SDK
stands in for the Claude Code subprocess. The stub is THREADED, so two turns' calls can be in flight
at once, and it records the peak to prove they were.

    python tests/gateway_attribution.py
"""
import asyncio
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
_INFLIGHT = {"now": 0, "peak": 0}
_LOCK = threading.Lock()


class Stub(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length", 0)))
        with _LOCK:
            SEEN.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
            _INFLIGHT["now"] += 1
            _INFLIGHT["peak"] = max(_INFLIGHT["peak"], _INFLIGHT["now"])
        if _DELAY["seconds"]:
            import time
            time.sleep(_DELAY["seconds"])
        with _LOCK:
            _INFLIGHT["now"] -= 1
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
    server = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    gw = f"http://127.0.0.1:{server.server_port}"
    failures = []

    def check(ok, what):
        print(("PASS " if ok else "FAIL ") + what)
        if not ok:
            failures.append(what)

    P, S, A = activity.PROJECT_HEADER, activity.SESSION_HEADER, activity.AGENT_HEADER

    # -- the context on its own --------------------------------------------------------------
    off = activity.CallContext("proj-a", on_gateway=False)
    off.set_session("sess-a")
    check(off.headers("title") == {} and off.sdk_env() == {},
          "off the gateway nothing is sent: the ids are the gateway's business, not the provider's")
    on = activity.CallContext("proj-a", on_gateway=True)
    check(on.headers("title") == {A: "sidecar:title", P: "proj-a"}, "no session yet: project + agent only")
    on.set_session("sess-a")
    on.set_session(None)
    check(on.headers("title").get(S) == "sess-a", "a later empty session id does not erase a known one")
    check(on.sdk_env() == {"ANTHROPIC_CUSTOM_HEADERS": f"{P}: proj-a"}, "the SDK gets the project header")
    evil = activity.CallContext("proj-a\nx-api-key: stolen", on_gateway=True)
    evil.set_session("se\r\nss")
    check(evil.sdk_env() == {} and P not in evil.headers("t") and S not in evil.headers("t"),
          "an id with a newline is dropped, not sent: ANTHROPIC_CUSTOM_HEADERS is newline-separated")
    check(activity.CallContext("proj-\u9879\u76ee", on_gateway=True).sdk_env() == {},
          "a non-ASCII id is dropped: it is not a legal header, and would fail every SDK call in the turn")
    check(activity.CallContext("p" * (activity.MAX_PROJECT_ID_LENGTH + 1), on_gateway=True).project_id is None,
          "an id longer than the gateway accepts is not sent")
    check(activity.CallContext(None, on_gateway=True).headers("t") == {A: "sidecar:t"},
          "a call outside any project still goes, with no project")
    os.environ["ANTHROPIC_CUSTOM_HEADERS"] = "x-route: blue"
    check(on.sdk_env() == {"ANTHROPIC_CUSTOM_HEADERS": f"x-route: blue\n{P}: proj-a"},
          "custom headers the box already sets are kept, with the project line added")
    os.environ["ANTHROPIC_CUSTOM_HEADERS"] = f"x-route: blue\n{P}: box-default"
    check(on.sdk_env() == {"ANTHROPIC_CUSTOM_HEADERS": f"x-route: blue\n{P}: proj-a"},
          "a project line the box already sets is REPLACED: the gateway reads the first one")
    del os.environ["ANTHROPIC_CUSTOM_HEADERS"]
    flag = [True]
    moving = activity.CallContext("proj-a", on_gateway=lambda: flag[0])
    before = moving.headers("t")
    flag[0] = False
    check(P in before and moving.headers("t") == {},
          "on/off the gateway is asked at each call, so a mid-turn bootstrap is followed")

    # -- each direct call carries the turn's ids (AC1 of DAT-312) ----------------------------
    main._apply_gateway_url(gw)
    ctx = activity.CallContext("proj-a", on_gateway=True)
    ctx.set_session("sess-a")
    asyncio.run(main.generate_title("Backtest a momentum strategy on SPY", None, call_context=ctx))
    asyncio.run(main.analyze_satisfaction("User: thanks", None, call_context=ctx))
    asyncio.run(main.classify_environment_intent("User: shut it down", None, call_context=ctx))
    collector = activity.Collector(gateway_url=gw, token="tok", model="claude-haiku-4-5",
                                   base_url=gw, call_context=ctx)
    asyncio.run(collector._ask([("msg_x", {"tools": ["Bash"], "texts": [], "thinking": []})]))
    sides = [h for path, h in SEEN if path == "/v1/messages"]
    agents = [h.get(A) for h in sides]
    # The classifier's URL is fixed when the turn begins, so it sends the ids only when THAT URL is
    # the gateway, whatever the box is on by now: after a mid-turn bootstrap the two disagree.
    SEEN.clear()
    moved_on = activity.Collector(gateway_url="http://the-new-gateway", token="tok",
                                  model="claude-haiku-4-5", base_url=gw, call_context=ctx)
    asyncio.run(moved_on._ask([("msg_y", {"tools": ["Bash"], "texts": [], "thinking": []})]))
    check(SEEN and P not in SEEN[-1][1] and S not in SEEN[-1][1],
          "a classifier posting to a URL that is not its gateway sends no ids")
    SEEN.clear()
    moved_off = activity.CallContext("proj-a", on_gateway=False)
    moved_off.set_session("sess-a")
    asyncio.run(activity.Collector(gateway_url=gw, token="tok", model="claude-haiku-4-5",
                                   base_url=gw, call_context=moved_off)._ask(
        [("msg_z", {"tools": ["Bash"], "texts": [], "thinking": []})]))
    check(SEEN and SEEN[-1][1].get(P) == "proj-a",
          "and one still posting to its gateway keeps sending them, so those calls stay attributed")

    check(agents == ["sidecar:title", "sidecar:satisfaction", "sidecar:environment-intent",
                     "sidecar:activity"], f"every direct call reached the gateway, named ({agents})")
    check(all(h.get(S) == "sess-a" and h.get(P) == "proj-a" for h in sides),
          "every direct call carries the turn's session and project")

    # -- real turns, through stream_agent_response, with the SDK faked -----------------------
    # Proves the wiring rather than the helper: the options carry the project env, and the session
    # id learnt from the SDK's init reaches the side-calls made after the stream.
    seen_options = {}
    no_init = set()

    async def fake_query(prompt, options):
        project = prompt.split()[-1]
        seen_options[project] = options
        session = "sess-" + project
        if project not in no_init:
            yield SystemMessage(subtype="init", data={"session_id": session})
        await asyncio.sleep(0.1)    # the other turn runs meanwhile
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                            num_turns=1, session_id=session, total_cost_usd=0.0,
                            usage={"input_tokens": 1, "output_tokens": 1}, result="done")

    async def no_state():
        return None

    main.query = fake_query
    main._fetch_deployment_state = no_state
    main.AGENT_USERNAME = "u1"
    main.credentials = {}           # loaded at startup, which this test does not run

    async def turn(project):
        async for _ in main.stream_agent_response("Build me a dashboard for " + project, project, None, "jwt"):
            pass

    # Two turns on different projects AT ONCE (AC3 of DAT-314). The stub is threaded and slow, so
    # their side-calls are genuinely in flight together; the peak proves it rather than assuming it.
    SEEN.clear()
    _DELAY["seconds"] = 0.3
    _INFLIGHT["peak"] = 0

    async def both():
        await asyncio.gather(turn("proj-1"), turn("proj-2"))

    asyncio.run(both())
    _DELAY["seconds"] = 0.0
    check(_INFLIGHT["peak"] >= 2, f"the two turns' calls really overlapped (peak {_INFLIGHT['peak']})")
    envs = {p: o.env for p, o in seen_options.items()}
    check(envs == {"proj-1": {"ANTHROPIC_CUSTOM_HEADERS": f"{P}: proj-1"},
                   "proj-2": {"ANTHROPIC_CUSTOM_HEADERS": f"{P}: proj-2"}},
          f"each turn hands ITS OWN SDK invocation its own project ({envs})")
    pairs = {(h.get(P), h.get(S)) for path, h in SEEN if path == "/v1/messages"}
    check(pairs == {("proj-1", "sess-proj-1"), ("proj-2", "sess-proj-2")},
          f"each turn's side-calls carry its own project and its own session from init ({sorted(pairs)})")
    check("ANTHROPIC_CUSTOM_HEADERS" not in os.environ, "the process environment is never touched")

    # A resumed turn whose SDK never reports init: its calls belong to no session, not the old one.
    main.conversations.ensure("proj-3")
    main.conversations.set_sdk_session("proj-3", "sess-from-last-run")
    no_init.add("proj-3")
    SEEN.clear()
    asyncio.run(turn("proj-3"))
    sides = [h for path, h in SEEN if path == "/v1/messages"]
    check(seen_options["proj-3"].resume == "sess-from-last-run", "the turn did resume the old session")
    check(bool(sides) and all(S not in h and h.get(P) == "proj-3" for h in sides),
          f"with no init, side-calls carry the project and NO session ({len(sides)} call(s))")

    main._apply_gateway_url(None)
    seen_options.clear()
    SEEN.clear()
    asyncio.run(turn("proj-4"))
    check(seen_options["proj-4"].env == {} and not [h for _, h in SEEN if P in h],
          "off the gateway the SDK gets no project header, and no call carries one")

    server.shutdown()
    print(f"\n{len(failures)} failure(s)" if failures else "\nall checks pass")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main_()
