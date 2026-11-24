"""Cloud deployments on the user's behalf (DAT-305).

The agent deploys the project's ENVIRONMENT (the descriptor of its local foundry) to Datafye's cloud
through the accounts deployment API, with the user's OWN session JWT: the one it already forwards for
usage and feedback. No agent token is minted, so the deployment is owned by the user, and whether they
may deploy at all is accounts' decision (the per-account grant, decided 2026-11-24), never this
module's. Live trading is refused by accounts at every launch whatever this module sends.

Everything here is a plain async function of (base URL, token, ...) so it can be tested against a stub
accounts with no agent running. Each returns text written FOR THE MODEL, never raises: what happened,
and what to tell the user, never a raw HTTP body or an exception.

A deploy runs for tens of minutes. `watch` follows the job's server-sent event stream for a bounded time
and returns what happened in that window, so the model can relay progress and call it again; the job runs
on accounts whatever the agent does, and a turn that ends mid-deploy loses nothing.
"""
import asyncio
import json
import re
from typing import Any, Optional

import httpx
import yaml

#: The accounts REST base. main.py's reporters use it too, so there is one copy of it.
API = "/datafye-accounts-api/v1"
#: Longest the model may ask `watch` to wait in one call. A turn can carry several calls.
MAX_WATCH_SECONDS = 600
#: How much of the log one `watch` hands the model: the tail, which is where the news is.
LOG_TAIL_CHARS = 3000
#: accounts' rule for a deployment name: it names every cloud resource the deployment creates.
NAME = re.compile(r"^[a-z][a-z0-9-]{0,30}[a-z0-9]$")
#: accounts' ids. Ids come from the MODEL and become URL path segments sent with the user's session,
#: so anything else is refused before a request is made: a crafted id ("../accounts/...") would
#: otherwise reach another accounts route as the user.
DEPLOYMENT_ID = re.compile(r"^dep-[0-9]{1,18}$")
JOB_ID = re.compile(r"^job-[0-9]{1,18}$")
OPERATIONS = ("upgrade", "start", "stop", "deprovision")


def default_name(username: str, project_id: Optional[str]) -> str:
    """`<username>-<project tail>`: unique per user and project in Datafye's one cloud account, where
    every granted user's deployments share the namespace, and valid under accounts' rule. The username is
    shortened, never the project tail, so a long username still gives each project its own name."""
    tail = re.sub(r"[^a-z0-9]", "", (project_id or "").lower())[-8:] or "env"
    user = re.sub(r"[^a-z0-9-]", "", (username or "user").lower()).strip("-") or "user"
    if not user[0].isalpha():
        user = "u" + user
    user = user[:32 - 1 - len(tail)].rstrip("-")
    return f"{user}-{tail}"


def _refusal(resp: httpx.Response) -> str:
    """accounts' own reason, which names what to do (the grant, the state, the busy job). A 401 is the
    session, not a permission: it has expired or been revoked, and a new message brings a fresh one."""
    try:
        body = resp.json()
        reason = body.get("message") or body.get("error") or resp.text
    except ValueError:
        reason = resp.text
    if resp.status_code == 401:
        reason = ("the user's session has expired or been revoked, so accounts would not answer. This is not a "
                  "refusal of the deployment: the next message the user sends carries a fresh session")
    return f"accounts refused it (HTTP {resp.status_code}): {reason}"


def _int(value: Any, default: int) -> int:
    """A number accounts sent, or the default when it sent none or something else."""
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default
    return result


async def _send(method: str, url: str, token: str, body: Optional[dict],
                client: Optional[httpx.AsyncClient]) -> tuple[Optional[httpx.Response], Optional[dict], Optional[str]]:
    """One call to accounts with the user's session: (response, its JSON or None, a transport failure or None)."""
    own = client is None
    client = client or httpx.AsyncClient(timeout=30.0)
    resp, data, failure = None, None, None
    try:
        resp = await client.request(method, url, json=body, headers={"Authorization": f"Bearer {token}"})
        try:
            data = resp.json() if resp.content else {}
        except ValueError:
            data = None
    except httpx.HTTPError as e:
        failure = f"accounts could not be reached ({e.__class__.__name__})"
    finally:
        if own:
            await client.aclose()
    return resp, data, failure


async def project_descriptor(base: str, token: str, username: str, project_id: str,
                             client: Optional[httpx.AsyncClient] = None) -> tuple[Optional[str], Optional[str]]:
    """The descriptor accounts has recorded for this project (the agent reports it whenever the
    project's environment changes): (descriptor or None, why not)."""
    resp, data, failure = await _send("GET", f"{base}{API}/accounts/{username}/projects/{project_id}", token, None, client)
    if failure:
        result = (None, failure)
    elif resp.status_code // 100 != 2 or data is None:
        result = (None, _refusal(resp))
    else:
        result = ((data.get("deployment_descriptor") or None), None)
    return result


def same_environment(running: str, recorded: str) -> bool:
    """Whether two descriptors describe the same environment: compared as parsed YAML, so formatting,
    comments and key order do not matter."""
    try:
        same = yaml.safe_load(running) == yaml.safe_load(recorded)
    except yaml.YAMLError:
        same = False
    return same


async def create(base: str, token: str, name: str, descriptor: str, project_id: Optional[str],
                 version: Optional[str], client: Optional[httpx.AsyncClient] = None) -> dict[str, Any]:
    """Create the deployment and queue its provision. Returns {ok, text, deployment_id?, job_id?}."""
    if not NAME.match(name or ""):
        result = {"ok": False, "text": f"'{name}' is not a valid deployment name: 2-32 lowercase letters, "
                                       "digits and hyphens, starting with a letter. Nothing was created."}
    elif not descriptor or not descriptor.strip():
        result = {"ok": False, "text": "There is no local environment to deploy: the project's foundry has no "
                                       "descriptor. Build the environment locally first. Nothing was created."}
    else:
        body: dict[str, Any] = {"name": name, "descriptor": descriptor}
        if project_id:
            body["project_id"] = project_id
        if version:
            body["version"] = version
        resp, data, failure = await _send("POST", f"{base}{API}/deployments", token, body, client)
        if failure:
            # the request may or may not have arrived: never invite a blind retry, which could make a second
            result = {"ok": False, "text": f"Creating the deployment failed: {failure}. It may or may not have been "
                                           "created; call list_deployments before trying again."}
        elif resp.status_code // 100 != 2:
            result = {"ok": False, "text": f"The deployment was not created: {_refusal(resp)}. Nothing was changed."}
        elif data is None:
            result = {"ok": False, "text": "accounts accepted the deployment but its answer could not be read; call "
                                           "list_deployments to find it before trying again."}
        else:
            dep = (data.get("deployment") or {}).get("id")
            job = (data.get("job") or {}).get("id")
            result = {"ok": True, "deployment_id": dep, "job_id": job,
                      "text": f"Deployment '{name}' created ({dep}); its provision is job {job}, queued on "
                              "accounts. It takes 20-30 minutes and runs whether or not this conversation "
                              f"continues. Follow it with deployment_progress(deployment_id='{dep}', job_id='{job}')."}
    return result


async def run(base: str, token: str, deployment_id: str, operation: str, version: Optional[str] = None,
              system: Optional[str] = None, confirm_name: Optional[str] = None,
              client: Optional[httpx.AsyncClient] = None) -> dict[str, Any]:
    """Run an operation on a deployment. Returns {ok, text, job_id?}.

    A deprovision removes the deployment and everything it runs on, so it also needs `confirm_name`: the
    deployment's NAME, as the user confirmed it, checked against accounts before anything is sent."""
    result = None
    if operation not in OPERATIONS:
        result = {"ok": False, "text": f"'{operation}' is not an operation: use one of {', '.join(OPERATIONS)}."}
    elif not DEPLOYMENT_ID.match(deployment_id or ""):
        result = {"ok": False, "text": f"'{deployment_id}' is not a deployment id (dep-<n>; list_deployments has them)."}
    elif operation == "deprovision":
        resp, data, failure = await _send("GET", f"{base}{API}/deployments/{deployment_id}", token, None, client)
        readable = not failure and resp.status_code // 100 == 2
        name = ((data or {}).get("deployment") or {}).get("name") if readable else None
        if name is None:
            why = failure or (_refusal(resp) if resp is not None else "no answer")
            result = {"ok": False, "text": f"Nothing was deprovisioned: the deployment could not be read to confirm "
                                           f"its name ({why})."}
        elif (confirm_name or "").strip() != name:
            result = {"ok": False, "text": f"Nothing was deprovisioned: this removes '{name}' and everything it runs on. "
                                           f"Ask the user to confirm by name, then pass confirm_name='{name}'."}
    if result is None:
        body: dict[str, Any] = {"operation": operation}
        if version:
            body["version"] = version
        if system:
            body["system"] = system
        resp, data, failure = await _send("POST", f"{base}{API}/deployments/{deployment_id}/jobs", token, body, client)
        if failure:
            result = {"ok": False, "text": f"The {operation} request failed: {failure}. It may or may not have been "
                                           "queued; call list_deployments to see before trying again."}
        elif resp.status_code // 100 != 2:
            result = {"ok": False, "text": f"The {operation} was not started: {_refusal(resp)}"}
        else:
            job = ((data or {}).get("job") or {}).get("id")
            result = {"ok": True, "job_id": job,
                      "text": f"The {operation} of {deployment_id} is job {job}, queued on accounts. Follow it "
                              f"with deployment_progress(deployment_id='{deployment_id}', job_id='{job}')."}
    return result


async def list_deployments(base: str, token: str, client: Optional[httpx.AsyncClient] = None) -> dict[str, Any]:
    """The user's deployments, one line each. Returns {ok, text, deployments?, can_create?}."""
    resp, data, failure = await _send("GET", f"{base}{API}/deployments", token, None, client)
    if failure:
        result = {"ok": False, "text": f"The deployments could not be listed: {failure}"}
    elif resp.status_code // 100 != 2 or data is None:
        result = {"ok": False, "text": f"The deployments could not be listed: {_refusal(resp)}"}
    else:
        deployments = data.get("deployments") or []
        lines = []
        for d in deployments:
            line = f"- {d.get('name')} ({d.get('id')}): {d.get('state')}"
            if d.get("version"):
                line += f", Datafye {d.get('version')}"
            if d.get("active_job_id"):
                line += f", job {d.get('active_job_id')} running"
            if d.get("url"):
                line += f", API {d.get('url')}"
            if d.get("actions"):
                line += f"; can: {', '.join(d.get('actions'))}"
            lines.append(line)
        can = ("may create a new one" if data.get("can_create")
               else "may NOT create one: an admin grants cloud deployments per account")
        text = ("No cloud deployments yet." if not lines else "Cloud deployments:\n" + "\n".join(lines))
        result = {"ok": True, "deployments": deployments, "can_create": bool(data.get("can_create")),
                  "text": f"{text}\nThis user {can}."}
    return result


def _parse_events(lines: list[str]) -> list[tuple[Optional[str], str, dict]]:
    """Server-sent events from their lines: (id, event, data). Comments and blank keep-alives are skipped."""
    events, ev_id, ev_name, data = [], None, "message", []
    for line in lines:
        if line == "":
            if data:
                try:
                    payload = json.loads("\n".join(data))
                except ValueError:
                    payload = {"raw": "\n".join(data)}
                events.append((ev_id, ev_name, payload if isinstance(payload, dict) else {"raw": payload}))
            ev_id, ev_name, data = None, "message", []
        elif line.startswith("id:"):
            ev_id = line[3:].strip()
        elif line.startswith("event:"):
            ev_name = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    return events


async def watch(base: str, token: str, deployment_id: str, job_id: str, offset: int = 0, seconds: int = 120,
                client: Optional[httpx.AsyncClient] = None) -> dict[str, Any]:
    """Follow a job's event stream for up to `seconds`, then return what happened in that window:
    {ok, done, status, next_offset, text}. The tail of the new log is included; call again with
    `next_offset` to continue where this left off. The job is unaffected by the watch ending."""
    seconds = max(5, min(_int(seconds, 120), MAX_WATCH_SECONDS))
    status, error, next_offset, done = None, None, max(0, _int(offset, 0)), False
    log: list[str] = []
    refused: Optional[str] = None   # accounts said this watcher may not follow the job
    broke: Optional[str] = None     # the stream failed; the job itself is unaffected
    if not DEPLOYMENT_ID.match(deployment_id or "") or not JOB_ID.match(job_id or ""):
        refused = f"'{deployment_id}' / '{job_id}' are not a deployment and job id (dep-<n>, job-<n>)"
    else:
        own = client is None
        client = client or httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=seconds + 20.0))
        try:
            async with client.stream("GET", f"{base}{API}/deployments/{deployment_id}/jobs/{job_id}/events",
                                     params={"offset": next_offset},
                                     headers={"Authorization": f"Bearer {token}", "Accept": "text/event-stream"}) as resp:
                if resp.status_code // 100 != 2:
                    await resp.aread()
                    refused = _refusal(resp)
                else:
                    loop = asyncio.get_running_loop()
                    deadline = loop.time() + seconds
                    iterator = resp.aiter_lines()
                    lines: list[str] = []
                    reading = True
                    while reading and not done and loop.time() < deadline:
                        try:
                            line = await asyncio.wait_for(iterator.__anext__(), timeout=max(0.1, deadline - loop.time()))
                        except (asyncio.TimeoutError, StopAsyncIteration):
                            reading = False
                            line = None
                        if line is not None:
                            lines.append(line)
                            if line == "":
                                for ev_id, name, data in _parse_events(lines):
                                    if name == "log":
                                        log.append(str(data.get("text", "")))
                                        next_offset = _int(ev_id, next_offset)
                                    elif name == "status":
                                        status = data.get("status")
                                    elif name == "done":
                                        status, error, done = data.get("status"), data.get("error"), True
                                        next_offset = _int(data.get("next_offset"), next_offset)
                                    elif name == "error":
                                        refused = str(data.get("error"))
                                        done = True
                                    elif name == "reconnect":
                                        # accounts ended a long stream with the job still running
                                        next_offset = _int(data.get("next_offset"), next_offset)
                                        reading = False
                                lines = []
        except httpx.HTTPError as e:
            broke = f"the progress stream broke ({e.__class__.__name__})"
        finally:
            if own:
                await client.aclose()
    tail = "".join(log)
    if len(tail) > LOG_TAIL_CHARS:
        tail = "...\n" + tail[-LOG_TAIL_CHARS:]
    log_text = "\nNew log:\n" + tail if tail.strip() else "\n(no new log lines)"
    seen = status or "of unknown status (none reported in this window)"
    if refused:
        result = {"ok": False, "done": False, "status": status, "next_offset": next_offset,
                  "text": f"Could not follow job {job_id}: {refused}" + (log_text if tail.strip() else "")}
    elif done:
        result = {"ok": True, "done": True, "status": status, "next_offset": next_offset,
                  "text": f"Job {job_id} has ended: {status}." + (f" Error: {error}" if error else "") + log_text}
    elif broke:
        result = {"ok": False, "done": False, "status": status, "next_offset": next_offset,
                  "text": f"Stopped following job {job_id}: {broke}. The job itself carries on, last seen {seen}; "
                          f"call deployment_progress again with offset={next_offset}." + log_text}
    else:
        result = {"ok": True, "done": False, "status": status, "next_offset": next_offset,
                  "text": f"Job {job_id} is {seen} after this {seconds}s look; it carries on regardless. "
                          f"Call deployment_progress again with offset={next_offset} to continue." + log_text}
    return result
