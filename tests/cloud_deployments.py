#!/usr/bin/env python3
"""Cloud deployments on the user's behalf (DAT-305), against a stub accounts. Offline and deterministic.

What must never be wrong, each of which would fail quietly:

  - every call carries the user's own session, and creates the deployment in THIS project's name
  - accounts' refusal (no grant, a busy deployment) reaches the model in accounts' words, and a refused
    create says nothing was changed
  - nothing is sent for a bad name, an empty descriptor or an unknown operation
  - the event stream is read across reconnects by offset, a finished job is reported done with how it
    ended, and a watch that runs out of time says the job carries on rather than that it ended
  - a watcher who loses access is told so, never told the job ended

    python3 tests/cloud_deployments.py

Exits non-zero on any failure.
"""
import asyncio
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import cloud_deployments as cd  # noqa: E402

FAILED = []


def check(ok, what):
    print(f"  {'PASS' if ok else 'FAIL'}  {what}")
    if not ok:
        FAILED.append(what)


class Accounts:
    """What the stub answers, and what it was asked."""
    requests = []
    create_status = 202
    create_body = {"deployment": {"id": "dep-7"}, "job": {"id": "job-12"}}
    events = []          # (delay seconds, raw SSE text) written in order, then the stream closes
    events_status = 200
    hold_open = 0.0      # keep the stream open this long after the last event


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _record(self, body=None):
        Accounts.requests.append({"method": self.command, "path": self.path,
                                  "auth": self.headers.get("Authorization"), "body": body})

    def _json(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        self._record(body)
        if self.path.endswith("/deployments"):
            self._json(Accounts.create_status, Accounts.create_body if Accounts.create_status < 300 else
                       {"error": "NotPermitted", "message": "This account cannot create cloud deployments: an admin grants them"})
        else:
            self._json(409, {"error": "DeploymentBusy", "message": "Deployment 'x' is running job job-3"})

    def do_GET(self):
        self._record()
        if "/events" in self.path:
            if Accounts.events_status != 200:
                self._json(Accounts.events_status, {"error": "DeploymentNotFound", "message": "No deployment found with id 'dep-9'"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for delay, raw in Accounts.events:
                time.sleep(delay)
                self.wfile.write(raw.encode())
                self.wfile.flush()
            time.sleep(Accounts.hold_open)
        else:
            self._json(200, {"can_create": True, "deployments": [
                {"id": "dep-7", "name": "u7-abc", "state": "Running", "version": "2.0.63",
                 "url": "https://x/datafye-api/v1", "actions": ["upgrade", "stop", "deprovision"]}]})


def sse(event, data, ev_id=None):
    return (f"id: {ev_id}\n" if ev_id is not None else "") + f"event: {event}\ndata: {json.dumps(data)}\n\n"


async def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    token = "user-session-jwt"

    print("== default names are valid and per project ==")
    n = cd.default_name("u7", "proj-0a1b2c3d4e")
    check(cd.NAME.match(n) is not None and n == "u7-1b2c3d4e", f"default name {n}")
    check(cd.NAME.match(cd.default_name("7Bob!", None)) is not None, "a username starting with a digit still gives a valid name")

    print("== create: the user's session, this project, accounts' refusal in its own words ==")
    Accounts.requests = []
    r = await cd.create(base, token, "u7-abc", "mode: backtest\n", "proj-1", "2.0.63")
    req = Accounts.requests[0]
    check(r["ok"] and r["deployment_id"] == "dep-7" and r["job_id"] == "job-12", "created and queued")
    check(req["auth"] == "Bearer user-session-jwt", "sent with the user's own session")
    check(req["body"] == {"name": "u7-abc", "descriptor": "mode: backtest\n", "project_id": "proj-1", "version": "2.0.63"},
          f"the body: {req['body']}")
    check("deployment_progress(deployment_id='dep-7', job_id='job-12')" in r["text"], "tells the model how to follow it")
    Accounts.create_status = 403
    r = await cd.create(base, token, "u7-abc", "mode: backtest\n", "proj-1", None)
    check(not r["ok"] and "an admin grants them" in r["text"] and "Nothing was changed" in r["text"], r["text"])
    Accounts.create_status = 202
    Accounts.requests = []
    r1 = await cd.create(base, token, "Bad Name", "mode: backtest\n", None, None)
    r2 = await cd.create(base, token, "u7-abc", "  ", None, None)
    check(not r1["ok"] and not r2["ok"] and Accounts.requests == [], "a bad name or no descriptor sends nothing")

    print("== run and list ==")
    Accounts.requests = []
    r = await cd.run(base, token, "dep-7", "explode")
    check(not r["ok"] and Accounts.requests == [], "an unknown operation sends nothing")
    r = await cd.run(base, token, "dep-7", "upgrade", "2.0.64", "datafye-sip-system")
    check(Accounts.requests[0]["body"] == {"operation": "upgrade", "version": "2.0.64", "system": "datafye-sip-system"},
          f"upgrade body {Accounts.requests[0]['body']}")
    check(not r["ok"] and "running job job-3" in r["text"], "a busy deployment is reported as accounts said it")
    r = await cd.list_deployments(base, token)
    check(r["ok"] and "u7-abc (dep-7): Running, Datafye 2.0.63" in r["text"] and "may create a new one" in r["text"], r["text"])

    print("== watch: the log as it grows, then how it ended ==")
    Accounts.events = [(0, ": keepalive\n\n"), (0, sse("log", {"text": "starting\n"}, 9)),
                       (0, sse("status", {"status": "Running"})),
                       (0.2, sse("log", {"text": "provisioned\n"}, 21)),
                       (0, sse("status", {"status": "Succeeded"})),
                       (0, sse("done", {"status": "Succeeded", "next_offset": 21}, 21))]
    Accounts.requests = []
    r = await cd.watch(base, token, "dep-7", "job-12", 0, 30)
    check(r["done"] and r["status"] == "Succeeded" and r["next_offset"] == 21, f"ended: {r}")
    check("starting\nprovisioned\n" in r["text"] and "has ended: Succeeded" in r["text"], r["text"])
    check("offset=0" in Accounts.requests[0]["path"] and Accounts.requests[0]["auth"] == "Bearer user-session-jwt",
          "asks from the offset given, with the user's session")

    print("== watch: a failed job says why ==")
    Accounts.events = [(0, sse("status", {"status": "Failed"})),
                       (0, sse("done", {"status": "Failed", "next_offset": 40, "error": "AccessDenied on RunInstances"}, 40))]
    r = await cd.watch(base, token, "dep-7", "job-12", 30, 30)
    check(r["done"] and "Error: AccessDenied on RunInstances" in r["text"], r["text"])

    print("== watch: running out of time is not the job ending ==")
    Accounts.events = [(0, sse("status", {"status": "Running"})), (0, sse("log", {"text": "launching\n"}, 50))]
    Accounts.hold_open = 8
    t0 = time.time()
    r = await cd.watch(base, token, "dep-7", "job-12", 40, 5)
    took = time.time() - t0
    Accounts.hold_open = 0
    check(not r["done"] and r["status"] == "Running" and r["next_offset"] == 50, f"still running: {r}")
    check("carries on regardless" in r["text"] and "offset=50" in r["text"], r["text"])
    check(4 <= took < 8, f"returned at its limit, not when the stream closed ({took:.1f}s)")

    print("== watch: losing access, and a job that cannot be found ==")
    Accounts.events = [(0, sse("status", {"status": "Running"})),
                       (0, sse("error", {"error": "This job can no longer be watched"}))]
    r = await cd.watch(base, token, "dep-7", "job-12", 0, 30)
    check("can no longer be watched" in r["text"] and "has ended" not in r["text"], r["text"])
    Accounts.events_status = 404
    r = await cd.watch(base, token, "dep-9", "job-1", 0, 30)
    check(not r["ok"] and "No deployment found" in r["text"], r["text"])

    server.shutdown()


asyncio.run(main())
print(f"\n{'ALL PASSED' if not FAILED else f'{len(FAILED)} FAILED'}")
sys.exit(1 if FAILED else 0)
