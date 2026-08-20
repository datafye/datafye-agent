# CLAUDE.md

This file provides guidance to Claude Code when working with this repository.

## Project Overview

Datafye Agent is a dedicated per-user AI backend for algorithmic trading project development. It wraps the Claude Agent SDK in a FastAPI service, giving each user an interactive agent session with access to Datafye documentation, the Datafye CLI, and file system tools for building Python-based algos.

## Technology Stack

- Python 3.13+
- FastAPI + Uvicorn (HTTP/SSE)
- Claude Agent SDK (Anthropic)
- Pydantic (request/response models)

## Project Structure

```
datafye-agent/
├── main.py          # FastAPI app, endpoints, SSE streaming, session management
├── prompt.py        # System prompt builder (assembled from runtime context, incl. memory + skills blocks)
├── auth.py          # JWT validation against accounts' JWKS (with clock-skew leeway)
├── credentials.py   # Encrypted on-disk credentials store
├── broker.py        # ConnectTrade broker integration
├── conversations.py # Per-user project store — one FOLDER per project (meta.json + CLAUDE.md + PROJECT.md + memory/ + .claude/skills/)
├── memory.py        # Cross-session memory: global store + the memory-protocol block injected into the prompt
├── skills.py        # Skill plugin wiring (system + user-global plugins) and GET /v1/skills listing
├── warmth.py        # Warm signal: is real work in flight (feeds /health active_proxied_apps)
├── foundry.py       # Derived environment readiness (intent + observation + in-flight) behind /health's `foundry` block
├── harness.py       # Which Claude Code CLI actually runs a turn (DAT-215): the `harness` block on /v1/bom
├── paths.py         # Single agent state-root (DATAFYE_AGENT_STATE_DIR) all per-user state derives from
├── plugins/datafye/ # System (predefined) skills, installer-managed/read-only — ship with the app clone
├── tests/sanity_e2e.py  # Manual end-to-end sanity suite (real agent + real model calls; not CI)
├── tests/test_prompt_audit.py  # Dependency-free prompt audit; renders the REAL prompt and pins every claim that has been wrong
├── requirements.txt # Python dependencies (incl. pyyaml for env_status descriptor parsing)
├── Dockerfile       # Legacy (agent now runs natively, Docker used for Datafye env containers)
├── install/
│   ├── install_template.sh   # Installer/upgrader template (--mode hosted|standalone, --ami-cleanup)
│   ├── first-boot.sh         # Marketplace/standalone first-boot script (reads EC2 user data, runs installer)
│   ├── foundry-boot.sh       # Foundry boot reconciler, all modes (datafye-foundry-boot.service)
│   ├── upgrade-check.sh      # Auto-upgrade cron script
│   └── publish_installer.sh  # Publishes versioned installer to downloads server
├── CLAUDE.md        # This file
└── PROJECT.md       # Detailed project documentation
```

## Running Locally

```bash
# Install dependencies
pip install -r requirements.txt

# Path environment variables
export DATAFYE_AGENT_DOCS_DIR="/path/to/datafye-docs"
export DATAFYE_AGENT_CLI_PATH="/path/to/datafye"
export DATAFYE_AGENT_WORKSPACE="/path/to/workspace"
export DATAFYE_AGENT_SAMPLES_DIR="/path/to/datafye-samples"
# Optional: relocate ALL writable state (credentials, projects, user skills,
# user memory) under one root — handy to keep local runs out of ~/.datafye
export DATAFYE_AGENT_STATE_DIR="/path/to/scratch/agent-state"

# Local-dev credential seed (production delivers these as credentials).
# These are folded into the encrypted credentials store the first time it
# is created — see _credential_env_seed() in main.py.
export DATAFYE_AGENT_ANTHROPIC_API_KEY="sk-ant-..."
export DATAFYE_AGENT_MASSIVE_API_KEY="..."
export DATAFYE_AGENT_CONNECTTRADE_CLIENT_ID="..."
# ... etc

# Run
python main.py
```

Service starts on port 18780 by default (`DATAFYE_AGENT_PORT`).

The agent boots into an **awaiting-bootstrap** holding state — only `GET /health`
and `POST /bootstrap` respond; every user-facing endpoint returns HTTP 503. The
accounts service drives it out of that state by pushing an accounts-signed
bootstrap JWT (see [API Endpoints](#api-endpoints) below). For local testing you
mint a bootstrap token yourself and `POST /bootstrap` it.

## Deployment

The agent runs **natively** on the host (not in a Docker container). Docker is installed on the instance for Datafye environment containers that the agent manages via the CLI.

### Two Deployment Modes

| Mode | Use Case | What's on the Instance |
|------|----------|----------------------|
| `hosted` | Rumi cloud sandbox (managed by accounts service) | Agent, CLI, docs, samples pre-installed. No nginx/SSL (jump server proxies). Identity, credentials and the Anthropic key are delivered by the accounts service over HTTP (`POST /bootstrap`) — nothing user-specific is baked into the AMI. |
| `standalone` | AWS Marketplace / DIY | First-boot script only. Downloads and installs everything on first boot from user data. Includes nginx + SSL. The Anthropic key arrives via the accounts credentials channel (no longer baked into EC2 user data or passed with `--anthropic-key`). |

### Installer

The version is baked into `install.sh` by `publish_installer.sh` — no `--version` flag needed.

```bash
# Hosted mode (Rumi cloud sandbox)
sudo ./install.sh --mode hosted

# Standalone mode (marketplace)
sudo ./install.sh --mode standalone --dns agent.mycompany.com
# (No --anthropic-key flag: the Anthropic key arrives from accounts over the
#  credentials channel for both hosted and standalone. The installer always
#  starts the agent; it boots awaiting-bootstrap.)

# Upgrades happen automatically via the auto-upgrade cron (preserves config, mode, credentials)
```

### Auto-upgrade never restarts mid-turn (`7447964`)

The auto-upgrade cron runs **every minute under `flock -n`** (a tick is a no-op while a prior check or an in-flight install still holds the lock), replacing the old blind `*/5 * * * *`. `upgrade-check.sh` **idle-gates** before it downloads/runs `install.sh`: it proceeds only when the agent's own `/health` reports `running_jobs==0` AND `active_proxied_apps==[]` AND `now - last_chat_activity_at >= DATAFYE_UPGRADE_INACTIVITY_WINDOW` (default 120s); otherwise it logs "deferred" and retries next tick. It adds a download **jitter** (`DATAFYE_UPGRADE_JITTER_SECONDS`, default 60 — `downloads.n5corp.com` is a single origin/no CDN), and the **top of `install.sh` does a last-moment `running_jobs` re-check that aborts the upgrade** if a turn started in the meantime — armed ONLY on the auto-upgrade path via the env flag `DATAFYE_AUTO_UPGRADE=1` (set when upgrade-check pipes `curl install.sh | DATAFYE_AUTO_UPGRADE=1 bash`), so fresh/manual installs are never blocked. Net: the agent never restarts mid-turn (which would drop the in-flight resumable-turn buffer). Unreachable `/health` → proceed (nothing to protect), **now with a back-off; see DAT-225 below, where that rule read the script's own damage as permission to keep going**. Caveat: one transitional blind upgrade per box before it's gated; takes effect on the next publish + re-bake/auto-upgrade.

⚠️ **The idle gate learns "is work in flight?" only from `/health`, which is UNFILED and still open.** An unreachable agent therefore means *proceed*, even while `foundry-boot.sh` holds a provision that has nothing to do with the agent process. The right source is the **DAT-183 marker on disk** (`~/.datafye/run/cli-*.json`), which needs no agent at all to answer, and which `warmth.py` already reads for exactly this question.

### An upgrade must be able to put the box back (DAT-225)

An upgrade to 2.0.39 **wedged a box for as long as anyone watched**: the agent went
down at installer step 4 and never came back, and the once-a-minute cron repeated the
identical failure forever. Three faults, each harmless alone:

1. **`cp -a` in the AMI-bake path preserves ownership.** `/opt/datafye/agent/app`
   inherited whoever owned the BUILD checkout, so every later upgrade ran `git` there
   as root and git refused with *"detected dubious ownership"*. Fixed by
   `chown -R root:root` after the copy (root ownership is what makes the tree
   read-only to the agent, so it is what the tree should have had anyway) plus
   `-c safe.directory` on the `git` calls in `clone_or_update_repo`, **scoped per
   call rather than written globally** so the exemption cannot outlive the command
   or widen to a repo we did not put there.
2. **The installer stops the agent at step 4 and starts it at step 11**, with `set -e`
   and nothing in between to put it back, so ANY failure in the whole install left the
   box down. There is now a **trap** that restarts the previous agent on a non-zero
   exit, cleared just before the deliberate start so the normal path is untouched (the
   old code is still on disk when an install dies partway, so starting it restores a
   working agent). ⚠️ **Never take a service down at a point from which the script
   cannot guarantee it comes back.**
3. **`upgrade-check.sh` read an unreachable `/health` as "nothing to protect, proceed"**,
   correct in isolation, and exactly wrong when the thing that made `/health`
   unreachable was this script. Failures now back off **1, 5, 15, 30, 60 minutes** and
   log why. It never gives up entirely: a fix published upstream must still reach a
   wedged box unattended.

⚠️ **A fix for something that breaks upgrades cannot arrive by upgrade.** Boxes already
carrying the bad ownership need a re-bake or a manual `chown`; the same shape as the
`--pin` bug above.

### The prompt drifts, and only an audit finds it (2026-08-10)

A full read-through of the rendered prompt against current code found **five wrong
claims**, none of which any test or review had caught. They matter more than
ordinary stale docs: the prompt is the model's only account of its own world, so a
wrong line there becomes wrong behaviour.

| Claim | Reality |
|---|---|
| `curl …local-foundry-dev-api-**rest**.datafye.local:7776/openapi` | That host does not exist. **The exact mis-guess DAT-209 was filed about** — sitting in the same prompt that now warns against it |
| Readiness "is recorded by whatever last changed the environment" | DAT-198 made readiness **derived**; DAT-199's boot service writes nothing. This described the design that was built and *reverted* |
| Datasets are "SIP, Crypto, Palpha, HWAI, Synthetic" | Palpha is not provisionable (DAT-155, open) and appears nowhere in the deploy engine. The `dataset add` line two lines below already said SIP/Crypto/Synthetic — the prompt contradicted itself |
| "Algo code is stored in GitHub repos" | GitHub is optional credentials most users do not have. Stated unconditionally |
| "PLAIN ASCII PUNCTUATION ONLY … no em dashes" | The prompt used **52 em dashes**. Models imitate the register of their instructions, and non-ASCII breaks the accounts store |

⚠️ **Four of these were found earlier the same day purely as side effects** of other
work (the `status` verdict list, "the timeout WILL kill it", "no tool to read a
background process's output", the crypto gotchas). Accidental discovery at that rate
is the signal that a deliberate audit is overdue — **the prompt needs re-reading in
full whenever a batch of tickets lands**, because each ticket changes the world the
prompt describes without touching the prompt.

`tests/test_prompt_audit.py` pins every one of these in **14 checks**, including a
blanket non-ASCII check and (since DAT-222) the intent classifier's own prompt (that
file is for prompts that have **been wrong**, and the classifier now qualifies), so they
cannot come back quietly. Run it with `python3 tests/test_prompt_audit.py`
— no pytest, no dependencies, because a test that is awkward to run does not get run.
It renders the REAL prompt through `build_system_prompt` rather than grepping the
source: the quant stack, the bash ceiling and the app preview base are all composed at
build time, so a source-level check would pass while the rendered prompt was wrong.

⚠️ **That file did not exist for a day after this paragraph first claimed it did**
(written 2026-08-11; the claim was made 2026-08-10). The checks were written as a
throwaway script during the audit, run, and then documented as though they had been
committed. So the whole day's findings sat unguarded behind a sentence saying they were
pinned — **strictly worse than saying nothing**, because it stops anyone looking. Two
things follow. **A safeguard is not real until it is committed and runs from a clean
checkout**; "I ran the checks" and "the checks exist" are different claims, and only the
second is worth writing down. And when it was finally written, it immediately failed
twice — the prompt still carried 9 `⚠️` and 3 `→` after the em-dash sweep, and the port
band still contained platform-occupied ports. Both had been "already handled".

⚠️ **Every check must be proven to FAIL on the bug it guards.** Each one is run against
the real historical defect reintroduced (wrong hostname, the reverted readiness wording,
an em dash, a renamed marker field). This repo has already shipped a test suite whose
assertions silently never matched and passed vacuously; a green suite is evidence of
nothing until you have watched it go red.

**A second pass checked the prompt against the docs ON DISK**, which is a different
surface: the prompt tells the model to trust those docs, so a contradiction there is
as damaging as a wrong prompt line. What held up: the REST-reference-is-stubs claim
(76 pure embed pages), crypto having no quotes, every CLI command named, and the
hostnames — which match the docs exactly, and where `-api-rest` appears in **none** of
183 occurrences. Cross-checking the docs would have caught that bug outright.

Two real problems came out of it, and the prompt now handles both:

- ⚠️ **The docs are written for a self-hosted reader who provisions their own
  environment** — the environment guides open with `foundry local provision`, which is
  precisely what a sandbox user must not do. Telling the model "ALWAYS check the docs"
  was pointing it at guidance that contradicts its situation. It is now told to take
  descriptors, schemas, syntax and concepts from the docs, and **environment lifecycle
  from the prompt**.
- ⚠️ **The docs trail the platform**: `foundry local status` is absent entirely, and so
  are DAT-200's `IN PROGRESS` / `PARTIAL` verdicts. The model is now told that **doc
  silence is not evidence of absence** for recently-shipped things — check `--help` or
  `/openapi` before declaring something unsupported. Filed as **DAT-216**.

### Projects, not strategies

The entity is a **project** everywhere now. Accounts mints project ids, the SPA
shows projects, and the user thinks in projects — "strategy" was only ever the
agent's private word for the same thing, and the scope had outgrown it: a project
can be a dashboard, a piece of research or a tool, not just a trading strategy.

On disk: `<state>/projects/<id>/`. In code: `project_dir`, `projects_base`,
`project_cwd`, `DATAFYE_AGENT_PROJECTS_DIR`. In the prompt and memory protocol:
PROJECT memory, per-project skills.

⚠️ **No back-compat, by decision (Girish, 2026-08-10): assume a fresh provision.**
There is no migration from `strategies/`, no fallback for the old env vars, and
the older `conversations/<id>.json` migration was deleted with them. **An upgraded
box carrying `strategies/` would come up with no projects at all** — its data is
still on disk, but nothing reads that path any more. Boxes carrying real project
data must be **reprovisioned, not upgraded**, or have the directory renamed by hand
first. Nothing outside this repo referenced the path or the env var (checked across
accounts, Yukti, the CLI and the installer), so the blast radius is the agent alone.

⚠️ **`conversation_id` STAYS — settled, not deferred** (Girish, 2026-08-10). It is the
accounts-minted id threaded through `/v1/chat` and the legacy `/v1/conversations`
endpoints, so it is an API name rather than the vocabulary this rename was about.
Do not "finish the job" by renaming it later: the inconsistency is deliberate, and
the churn would reach the wire format for no gain. The `GET /v1/skills` tier value
did move (`user-strategy` → `user-project`), which was safe because nothing
consumes it.

### Showing the user an app the model built (DAT-202)

Sutra's refine-preview mechanism, reused rather than reinvented: a **reserved port
band** the jump server routes straight through, so there is no per-app route to
register, nothing to allocate centrally and nothing to leak.

- **`https://<username>.app.datafye.io:<port>`**, port from
  `DATAFYE_AGENT_APP_PORT_RANGE` (**`10010-10019`**, moved off `8080-8089` by
  DAT-220, see below). Host is `DATAFYE_AGENT_APP_PREVIEW_HOST`; the base is
  composed **per turn** in `main.py` because it needs the bootstrapped username.
- **`.datafye-app-<port>.json`** in the project folder,
  `{"name": …, "port": …, "pid": …}`, is what makes the app a warm signal.
  `warmth.running_apps()` globs `APP_MARKER_GLOB` (`.datafye-app*.json`) and reports
  each as **`compute:<name>`**, finally filling the label space DAT-184 reserved.

**⚠️ ONE MARKER PER APP, not one per project (DAT-221).** The marker used to be a
single `.datafye-app.json`, so a project could keep only one app's worth of the box
awake: a user with two dashboards had one invisible to the warm signal, and stopping
the tracked one let the idle monitor dorm the box while the other was still serving a
page they had open. **The band holds ten ports precisely so several apps can run at
once, so a one-per-project marker contradicted the reason the band exists.**

- **A file per app, deliberately, rather than a list inside one file.** Starting an
  app writes one file and stopping it deletes one file, so two apps never
  read-modify-write anything shared and neither can lose the other's entry. The
  self-healing property stays *per app*: a stale marker whose port is dead is ignored
  on its own, without taking a live sibling with it.
- **The glob also matches the legacy single `.datafye-app.json`, on purpose.** An agent
  upgrade while an app is running would otherwise orphan that app's marker and let the
  box go cold with the user's page open, the exact failure this fixes, caused by the
  fix. Entries are **deduped by port**, so a leftover legacy marker naming the same
  port as its successor cannot report one dashboard twice.
- ⚠️ **Found only because the model noticed.** It flagged that the keep-awake mechanism
  tracks one app at a time, repointed the marker to the survivor by hand, and wrote a
  note in the project's `CLAUDE.md` to remember the untracked port. **It used project
  memory as a substitute for a mechanism that did not exist.** Behaviour that depends
  on the model spotting an infrastructure gap is not a guarantee, and it paid tokens
  for the workaround on every turn.

**⚠️ The app server is the ONE carve-out from the no-background rule (DAT-219).** The
prompt told the model to serve an app that outlives the turn while DAT-185 forbade
backgrounding "not for anything else", so there was **no legal way to start it**, and
every piece around the app (band, marker, warm signal, jump-server route) had been
built around a hole where the app itself should be. The exception is narrow and follows
from what the ban is *for*: all three of its reasons (you cannot await it, kill it, or
see it finish) are supervision problems, and none holds for a server. Orphaning is the
required behaviour, finishing is failure, and liveness is the listening port `warmth.py`
already probes. The rule the model is given: **if it produces a result, run it in the
foreground; if it answers on a port, detach it.**

The prompt prescribes the exact shape:

```
setsid nohup <command> > app.log 2>&1 < /dev/null &
sleep 2
ss -ltnp | grep :<port>
```

- **That `ss` line is not a formality.** It proves the app is really listening, confirms
  it bound `0.0.0.0` rather than loopback, and yields the **pid** (`$!` is unreliable
  because `setsid` forks).
- **The pid in the marker is ADVISORY ONLY.** It exists so a later turn can stop the app
  cleanly instead of guessing with `pkill -f`. `warmth.running_apps()` carries it and
  **never lets it influence liveness**: a recycled pid would pin a box awake for an
  unrelated process, and a live pid whose port is dead is a crashed app that must not
  report warm. **The port remains the sole liveness test.**
- **The model picks a FREE port first** (`ss -ltn` before it binds, DAT-220 AC). Without
  that its own second app collides with its first, and the recovery is a failed start,
  the same experience the band move was meant to end, with the model as the cause rather
  than the platform. The check deliberately does not grep for the band's digits: a
  hardcoded `':1001[0-9]'` would silently stop matching the day the band moves again,
  and `APP_PORT_RANGE` is the single place the band is written down.

⚠️ **A marker is a claim; a listening port is the fact.** Only markers whose port
actually answers are reported, so a crashed app or an abandoned marker stops
keeping the box awake by itself. That self-healing is why this is a probe and not
a registry — nothing has to be reliably cleaned up.

⚠️ **Probe BOTH loopback and the private IP** — this is the RUMI-369 lesson, not
belt-and-braces. An app bound only to the private interface is reachable through
the jump server and is a real running app, but a loopback-only check calls it dead
and the box gets stopped while the user is looking at it.

⚠️ **The URL is NOT authenticated, and that is the decision, not an oversight**
(Girish, 2026-08-10): auth is driven by the user and baked into the app the model
builds. The prompt therefore tells the model plainly that anyone with the URL can
open it, that protection must go *inside* the app if the content warrants it, and
to say what it did and did not protect. Sutra's band has the same posture today.

⚠️ **Self-hosted agents get no URL.** With no jump server there is no external
route, so an empty preview host makes the prompt describe the app as local-only
rather than handing the user a link that cannot resolve.

⚠️ **The band moved 8080-8089 → 10010-10019 (DAT-220), and the jump server must move
with it.** `rumi-solace` publishes **8080** and `rumi-influxdb` publishes **8086** on
every box, empty foundry or not, so the band's first port (the one anything picks by
default) could never bind. The band came from Sutra, where it is correct because no
Datafye platform runs underneath it: **reusing the mechanism was right; reusing the
numbers carried an assumption that did not survive the move.** The platform sprawls
across 8000/8001/8008/8080/8086/8443/8883, so the fix leaves the neighbourhood entirely
rather than shuffling within it. **Not 10000**, because that is Webmin's conventional
default, and putting our first port where something else conventionally lives is the
exact mistake being fixed. A fully provisioned box publishes nothing in 1001x (its REST
services are in the 7xxx range).

⚠️ **The jump-server half is NOT in this repo and is not done.** nginx must route
`<username>.app.datafye.io:10010-10019` → `<username>.rumi.local:<port>` and the
jump SG must allow the band. Note both `datafye-accounts` and `nvx-accounts` pass
`proxyInfo = null` at launch, so neither wires a band today — this is a bastion
config change, not an accounts code change.

### Node is installed, project-local by default (DAT-201)

Node **v24.19.0 LTS ("Krypton")** plus npm and npx, from the official tarball into
`/opt/node-v<ver>-linux-<arch>`, symlinked into `/usr/local/bin`. Same idiom as
Maven above, and **pinned** — a runtime that silently differs per box is a class of
bug already paid for (DAT-215). Both `x64` and `arm64` are handled; an unknown
architecture warns and skips rather than aborting the install.

The lifecycle tracks had been promising this all along (`dashboard`/`app`/`tool` →
Explore → Design → Build → Ship) with nothing to build with. The concrete failure was
smaller: the model reached for a bundled skill's palette validator, reported *"No Node
in this sandbox"*, and proceeded on documented values instead of checking its work.

**Dependencies are project-local**, mirroring the per-project `.venv`. `npm install`
inside a project folder writes that project's `node_modules`; nothing is shared and no
project can disturb another.

⚠️ **The trap is `npm install -g`**, whose default prefix is the Node tree in `/opt` —
root-owned, exactly like the agent's own venv and for the same deliberate reason. The
installer sets `prefix=/home/datafye/.npm-global` in **`~datafye/.npmrc`** (npm reads it
whatever the shell is; `/etc/profile.d` would not, since a model's Bash command is not a
login shell) and pre-creates `~datafye/.npm` — because if anything ever runs npm as
root, the resulting root-owned cache breaks every later install.

⚠️ **`main.py` adds `~/.npm-global/bin` to `PATH`.** The profile script reaches an
operator's login shell but not the model, so without this a tool the model just
installed globally would not be runnable by its next command.

**Disk (DAT-178).** The runtime is **209 MB extracted** (30 MB compressed) — comparable
to the whole 292 MB quant stack, one time. The part that grows is per-project
`node_modules`, which multiplies by project count and can be 100 MB+ each for a
framework app. On a single root volume that is the number to watch, and it is a fresh
argument for DAT-178 rather than something this ticket solves.

**No framework baseline is pre-installed**, deliberately: pre-warming a stack would
presume one (React? Vue? none?) on evidence that only ever showed a need to *run a JS
file*, and it multiplies the disk above. The prompt therefore tells the model plainly
that a first `npm install` hits the network, and points it at plain HTML or matplotlib
when those suffice.

### systemd's PATH is not a login PATH, and `ss` lived in `/usr/sbin`

`_EXTRA_BIN_DIRS` in `main.py` is now `[~/.npm-global/bin, /usr/sbin, /sbin]`, prepended
to the process `PATH` the model's Bash tool inherits.

The model's **first command of its first app** was `ss -ltn`, exactly as `prompt.py`
tells it to run. It failed. systemd hands a service a minimal `PATH` with no `/usr/sbin`
in it, and `ss` is what the prompt calls for **twice per app**, once to pick a free port
and once to verify the bind.

⚠️ **It recovered every time, which is why this would never have surfaced.** The next
command fell back to `/usr/sbin/ss` and the app came up fine, so the whole cost was a
wasted round trip on every app for the life of the product: tokens and latency and
nothing else. **Only visible by reading a transcript, never by a check**, because
nothing fails.

**Fixed on `PATH`, not by hardcoding `/usr/sbin/ss` into the prompt.** The path is a
property of the box, not of the instruction, and the next sbin tool the model reaches for
would hit the same wall. Adding sbin to a non-root `PATH` is safe: the binaries are
world-executable and simply do less without privileges (`ss -p` shows this user's own
pids, which is all it needs).

### The quant stack has one source of truth (DAT-210)

`install/quant-stack.txt` lists the packages installed into the **system**
interpreter for project code. The installer installs exactly those, and `prompt.py`
reads **the same file** to tell the model what it already has. Adding a line there
is the whole change.

The drift it prevents is asymmetric, which is why they share a file rather than
being kept in step by hand: telling the model a package is present when it is not
costs a failed import and a retry, while telling it a package is absent when it is
present costs a pointless install on every project. `requests` was the second case
made real — the model's first act in a data project was `pip install requests`, per
project, on a box that may sit behind a proxy.

**Added:** `requests` (+4 MB — every REST call to our own API needs an HTTP client)
and `tabulate` (+0 MB — `DataFrame.to_markdown()` raises without it, and this agent
presents findings as markdown tables).

⚠️ **Deliberately absent, and the prompt says so** rather than leaving the model to
find out: `pyarrow` (+132 MB, ~45% again on top of the 292 MB baseline, for a
storage-format convenience when CSV works) and `statsmodels` (+55 MB, speculative —
no evidence it was reached for, and `scipy.stats` covers the common tests). Both are
one `pip install` away inside a project venv, which is what that venv is for. The
measured costs are recorded in the file so a future addition can be argued with
numbers rather than taste — the list is inherited by every project and paid for in
image size on a single root volume (DAT-178).

⚠️ **The fallback deliberately UNDERSTATES.** If the file cannot be read, `prompt.py`
names only the original four. Understating makes the model check, which is
recoverable; overstating makes it assume, which is not.

**Existing boxes need no venv rebuild** — project venvs are built with
`--system-site-packages`, so a re-run installer adds the packages to the system
interpreter and every existing project inherits them immediately.

### Fleet memory is seeded, and a wrong rule is harder to write (DAT-209)

Diagnosing a broken environment, an agent concluded *"the SIP container logs are
completely empty, which means the apps never actually launched"* — which is false,
because Rumi services log to files **inside** the container. It acted on that, and
**wrote it into its own memory** as a durable lesson. It self-corrected two turns
later only because a later check happened to contradict it.

We already knew the fact. It was in a human-facing note the model could not read.

Three changes:

- **The bank is seeded** — `fleet_memory/diagnosing-the-environment.md` carries the
  traps: an empty `docker logs` proves nothing, containers "Up" are *machines*
  running `sshd` and can hold no applications at all, the only settling check is a
  real data call, `status`'s verdicts, where the failure reports already are, and
  `journalctl` needing sudo. This is the content DAT-176's empty scaffold existed
  for.
- **The prompt names the hostnames** rather than leaving them to be guessed — the
  model had probed three candidates before finding `local-foundry-dev-api…`. The
  `/etc/hosts` block the CLI writes is the authority; both `foundry` and `trading`
  forms are listed, with the `-api-rest` mis-guess called out by name.
- **The memory protocol distinguishes an observation from a rule.** An observation
  about this user's setup is cheap to record and cheap to be wrong about. A general
  rule about platform behaviour is expensive to be wrong about, because it gets
  applied confidently without re-checking — so it may not be written from a single
  observation or an inference, must say how it was verified, and loses to fleet
  memory on conflict.

**Rule: a platform workaround belongs in fleet memory, not the prompt** (Girish,
2026-08-10). The prompt is paid on every turn; a workaround only matters when the
model is doing the thing it applies to. `platform-gotchas.md` now holds the crypto
symbol form, crypto having no quotes, one-dataset-at-a-time, the tick-fetch OOM, and
the `numDays`/replace/unadjusted fetch semantics. The prompt keeps a short trigger
("before you touch a dataset or plan a fetch, read it") because these fail *silently
or expensively* — an unread warning about an OOM that writes zero data is worse than
a slightly longer prompt.

⚠️ **Re-verify before moving anything, and expect some of it to be wrong.** The
DAT-176 candidate list was written weeks ago and this week's tickets had moved under
it. What the check found:
- **Bare crypto symbols** — refined, not copied. Every crypto data endpoint now
  normalises inbound symbols (DAT-32), so REST tolerates `X:BTCUSD`; the descriptor
  does not, because history builds `"X:" + symbol` with no strip.
- **Crypto trades-only** — true but reframed. DAT-107 did **not** make quotes work
  (the provider has none); it made the API say so with a clear error instead of
  returning empty. Reading the ticket title alone would have deleted a true warning.
- **One dataset at a time** — still true. DAT-101 is *Done* but shipped only the
  prompt workaround; the platform fix is deferred. A Done ticket is not a fixed bug.
- **>1.3 GB tick OOM** — still true, `-Xmx2g` is still hardcoded.
- **"Run the CLI as `datafye`"** — dropped: the agent already runs as `datafye`, so
  it is an operator fact, not a model one.
- ⚠️ **"On DEGRADED, deprovision and rebuild"** — dropped as **actively wrong**.
  DAT-197 made `start` converge, and the prompt teaches converge-first; adding this
  would have reintroduced the rebuild-happy behaviour that destroys deployed data. A
  test now asserts it cannot come back.

⚠️ **The fleet index is paid on EVERY turn**, so the bank stays bounded: a few topic
files rewritten as they accumulate, never one file per lesson. Seeding it moved the
always-on memory block from ~300 to ~740 tokens; a test pins it under 1000. Bodies
are still read on demand — only the index is always-on.

⚠️ **An unseeded bank is a valid state and renders as nothing.** `build_memory_context`
treats an index carrying only its header as empty and omits the whole scope, so the
bank activates only once `MEMORY.md` has at least one `- ` line. A topic file added
without its index line is invisible.

### One oversized tool result must not destroy the turn (DAT-204)

The SDK frames the CLI's NDJSON stdout and refuses any single message larger than
`max_buffer_size`, **default 1 MB**. It raises out of the *read loop*, so it does not
fail the tool call — it ends the whole turn. A user lost a 37-minute analysis to it
because the model read back a chart it had just drawn, which is a reasonable thing to
do and had already caught two real layout bugs.

Three changes, because no one of them is sufficient:

- **`MAX_BUFFER_SIZE` = 16 MB** (`DATAFYE_AGENT_MAX_BUFFER_SIZE`), passed as
  `max_buffer_size`. Far above any legitimate result, still finite — an unbounded cap
  would trade a lost turn for a lost process.
- **`guard_oversized_read`**, a `PreToolUse` hook on `Read`, refuses a file at or above
  **half** the buffer before it is read, and tells the model what to do instead
  (`head -c`, `grep`, summarise in Python, check an image's dimensions rather than
  reading it). Half, because the framer bounds the *encoded* message: JSON escaping
  plus the envelope exceeds the file on disk, and base64 for an image adds a third
  again. ⚠️ **FAIL-OPEN by construction** — it runs before every `Read`, and a guard
  that broke reading would be worse than the bug; anything unexpected allows the read.
- **`_turn_error_message`** translates the overflow for the user. The raw text names
  our transport and reads like data corruption, which sends people to the wrong place.

⚠️ **Raising the buffer is necessary and not sufficient**, which is the point of the
guard. Verified by driving the real transport with the pinned SDK: 2 MB dies at the 1 MB
default, parses at 16 MB, and 20 MB still dies at 16 MB. There is always a payload big
enough.

**A current CLI already guards its own tools** — it refuses huge text files, truncates
bash output and downsizes images — so on 2.1.226 none of the obvious vectors reproduce,
and the shipping harness is newer than that. The guard is the backstop for results
arriving by another route.

⚠️ **The agent's harness is the SDK's BUNDLED CLI, not the one the installer puts on
PATH.** `_find_cli` checks `claude_agent_sdk/_bundled/claude` **first** and only falls
back to `shutil.which("claude")`. So the harness version tracks
`claude-agent-sdk` in `requirements.txt` (`>=0.2.128,<0.3`), and the installer's
`/home/datafye/.local/bin/claude` is not what runs a turn. Anything that depends on
harness behaviour must be verified against the *bundled* binary.

⚠️ **Do NOT read that version off the local `.venv`.** It carries `claude-agent-sdk`
**0.1.51** while production resolves `>=0.2.128,<0.3`, so the two bundle very different
CLIs. This repo shipped "the bundled CLI is 2.1.85" in a ticket, a correction to that
ticket, and this file, all from the local bundle. Measured on a real box, the bundled
CLI is **2.1.228** and production's `sdk_version` is **0.2.136** (the installer's PATH
copy happened to be the same CLI version, which is exactly the sort of coincidence that
lets a wrong mental model survive). **A version derived from a pin RANGE is a guess
about what resolution will do, not a fact**; only a provisioned box answers it.

### `harness.py`: the box now says which CLI runs its turns (DAT-215)

`GET /v1/bom` carries a `harness` block: `cli_version`, `cli_version_error`,
`cli_path`, `cli_source` (`bundled`|`path`), `sdk_version`, `path_cli_version`. The
wrong number above survived three writings because **a fact nobody can read without SSH
is one people will guess about instead**; `sdk_version` alone now shows at a glance that
you are looking at a dev venv rather than production.

- **On `/v1/bom`, not `/health`.** This is a dependency fact, not a liveness one, and
  `/health` is polled every minute by the upgrade cron, by accounts for dormancy and by
  the SPA. The probe is a subprocess, computed **once per process**, not merely for
  speed: `/v1/bom` is unauthenticated, and an endpoint that forks a 300 MB binary on
  demand is a free denial of service. The answer cannot change without a restart anyway.
- ⚠️ **Report the REASON, never a bare `null`.** The first cut returned `cli_version:
  null` on a real box with no way to tell a timeout from a non-zero exit from a missing
  binary, *the exact "a fact nobody can read" problem this module exists to fix,
  reproduced one level down.* `cli_version_error` now carries `timed out after Ns` /
  `exit N: <stderr>` / the exception, and is logged at WARNING when set. **An unknown
  that cannot say why is barely better than no field.**
- ⚠️ **60s timeout, not 10.** The bundled CLI is a ~308 MB Bun binary whose FIRST exec
  has to fault the whole thing in from a cold EBS volume; a warm re-run is about a
  second. **That is what makes it easy to under-size: every measurement you take by hand
  is warm, because taking it is what warms it.** Nothing waits on this probe, so a
  generous bound costs nothing.
- ⚠️ **Cache success ONLY.** Caching a failure pins the wrong answer for the life of the
  process, and this probe is most likely to fail on its *first* call (cold binary, cold
  page cache, a box still settling after boot). The first cut cached unconditionally and
  did exactly that.
- **The bundled path is derived from the installed package** the same way the SDK derives
  it, so it follows the venv and cannot drift. **Mirrored rather than imported**:
  `_find_bundled_cli` is a method on `SubprocessCLITransport` under `_internal`, and
  standing up a transport to ask it a question is a heavier and more fragile coupling
  than eight lines saying the same thing.
- **What the installer's PATH CLI is for:** nothing in the agent invokes it. It exists
  only as the SDK's fallback for a future version that ships without a bundle: cheap
  insurance, kept deliberately, with its **~300 MB of duplicate** noted for DAT-178 so
  the trade-off is a decision rather than a discovery.
- **The pin is now unblocked and NOT yet done.** `requirements.txt` still allows
  `>=0.2.128,<0.3`; the exact pin to write is `claude-agent-sdk==0.2.136`, the version
  production actually resolved. Writing a number before it was measurable would have
  repeated the mistake the block exists to prevent.

### Long environment commands run in the foreground (DAT-203)

`main.py` raises the harness's `BASH_MAX_TIMEOUT_MS` to **30 minutes**. That single
env var is the whole fix, and what it prevents is worth stating precisely.

**The harness does not kill a command that outlives its timeout — it BACKGROUNDS
it.** The tool returns `Command did not complete within its Ns timeout and was moved
to the background`, hands back a task id and an output file, and the turn continues.
To a model that message is nearly indistinguishable from completion, and the agent
has no `BashOutput`, no `KillShell` and no `Task` tool with which to await the thing.
On u1 that is exactly what happened: a `start` was backgrounded at the 600s default,
looked finished, and an `apply` was fired on top of it. Both failed and the
environment was destroyed.

**⚠️ `prompt.py` forbidding background execution (DAT-185) could never have fixed
this.** That rule governs the *model*; the *harness* backgrounds on its own, whatever
the prompt says. Raising the ceiling removes the harness's REASON to background
rather than leaving a prohibition the surface ignores — the fourth instance of
*never offer, or forbid, a capability the surface does not control*, after
`AskUserQuestion`, the `Task` family, and backgrounding itself.

- **Only the MAX moves.** `BASH_DEFAULT_TIMEOUT_MS` stays at the harness default of
  2 minutes, because it applies to *every* command — an ordinary one that hangs
  would otherwise block a turn for half an hour. The prompt tells the model to pass
  an explicit generous timeout; the raised ceiling is what makes that request
  honored instead of silently clamped.
- **A request above the ceiling is clamped, not refused** — which is why the old
  600s cap was invisible: asking for 900s got you 600s and no error.
- **Verified against a real CLI**, both directions: with the cap at 15s a 40s command
  requested at `timeout: 600000` backgrounded at 15s; with the cap at 30 minutes the
  same command finished in the foreground; and a 660s command — past the old 600s cap —
  ran to completion in the foreground.
- **Verified on PATH CLI 2.1.226; the shipping bundled CLI is 2.1.228** — two patch
  releases apart, measured on RC 2.0.37. This was recorded for a day as "unverified,
  the bundled build is 2.1.85 and these env vars may be inert on it"; that version was
  read off the local `.venv` and was wrong (see the warning under DAT-204 above). The
  gap is now narrow enough not to be a material risk, though a direct test on a box is
  still the only thing that closes it outright.
- ⚠️ **`sleep` cannot be used to test this.** The harness blocks foreground `sleep`
  outright, so the command never reaches the shell and the result says nothing about
  the timeout ceiling. Use a long command that is not the `sleep` program (a Python
  `time.sleep`, or better, a real environment operation — which is what the ceiling
  exists for). The block is also a live example of the harness advertising tools this
  agent does not have; see *The harness recommends what the prompt forbids* below.
- **The prompt covers the residual case.** If a command is backgrounded anyway, the
  model is told to treat it as still running, to never start a second environment
  command, and to establish whether it finished from the DAT-183 marker
  (`~/.datafye/run/cli-*.json`) rather than from an output file — an empty file
  reads exactly like a job that never started.
- **Dormancy cannot cut it off**: a command in flight is reported warm through the
  same marker (`warmth.py`), so the box stays up for the full 30 minutes.

### The harness recommends what the prompt forbids (DAT-218)

Trying to test DAT-203 on a live box with `sleep 700` produced this, from the tool
layer rather than the shell:

> Blocked: `sleep 700` followed by: `echo ...`. To wait for a condition, use **Monitor**
> with an until-loop. To wait for a command you started, use **`run_in_background: true`**.

Both suggestions are unavailable here. There is no `Monitor` in `INTERNAL_TOOLS`, and
backgrounding is banned outright (DAT-185) because with no `BashOutput` the model can
neither read nor kill what it started. **The harness is recommending the exact technique
that orphaned a provision on u1.**

⚠️ **The DAT-185 ban has a hole: it enumerates SHELL constructs.** `prompt.py` names
`&`, `nohup`, `setsid`, `disown` and "no detached wrapper of any kind" — all things you
type into a command line. `run_in_background` is a **Bash tool parameter**, not a shell
construct, and is never named. A model following an explicit instruction from its own
tool layer would not obviously be breaking the letter of that rule.

It held anyway: the model refused, said the block came from its tool layer rather than
the box, and did not chain shorter sleeps. That is the wording working under direct
pressure from the harness — which is evidence the rule is well written, not evidence
the hole is safe.

**This is the fifth instance of the same class**, after `AskUserQuestion`, the `Task`
family, backgrounding itself, and `BASH_MAX_TIMEOUT_MS`: *never offer, forbid, or rely
on a capability the surface does not control.* The new wrinkle is that the conflicting
advice now arrives in a harness **error message**, which no amount of tool-list curation
reaches, so the prompt has to answer it directly, and now does:

- The ban **names `run_in_background`** as a Bash tool *parameter*, saying plainly that
  it is not a shell trick but detaches just the same, and that it is the form the model
  is most likely to reach for **because its tool layer suggests it**.
- **`Monitor` joins the absent-tools list** alongside `BashOutput`, `KillShell` and
  `Task`, so the "you cannot await or kill one" reason names every tool the harness
  might offer.
- A standing rule: **WHEN YOUR TOOL LAYER SUGGESTS SOMETHING THIS PROMPT FORBIDS, THIS
  PROMPT WINS.** The block is real; the suggested workaround is not available. The model
  is told to report what was blocked rather than route around it, with foreground
  `sleep` named as the known case.
- Pinned in `tests/test_prompt_audit.py`, because a rule that answers a message from
  outside the repo has nothing else watching it.

### Foundry reconciliation at boot (DAT-199)

Every box, in every installer mode, reconciles its foundry on **every boot** through
one systemd one-shot: `datafye-foundry-boot.service`, running
`install/foundry-boot.sh`. This is what makes the "your sandbox already has one"
claim in `prompt.py` and the *pre-provisioned empty foundry* line above actually
true.

**Why one unit and not two.** It replaces the hosted-only first-boot provisioner
(`datafye-foundry-firstboot.service` / `first-boot-foundry.sh`, DAT-170) and
supersedes the separate wake-restore hook DAT-124 proposed. Merging them is the
whole point: **two boot-time actors mutating one foundry is the u1 incident**, where
a first-boot provision, an agent `start` three minutes into it, and an `apply` on
top of both destroyed the environment twice. Shipping a second boot-time actor would
have moved that from every fresh boot to every wake. The hosted-only scope was wrong
independently — a self-provisioned user stops and starts their own box and hits the
identical app-less wake, and DAT-124's proposed `~rumi/boot.sh` is the Rumi Worker
AMI's extension point, which a DIY box may not have either.

The **name changed with the job**. "first-boot" described one of the two things it
does, and a unit whose name disagrees with its behaviour is exactly the drift that
produced DAT-170 in the first place (an installer comment promising a first-boot step
that did not exist).

**What it reads, and what it does not write.** Readiness is *derived* from three
inputs and stored as no single fact:

| Input | Source |
|---|---|
| Intent | the local cache of the last intent accounts pushed (`~datafye/.datafye/run/foundry-intent.json`). **Absent means running** — no deviation has ever been recorded, and a sandbox exists to host a foundry |
| Observed | interrogated on demand: are the applications **answering** |
| In flight | the DAT-196 operation lock, plus the DAT-183 markers that report it |

It **writes no state file**. An earlier cut had every lifecycle command record the
environment's desired state; that shipped and was **reverted** (`datafye-deploy`
PR #11). The bug: a human SSHes in to debug and runs `foundry local stop`, the engine
records `intended=stopped`, and this unit then leaves the foundry down on every
subsequent boot — a debugging action promoted into standing policy by a component
with no way to tell the two apart. Intent is formed in **accounts**, where the user's
request actually arrives, and is pushed here; the box holds a replica, never the
record. (The push itself is DAT-198 and does not exist yet. The unit is correct
without it, which is the design: absent cache means running.)

**The reconcile, and the two rules that keep it safe:**

```
another operation owns the environment  -> exit cleanly, do nothing
intent = stopped                        -> do nothing, whatever the observation
no containers                           -> provision, and leave it RUNNING
containers present                      -> converge via the idempotent start
```

- **Reconcile additively only.** Intent `stopped` with a fully serving foundry means
  somebody started it by hand; never tear down live work to satisfy a record.
- **Never act while something else owns the environment**, and exit **0** rather than
  wait or force. A boot unit parked behind a 17-minute provision is indistinguishable
  from a hung boot, and a red unit in `systemctl status` is a signal worth reserving
  for the boots where the foundry really is broken.

**⚠️ It leaves a fresh box RUNNING**, reversing DAT-170's provision-then-stop. Under
the readiness model a fresh box must end up matching intent, and intent is running —
otherwise it is permanently unready and `prompt.py`'s claim stays false on exactly
the boxes that just built one. Both of DAT-170's original reasons survive elsewhere:
the uniform postcondition is now this unit's job (it converges on *every* boot, not
just the first), and staying out of the app-less wake state is **DAT-125**'s (stop
the apps cleanly *before* the box stops).

Details that are load-bearing:

- **⚠️ The script cannot take the DAT-196 lock, and must not pretend to.** That lock
  is a `FileChannel.tryLock()`, which maps to **fcntl(2)** POSIX record locks, while
  `flock(1)` uses **flock(2)** — on Linux those are *independent lock namespaces*, so
  a shell `flock` on the same file succeeds against a held Java lock and gives mutual
  exclusion that silently is not. Exclusion is enforced where it always was: every
  operation the script performs goes through the CLI, which takes the real lock for
  its duration. The script's own check is the courtesy half — it avoids starting a
  command that would only be refused, and names the holder in the journal. The window
  between checking and acting is closed by the lock, not by the check.
- **The in-flight check reads the DAT-183 marker, not the lock file.** The lock file
  is deliberately never deleted on release (unlinking it would hand two processes
  different inodes), so its contents describe the *last* holder, not necessarily a
  current one. The marker's contract is the stronger one and is exactly this
  question: present **AND** the process alive. A boot is also when **PID recycling**
  bites — a marker left by a CLI killed before the last shutdown can name a PID this
  boot has since reassigned — so `kill -0` is confirmed against
  `/proc/<pid>/cmdline`.
- **It does not skip the converge when `status` says HEALTHY.** That verdict keys on
  the deployment API answering, and an environment with one dead service still
  reports HEALTHY (**DAT-200**, reproduced live). Skipping on it would make the unit
  blind to exactly the partial state it exists to repair. The serving decision belongs
  to DAT-197's per-service prober, in one place, not duplicated in shell — which is
  also why the only branch the script makes for itself is *does an environment exist
  at all*.
- **`start` is the converge primitive** (DAT-197): it probes each service for an
  answer and launches only the dead ones, so one call covers a cleanly stopped box, an
  app-less wake, and a partially serving one, and is a fast no-op on a healthy one.
- **It announces by being an ordinary CLI caller.** `provision` and `start` each write
  their own DAT-183 marker for their duration, so a boot-time provision is visible as
  in-flight work rather than the box looking idle for seventeen minutes. Nothing here
  changes when DAT-184 starts consuming that marker as the warm signal.
- **It keys on real state, not a sentinel.** A done-marker written before the work
  succeeded would lock a box out of ever retrying after a failed provision — exactly
  the state this exists to stop shipping. `RemainAfterExit` is deliberately unset for
  the same reason.
- **It runs the CLI as `datafye`, always.** `rumi` is in `wheel` but NOT in `docker`,
  so as any other user the CLI cannot reach the Docker socket; before DAT-172 that
  came back as "not provisioned" — a false negative indistinguishable from an empty
  box, which sends you on to provision on top of a live environment. DAT-172 now
  classes it as a permission failure, which is a better error but still not an answer,
  so the script treats an indeterminate `Provisioned:` as **stop and say why**.
- **⚠️ The installer must RETIRE the old unit, not just stop writing it.** An upgraded
  box already has `datafye-foundry-firstboot.service` enabled and pointing at a copy
  of the old script still on disk. Leaving it would put two boot-time actors on one
  foundry — the incident, reintroduced by the change meant to prevent it. The
  installer disables it, deletes the unit file *and* the old script; `disable` alone
  is not enough, since a later `systemctl enable` by hand would resurrect it.
- **It rides the companion-refresh loop** in `install_template.sh` alongside
  `upgrade-check.sh`, because it is executed from `INSTALL_DIR` by a systemd unit.
  Without that, a box would stay pinned to whatever version its AMI baked and a fix
  could only reach the fleet via a re-bake (the DAT-131 lesson).

Failure is non-fatal: the agent is useful without a foundry (chat, docs, code and
memory all work), so the unit logs loudly to the journal, leaves the failure report
under `~/.datafye/logs` in place, and retries next boot rather than leaving the agent
service down. It never auto-rebuilds — the broken environment is the only evidence of
why it failed.

### `--version` selects what to install; `--pin` disables upgrades (DAT-187)

They used to be the same flag, and that silently disabled auto-upgrade **across the
entire hosted fleet**. The Packer bake passes `--version` (for reproducibility, which
is right), `--version` set `VERSION_EXPLICIT=true`, that was written to `agent.env` as
`DATAFYE_AGENT_PINNED=true`, and `upgrade-check.sh` stands down when pinned. Every
step was individually reasonable; composed, no hosted box had ever auto-upgraded.

Now `--version` means only "install exactly this version" and a separate `--pin` means
"never auto-upgrade". The bake passes `--version` alone, so baked boxes upgrade
normally.

**It hid because two different silences looked identical.** The cron fired every
minute and wrote nothing, which was equally consistent with *pinned and standing down*
and with *checked, already current* — and on the box where it was found, the installed
and published versions happened to match. `upgrade-check.sh` now logs **on state
change only** (a small `.upgrade-check-state` file next to the version): one line when
it becomes pinned, one when it starts tracking a version, nothing on the thousands of
identical no-ops in between. Bounded volume, durable trace.

⚠️ **The fix cannot reach an already-pinned box by upgrading**, since that box will
not upgrade — that is the bug. Existing sandboxes need a reprovision (or a manual edit
of `DATAFYE_AGENT_PINNED` in `agent.env`).

### Companion scripts: always refresh them, and never `cp` onto a running one (`ddc5c01`, DAT-131)

`install.sh` copies `upgrade-check.sh` (and itself) into `INSTALL_DIR` on every install. Two rules the old code broke, both of which only bite on the **auto-upgrade** path, where `install.sh` arrives as `curl … | bash` and is invoked **by** the still-executing `upgrade-check.sh`:

- **ALWAYS refresh.** Under `curl | bash`, `BASH_SOURCE[0]` is **unset**, so `SCRIPT_DIR` silently resolved to cron's working directory. The old `if [ -f "${SCRIPT_DIR}/upgrade-check.sh" ] … elif [ ! -f "${INSTALL_DIR}/upgrade-check.sh" ]` then matched **neither** branch (nothing to copy from, and the installed file already existed), so `upgrade-check.sh` was **never replaced** — a box stayed pinned to whatever was baked into its AMI, and a fix to that script could only reach the fleet via a re-bake, never auto-upgrade (this included the DAT-127 Phase 4a idle gate, which lives in it). Now a loop always refreshes, sourcing `${AGENT_CODE_DIR}/install/<f>` (the version-matched copy in the cloned agent tree) when `SCRIPT_DIR` is unusable; the download survives only as a last resort for a first install whose tree has no `install/` dir. `SCRIPT_DIR` is now left **EMPTY** rather than wrong when `BASH_SOURCE[0]` is unset, so a stray `upgrade-check.sh` in cron's cwd cannot hijack the copy either.
- **ATOMIC replace.** A plain `cp` truncates and rewrites the **same inode**, and bash reads a script lazily **by byte offset** — so the running shell's next read lands mid-line in the new file and it dies with a bogus `syntax error near unexpected token` **after** a fully successful upgrade. (Observed live on the Sutra agent; datafye was masked from it only by the bug above, so fixing that alone would have exposed this.) Companion scripts are now installed by `cp` to `.<f>.new` + `chmod +x` + `mv -f`, so a running shell keeps its original (now unlinked) inode; and `upgrade-check.sh`'s tail is a **brace group ending in `exit 0`** so bash parses the upgrade + log + exit as one compound command and never reads the file again.

Cross-agent: identical fix in nvx-sutra (`50d6642`) + the Rumi Support agent (`8023341`).

### AMI Build

```bash
# Hosted AMI (install + cleanup for snapshot)
sudo ./install.sh --mode hosted --ami-cleanup

# Standalone AMI (copy first-boot.sh, create systemd one-shot)
# See first-boot.sh for details
```

### Installed Layout

```
/opt/datafye/agent/
├── app/             # Agent source (cloned from GitHub)
├── venv/            # Python virtual environment
├── agent.env        # Configuration (credentials, mode, paths)
├── version          # Installed version
├── install.sh       # Installer (for upgrades)
└── upgrade-check.sh # Auto-upgrade script
/opt/datafye/docs/       # Datafye docs (cloned from GitHub)
/opt/datafye/samples/    # Datafye samples (cloned from GitHub)
/usr/local/opt/datafye/cli/<version>/  # Datafye CLI
/home/datafye/workspace/ # User workspace
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check — `bootstrapped`, `anthropic_key_status`, credential status, idle signals, and `foundry` (derived environment readiness, see below — including **`env_type`**: `foundry`/`trading`/`null`, DAT-217). Always available, including before bootstrap |
| `/v1/bom` | GET | Dependency bill-of-materials — the single Datafye version this agent is built against (platform/samples/CLI/docs share one version). Reads `bom.json`; also carries the **`harness`** block from `harness.py` (which Claude Code CLI actually runs turns, DAT-215). Unauthenticated like `/health`; rendered on the Yukti agent surface |
| `/bootstrap` | POST | Accounts-only. Bootstrap the agent's identity + credentials-store key from an accounts-signed JWT (`Authorization: Bearer`, `purpose=agent-bootstrap`). Idempotent for the same user; 409 on rebind |
| `/v1/chat` | POST | SSE streaming chat with agent. JWT-protected; 503 if no Anthropic key, 502 if invalid |
| `/v1/credentials` | POST | REMOVED — returns 410 Gone; credential writes go through the accounts service |
| `/v1/credentials/update` | POST | Accounts-only. Push a single credential `{provider, value}` into the encrypted store; 204 |
| `/v1/activity` | POST | Presence heartbeat from the SPA (DAT-169). Advances `last_chat_activity_at` without running a chat turn, so a user who is *reading* still counts as present. JWT-protected, self-scoped, one assignment and no I/O. 204 |
| `/v1/foundry/intent` | POST | Accounts-only, `agent-lifecycle`-token-gated. Record the foundry intent accounts has decided (`{intended, source}`); written to the on-disk replica the boot service reads. Errors are reported rather than swallowed — a push that did not land must not look like one that did |
| `/v1/foundry/stop` | POST | Accounts-only, `agent-lifecycle`-token-gated. Bring the foundry down cleanly before the box is powered off (DAT-125). Returns `{status, detail}` where status is `stopped`/`absent`/`busy`/`failed` — `busy` tells accounts to ABORT the instance stop |
| `/v1/credentials/status` | GET | Check which credentials are configured (JWT-protected) |
| `/v1/broker/brokers` | GET | List brokers Datafye supports (StocksBroker enum) |
| `/v1/broker/connections` | GET | List the user's brokerage connections with linked accounts |
| `/v1/broker/connections` | POST | Create a ConnectTrade OAuth URL for a chosen broker; body `{type, broker}` |
| `/v1/skills` | GET | List skills available to the agent across all tiers: `system` (predefined, read-only), `user-global` (agent-authored, reusable), `user-project` (per-project; pass `?conversation_id=`). JWT-protected. Execution is chat-driven ("use the `<name>` skill"), no separate run endpoint |
| `/v1/broker/connections/{id}` | DELETE | Revoke a brokerage connection |
| `/v1/conversations` | GET | List conversations (projects), most-recently-updated first. **LEGACY/UNUSED** — accounts is the authoritative project registry; the frontend lists from accounts |
| `/v1/conversations` | POST | Create a conversation (agent mints the id, deduces a name). **LEGACY/UNUSED** — accounts mints project ids; new chat threads arrive with an accounts-minted `conversation_id` that `/v1/chat` materialises via `conversations.ensure()` |
| `/v1/conversations/{id}` | PATCH | Rename a conversation; 404 if absent |
| `/v1/conversations/{id}` | DELETE | Permanently delete a project's agent-side folder via `conversations.delete()` (path-safety guard refuses anything outside the projects base); 204 on success, 404 if the agent never materialised it. Accounts deletes its own project record separately |
| `/v1/conversations/{id}/history` | GET | Replay a conversation's `messages` and `commentary` audit trail; also returns the project's `intent` + `track` (+ `stage`/`maxStage`) so the frontend can rehydrate the right stepper. Each assistant message carries a per-turn `usage` (tokens+cost) tagged by `conversations.set_last_message_usage`, for the accounts Conversation view |
| `/v1/conversations/{id}/outputs` | GET | List the project's downloadable deliverables — files the agent wrote to the project's `outputs/` folder (distinct from `uploads/`), as `{name, type, size, modified_at}`. JWT-protected |
| `/v1/conversations/{id}/outputs/{filename}` | GET | Download one deliverable as a `FileResponse` from `outputs/`. Path-safety-guarded (refuses anything resolving outside `outputs/`); JWT-gated; 404 if absent |

Every endpoint except `/health` and `/bootstrap` is gated by the
`require_bootstrapped` dependency and returns 503 until the accounts bootstrap
push lands.

## SSE Event Types

| Event | Description |
|-------|-------------|
| `init` | Session initialized |
| `title` | Summary-generated project title (`{conversation_id, name}`). Emitted once on the first turn of a new conversation after `generate_title()` summarizes the first message and renames the project; Yukti adopts it over the provisional `deduce_name` |
| `content` | Text content chunk |
| `thinking` | Agent reasoning (`{text}`). Requested explicitly via `thinking={"type":"adaptive","display":"summarized"}` — on Opus 5 `display` defaults to `omitted`, so blocks arrive empty while still being billed. **ASCII-folded** (`_ascii_fold`) before it is emitted or persisted: it is the API summarizer's prose, which no prompt rule reaches, and accounts stores the trail ASCII-encoded. Also **persisted** as a `thinking`-kind commentary entry, so it replays from `/history` instead of being live-only |
| `tool_use_start` | Tool invocation started |
| `tool_result` | Tool execution result. Carries `result_tokens` — the weight of the **full** result, measured before `_DETAIL_OUTPUT_CAP` truncates it for display, because the whole result is what lands in the prompt and is re-read on every remaining round of the turn (the client only ever sees the capped text, so it cannot recompute this) |
| `commentary` | A line for the workspace's per-turn **activity rail** (`{text, kind}`). Machine tool-labels (notable Bash + MCP calls) carry `kind` `muted`/`notable`/`check`/`error`; the agent's own **work-narration** (a text burst followed by a tool call) carries `kind` **`narration`** so the frontend renders it a shade brighter than the tool-labels. A `kind` of **`step`** is the per-round cost badge (empty text; carries `usage` = `{new, carried}`), and **`thinking`** is the model's reasoning. Every entry carries the **`step`** it belongs to; a tool-label entry also carries `call_tokens` (what the CALL put into the prompt — for `Write`/`Edit` the model generates the whole file into the call, so a result-only figure reported ~nothing for the most expensive thing in the step). Also appended to the conversation's commentary audit trail (**uncapped** — commentary is the analytics record accounts persists) |
| `ticker` | The conversation's **context size** for the live status ticker (`{tokens}`). Emitted once per model round as `new + carried` — the whole prompt at that step, so it is exact and needs no summing. The field name is kept for older clients but its **meaning changed**: it used to be a running `input+output` tally, which reads in the tens once the prefix is cached. Gated on a round actually being new, because the SDK repeats one round's usage across every message of that round (the old per-message accumulation was double-counting) |
| `result` | Final result with metadata |
| `stage` | Intent-aware lifecycle position the turn landed in, classified post-stream by `classify_lifecycle()` (cheap haiku). `{conversation_id, intent, track, stage, maxStage}` where `track` is the ordered stage list for the project's `intent` and `stage` is the current step within it. Drives the workspace stepper (frontend renders whatever `track` it's given). See **Project lifecycle** below |
| `usage` | Per-`(stage × model)` token/cost/tool usage, emitted at turn-end after attribution: `{conversation_id, usage, stage, model}` where `usage` = cumulative `{totals, by_stage_model, updated_at}`. Sourced from `ResultMessage.model_usage` (one delta per model the turn actually used, idempotency-keyed per model — replaces the flat single-`usage` read that undercounted multi-step turns), plus the Haiku sidecar tokens folded in via `usage_sink` (`generate_title`, `classify_lifecycle`, `analyze_satisfaction`). Falls back to the flat `usage` if the CLI emits no `model_usage`. When the project has no lifecycle stage (a `research`/`chat` project has an empty track, so its stage is blank), usage is tagged with the project **intent** (e.g. `research`) rather than a blank stage that would render as "unknown" (`stage_now = rec.stage or rec.intent or 'general'`). Drives the telemetry footer + stepper badges; also reported to accounts (`POST …/projects/{id}/usage`, JWT-forwarded, idempotency-keyed) for billing + the hosted-tier quota meter |
| `descriptor` | Raw deployment-descriptor YAML text (`{descriptor}`), relayed by the frontend to accounts. Best-effort read of the deployed environment's deployment REST API after a chat turn |
| `env_status` | Environment state, derived from the descriptor: `{status, env_type, datasets, symbols, broker, mode}`. The environment-type field is keyed **`env_type`** (NOT `type`) so it can't collide with the SSE frame's own `type` discriminator that `sse_event` sets. When the post-turn deployment read returns **None** (env torn down or the user switched datasets), the agent emits a **CLEARED** `env_status` (`status:'idle'`, `env_type:None`, empty lists) instead of nothing, so the SPA panel doesn't keep showing a stale environment (e.g. SIP after moving to a Crypto foundry) |
| `artifact` | A deliverable the agent wrote to the project's `outputs/` folder this turn (`{conversation_id, name, type, size}`). Emitted post-stream by diffing the `outputs/` snapshot taken before the turn against the snapshot after — one event per new or changed file — so the frontend can offer a download. Best-effort; never breaks the turn |
| `scorecard_update` | Test results (for frontend) |
| `chart_data` | Chart data push (for frontend) |
| `error` | Error occurred |
| `done` | Stream complete |

## Project lifecycle (intent-aware tracks)

The lifecycle is **agent-driven and per-project**, not one fixed global pipeline.
`classify_lifecycle()` (cheap haiku, post-stream) infers the project's **intent**
and returns `{intent, track, stage}`; the agent owns each project's lifecycle and
reports it, while the frontend renders whatever track it is handed.

- **Tracks** (`conversations.py`): per-intent ordered stage lists.
  - `algo` / `signal` → `[Explore, Design, Build, Backtest, Validate, Deploy]`
  - `dashboard` / `app` / `tool` → `[Explore, Design, Build, Ship]`
  - `chat` / `research` → `[]` (no stepper)
- The first stage was renamed **Idea → Explore**. The intent vocabulary is open —
  the agent can compose a track for a novel build intent (common
  `Explore→Design→Build` spine + an artifact-dependent tail). For `signal`,
  "Deploy" means *publish* (vs an algo's deploy).
- Project records carry `intent` + `track` alongside `stage`/`maxStage`; helpers
  `set_intent_track` / `track_for_intent` manage them. `STAGES` remains as a
  back-compat alias for the trading (`algo`) track.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DATAFYE_AGENT_ANTHROPIC_API_KEY` | - | Local-dev seed only. The Anthropic key is a *credential* — in production it lives in the encrypted credentials store and is delivered by accounts via `/v1/credentials/update`. This env var only seeds the store the first time it is created |
| `DATAFYE_AGENT_MODEL` | `opus` | Claude model |
| `DATAFYE_AGENT_LOG_USAGE` | - | Set to `1` to dump the raw per-round usage object (and the usage-bearing stream events) from the SDK. **Off by default** — it is one line per model round, hundreds per build turn. Logged at INFO deliberately: the service runs at INFO, so a debug-level line would be silently swallowed and the diagnostic would look broken rather than disabled |
| `DATAFYE_AGENT_TITLE_MODEL` | `claude-haiku-4-5` | Cheap model used only by `generate_title()` to summarize a new project's first message into a title (direct Anthropic `/v1/messages` httpx call, never the main reasoning model) |
| `DATAFYE_AGENT_PORT` | `18780` | HTTP port |
| `DATAFYE_AGENT_WORKSPACE` | `/home/datafye/workspace` | User workspace directory |
| `DATAFYE_AGENT_DOCS_DIR` | `/home/datafye/docs` | Path to Datafye docs |
| `DATAFYE_AGENT_CLI_PATH` | `datafye` | Path to Datafye CLI |
| `DATAFYE_AGENT_SAMPLES_DIR` | `/home/datafye/samples` | Path to datafye-samples (API reference) |
| `DATAFYE_AGENT_ALLOWED_ORIGINS` | `*` | CORS origins |
| `DATAFYE_AGENT_MASSIVE_API_KEY` | - | Massive (Polygon) API key |
| `DATAFYE_AGENT_PALPHA_API_KEY` | - | Precision Alpha API key |
| `DATAFYE_AGENT_HWAI_API_KEY` | - | HWAI API key |
| `DATAFYE_AGENT_CONNECTTRADE_CLIENT_ID` | - | ConnectTrade client ID — **local-dev seed only**; in production accounts pushes it as the `connecttrade_client_id` credential (see Broker-Credential Foundation) |
| `DATAFYE_AGENT_CONNECTTRADE_CLIENT_SECRET` | - | ConnectTrade client secret — local-dev seed only; pushed by accounts as `connecttrade_client_secret` in production |
| `DATAFYE_AGENT_CONNECTTRADE_USER_ID` | - | ConnectTrade user ID — minted by the agent on first broker-link and written back to accounts; this env var only seeds local dev |
| `DATAFYE_AGENT_CONNECTTRADE_USER_SECRET` | - | ConnectTrade user secret — minted + written back like the user id |
| `DATAFYE_AGENT_GITHUB_USER` | - | Personal GitHub username (optional) |
| `DATAFYE_AGENT_GITHUB_TOKEN` | - | Personal GitHub token (optional) |
| `DATAFYE_AGENT_GITHUB_ORG` | `datafye` | GitHub org for algo repos |
| `DATAFYE_AGENT_MCP_SERVERS_ADDITIONAL` | `[]` | Additional MCP servers (JSON) |
| `DATAFYE_AGENT_CONNECTTRADE_API_URL` | `https://api.connecttrade.com` | ConnectTrade REST base URL |
| `DATAFYE_AGENT_BROKER_REDIRECT_URL` | `https://yukti.datafye.ai/broker-callback.html` | OAuth redirect target — the static page that tells the parent window OAuth finished. ⚠️ **This defaulted to `https://developer.datafye.io/broker-callback.html` until DAT-224, and that domain does not resolve at all.** The installer never sets this var, so every box in the fleet used the dead value and brokerage linking (the first step of the entire trading path) was broken everywhere. See *A default pointing at a domain we no longer own* below |
| `DATAFYE_AGENT_BROKER_STATE_FILE` | `~/.datafye/agent/broker_user.json` | Where the ConnectTrade user_id / user_secret are persisted (TODO: migrate to accounts-manager) |
| `DATAFYE_AGENT_DEPLOYMENT_API_URL` | `http://local-foundry-dev-api.datafye.local:7776` | Datafye deployment REST API base URL — read after a chat turn to derive `descriptor` / `env_status` from the deployment descriptor (`GET .../deployment/{descriptor,datasets,symbols}`) |
| `DATAFYE_AGENT_STATE_DIR` | `~/.datafye/agent` | Single root for ALL per-user writable state (credentials, projects, user-skill plugin, user memory). Relocate everything with one var — used by local tests to avoid polluting `~/.datafye`. Each narrower var below still overrides when set |
| `DATAFYE_AGENT_PROJECTS_DIR` | `<state>/projects` | Base dir holding one FOLDER per project. ⚠️ No fallback to the older `DATAFYE_AGENT_STRATEGIES_DIR` / `DATAFYE_AGENT_CONVERSATIONS_DIR`, and no migration — see *Projects, not strategies* |
| `DATAFYE_AGENT_SYSTEM_PLUGIN_DIR` | `<app>/plugins/datafye` | Read-only system-skill plugin (ships with the app clone) |
| `DATAFYE_AGENT_USER_PLUGIN_DIR` | `<state>/plugins/user` | Writable user-global skill plugin (agent authors skills here) |
| `CLAUDE_CODE_DISABLE_AUTO_MEMORY` | `1` (set by the agent) | Disables the `claude` CLI's own auto-memory so the agent runs ONE explicit memory model (see Key Design Decisions). Not a `DATAFYE_` var; `os.environ.setdefault` in main.py, overridable |
| `DATAFYE_AGENT_WARM_DATA_WINDOW` | `600` | How recent a service's activity must be for the environment to count as working. A third of the 30-minute idle threshold, so it can never dominate the dormancy decision. **0 or less switches environment-based warmth off entirely** — handled agent-side, because the platform reads `activeWithinSeconds == 0` as "use my default of 300", so forwarding a zero would give a *shorter* window rather than none. The hook for when environments move to the cloud and a local foundry should stop blocking dormancy; the in-flight signal is unaffected |
| `DATAFYE_AGENT_WARM_REFRESH_INTERVAL` | `60` | Seconds between warm-signal probes. Cached so `/health` never waits on the deployment |
| `DATAFYE_AGENT_WARM_PROBE_TIMEOUT` | `15` | Bound on the aggregate activity call |
| `DATAFYE_AGENT_FOUNDRY_OBSERVE_INTERVAL` | `20` | Seconds between background foundry observations. `/health` serves the latest snapshot rather than probing on the request, so **this is the binding constraint on freshness, not the reader's poll interval** — a UI polling every 15s against a 60s snapshot re-reads one value four times (was 60, lowered for DAT-234). Safe to lower because `observe_forever` sleeps AFTER each pass, so passes cannot stack. ⚠️ The **in-flight** half is exempt: it is read live from the DAT-183 marker on every `/health` call, so "an operation is running" is never stale |
| `DATAFYE_AGENT_FOUNDRY_PING_TIMEOUT` | `40` | Bound on the per-dataset health ping. Deliberately ABOVE the deployment API's own 30s reply timeout: a dead service makes the API wait that long, and bounding below it would turn every partial environment into "not answering at all" |
| `DATAFYE_AGENT_RUN_DIR` | `~/.datafye/run` | The CLI's run directory, where the DAT-196 lock and the DAT-183 in-progress markers live. Read (never written) to tell whether an environment operation is in flight. Not under the agent state root — it belongs to the environment, which outlives this process |
| `DATAFYE_AGENT_FOUNDRY_STOP_TIMEOUT` | `240` | Upper bound (seconds) on the graceful environment stop. Deliberately BELOW accounts' own 300s read timeout so the agent is what expires and can answer with which outcome happened |
| `DATAFYE_AGENT_JWT_LEEWAY_SECONDS` | `60` | Clock-skew tolerance applied to time-based JWT claims (iat/nbf/exp) when verifying accounts-signed tokens — avoids "token not yet valid (iat)" failures from clock drift |
| `DATAFYE_UPGRADE_INACTIVITY_WINDOW` | `120` | Auto-upgrade idle gate (seconds). `upgrade-check.sh` proceeds only when `now - last_chat_activity_at >= this` (plus `running_jobs==0` and `active_proxied_apps==[]`); otherwise it defers to the next tick. See *Auto-upgrade never restarts mid-turn* |
| `DATAFYE_UPGRADE_JITTER_SECONDS` | `60` | Random pre-download sleep in `upgrade-check.sh` to spread fleet load on `downloads.n5corp.com` (single origin/no CDN) |
| `DATAFYE_AUTO_UPGRADE` | - | Set to `1` by `upgrade-check.sh` when it runs the freshly-downloaded `install.sh`. Arms the last-moment `running_jobs` re-check at the top of `install.sh` that aborts a mid-turn restart. Unset on fresh/manual installs (never blocked) |
| `DATAFYE_AGENT_MAX_BUFFER_SIZE` | `16777216` | The SDK's `max_buffer_size` (DAT-204) — the largest single message accepted off the CLI's stdout. The SDK default of 1 MB is small enough for one chart to exceed, and exceeding it ends the **turn**, not just the tool call. `READ_REFUSE_BYTES` (half this) is where `guard_oversized_read` refuses a `Read` outright |
| `DATAFYE_AGENT_BASH_MAX_TIMEOUT_MS` | `1800000` | The value `main.py` puts in the harness's own `BASH_MAX_TIMEOUT_MS` (DAT-203) — the ceiling on how long a foreground Bash command may run before the harness moves it to the background. 30 minutes, clearing the ~17-minute cold provision. A pre-set `BASH_MAX_TIMEOUT_MS` wins over both. See *Long environment commands run in the foreground* |

### A default pointing at a domain we no longer own (DAT-224)

`broker.py`'s `BROKER_REDIRECT_URL` defaulted to
`https://developer.datafye.io/broker-callback.html`, and **that domain does not resolve**.
Nothing sets the override, so every box sent a user linking a brokerage through
ConnectTrade's OAuth flow to a host that answers nothing.

The default was correct once: the workspace and the public site were one app on
`developer.datafye.io`, the workspace was later extracted into **Yukti** on its own
domain and the old host retired, and this default was left behind. The callback page
went with the workspace and is live at `https://yukti.datafye.ai/broker-callback.html`.

⚠️ **It survived because nothing fails until a real user links a real brokerage.** The
agent builds the URL, hands it to ConnectTrade and returns 200, so every test short of
completing an OAuth round trip passes, and the one component that would notice is the
**user's browser**, at the far end of a redirect nobody on our side observes.

⚠️ **A default pointing at a domain we no longer own is worse than no default.** An unset
value would have failed loudly at startup instead of silently at the worst possible
moment. The dead host is also retired from the 14 README references (including the tier
table, which told readers to point `developer.datafye.io` at a self-hosted agent), this
file, `PROJECT.md`, and the nginx placeholder page the installer writes.

Found by pulling on a domain mismatch between a CNAME and a `CLAUDE.md` in a *different*
repo. Worth remembering: **the doc discrepancy was the visible end of a broken product
path.**

### Presence: a reading user is still a user (DAT-169)

The accounts idle monitor measures idleness from `last_chat_activity_at`, and that field only ever advanced when a **chat turn** ran. So a user reading a backtest result, studying a scorecard, or thinking for half an hour looked exactly like a user who had closed the tab — and their box dormed underneath them.

`POST /v1/activity` is the fix's agent half: it bumps the same timestamp a turn does, without running one.

- **Deliberately the same field**, not a peer. The monitor's question is "when was this box last of use to somebody", and reading answers it as well as typing does; a second field would only make accounts take a `max` over two for no gain.
- **It only ever PREVENTS dormancy, never reverses it** — a stopped box cannot receive a heartbeat, so waking stays the auto-wake path's job.
- **⚠️ Visible tabs only, and the agent cannot enforce it.** That is the frontend's half of the contract: a hidden tab that kept pinging would pin every abandoned browser session's box awake forever, and dormancy would stop saving anything. The Yukti SPA gates on `document.hidden`, stops the interval when the tab goes away, and self-clears on sign-out (a tick with no token means the session is over). It also starts from the sandbox-state handler rather than only from `visibilitychange`, or a user who never switches tabs would send nothing at all.
- **Cheap by construction** — one assignment, no I/O. Every open tab calls it on an interval, so anything more would be a per-user background load whose only purpose is to say "still here".

**This is one of two guards and does not substitute for the other.** The heartbeat keeps a box warm for a present *user*; the warm signal below keeps it warm for active *work*. A provision running while nobody watches needs the second; a user reading while nothing runs needs the first.

**⚠️ The payoff is a threshold change that has NOT been made.** Datafye sits at 30 minutes (`datafye.accounts.idle.threshold.minutes`) precisely because it lacked this; N5 runs 10 because it has one. Lowering it is deliberately a separate, later step — doing it before the heartbeat is deployed everywhere would make the problem worse, not better. Note it would also invalidate the arithmetic behind the warm-signal window, which is documented as a third of the threshold.

### The warm signal: why a box refuses to sleep (DAT-184)

Accounts' idle monitor stops a box nobody has **chatted with** for 30 minutes. That is the wrong question, and on u1 it stopped a sandbox **sixteen minutes into a foundry provision** — nobody had typed anything, so by the only measure available the box was idle. It was not. On wake `--restart unless-stopped` restored the containers with the applications never deployed.

`warmth.active_work()` fills `/health`'s `active_proxied_apps`, which accounts **already** treats as busy in both places that matter (`agentBusy`, the pre-stop re-check; and `idleSnapshot`, the admin countdown). The plumbing was live and inert the whole time — the agent had always sent `[]`. **Filling the existing field rather than adding one means no accounts change and no coordinated deploy.** Values are self-describing (`env:data-flowing`, `env:provision`) because they surface raw in the admin panel and in logs, where a bare `true` tells an operator nothing about *why*.

Two signals today:

- **Data flowing** — one call to `GET /deployment/activity?activeWithinSeconds=N`, which fans out across every deployed dataset's feed/agg/history/reference **inside the platform** and returns a single verdict against the window we supply. The agent does not fan out over HTTP, and no threshold is baked into the platform. Cached and refreshed on a timer, never on the `/health` path.
- **A lifecycle command in flight** — the DAT-183 marker with a liveness check, read **live** rather than cached: it is a couple of small local files, and this is the signal covering a 17-minute provision, where being a minute stale at the wrong moment is exactly the u1 failure. **A hung command counts as warm on purpose** — a box with a wedged CLI is precisely the one you want left running so somebody can log in and find out why.

The third category from the ticket — compute the agent started outside a turn — reports nothing, because **there is none to report**: `prompt.py` forbids background execution outright after a backgrounded provision was orphaned with its session (DAT-185), and a turn in flight is already reported as `running_jobs`. The `compute:<name>` label space was reserved for when the agent could run and expose a long-lived app; **that is now live** (DAT-202/219/221): `warmth.running_apps()` globs one `.datafye-app-<port>.json` per app and reports each by name, with the **listening port as the only liveness test** and the marker's pid deliberately ignored.

Three decisions worth not relitigating:

- **⚠️ An idle or empty foundry reports nothing**, and it falls out of the definition rather than needing a carve-out: an empty foundry has no datasets, so no service reports activity. If merely having containers up kept a box awake, **dormancy would stop saving anything at all** — every provisioned sandbox in the fleet would be permanently warm. Verified live against a running, provisioned, idle Synthetic foundry: `active_work() == []`.
- **⚠️ Unreachable is not warm.** "I could not look" must not pin a box awake indefinitely on a probe that may never recover; it matches how accounts treats an agent *it* cannot reach. The case this looks like it loses — a box mid-provision whose API is not up yet — is covered by the in-flight signal, which is local and needs nothing to answer.
- **The window is a third of the idle threshold** (`DATAFYE_AGENT_WARM_DATA_WINDOW`, 600s vs Datafye's 30 min — deliberately *not* the 10 that N5 uses, because Yukti has no presence heartbeat yet), chosen against it rather than picked: long enough that no gap inside genuinely continuous work (a live feed, a replay advancing clock ticks, a fetch reporting progress) reads as cold, short enough that it can never dominate the dormancy decision.

**⚠️ Observation must never count as activity** — the platform guarantees this on its side (health pings, fetch-status polls and the activity reads themselves do not bump the signal, pinned by its own live test), which is what makes it safe to poll this once a minute forever. Without that, the accounts monitor's own polling would keep the entire fleet warm.

### Foundry readiness is DERIVED, not stored (DAT-198)

`/health` publishes a `foundry` block, and the model is handed the state and the reason in its prompt rather than discovering the situation by colliding with it. "Running" used to mean this Python process answers `/health` — a real fact, and almost useless: the agent can be perfectly healthy on a box whose foundry is half-built, wedged, or absent. On u1 that gap put a user's request onto a box three minutes into its first provision, and the agent had *already logged* `Datafye API MCP: NOT REACHABLE` fifteen seconds earlier. The information existed; nothing consumed it.

**Readiness is derived from three inputs and stored as no single fact:**

| Input | Where it comes from |
|---|---|
| **intent** | accounts, pushed to `POST /v1/foundry/intent`, cached at `~/.datafye/run/foundry-intent.json`. **Absent means running** |
| **in flight** | the DAT-183 marker, read with a liveness check (`in_flight_holder`) |
| **observed** | are the applications *answering* — interrogated, cached, refreshed in the background |

`derive()` combines them, and is a pure function precisely because the truth table is the part that is easy to get subtly backwards.

**⚠️ The first version stored readiness as one fact that every lifecycle command wrote.** It shipped and was reverted (`datafye-deploy` PR #11). The bug: an engineer SSHes in to debug and runs `foundry local stop`, the engine records `intended=stopped`, and the box then stays down on *every subsequent boot* — a debugging action promoted into standing policy by a component with no way to tell the two apart. **"An operation is in flight" is a fact about a process; "this box should have a running foundry" is a policy decision**, and the component performing an action is very often not the one that decided it should happen.

Three branches of the truth table are worth stating because the obvious reading is wrong:

- **intent `stopped` is READY**, not unready. A foundry the user asked to stop is in good order; calling it unready leaves the box permanently unhealthy, fixable only by starting an environment they explicitly did not want. It is ready even if observation finds it *serving* — somebody started it by hand, which is more than intended, not less. ⚠️ **But NOT when the environment is wedged (DAT-226)**, see below.
- **in-flight beats everything.** Mid-provision the deployment reports "not serving", which is indistinguishable from a real mismatch — so anything reconciling on that would try to fix an environment that is being built. That is the u1 collision, produced by something trying to help.
- **absent intent means running.** A sandbox exists to host a foundry, so "no deviation has ever been recorded" and "it should be running" are the same statement. Unknown would leave a fresh box permanently unready; stopped would leave it permanently empty.

**The observation is refreshed in the background, not on the request.** Interrogating costs real time when something is wrong, and `/health` is polled by accounts for dormancy decisions, by the upgrade cron every minute, and by the SPA — an agent that goes quiet is indistinguishable from a dead instance. `observe_forever` refreshes on a timer and `/health` serves the snapshot with its age; the in-flight read is cheap enough to do inline, so it is always current.

**⚠️ That background refresh is what buys per-service fidelity from one call.** The health ping asks the API about four services at once, and a *dead* service makes the API wait out its own 30s Rumi reply timeout. The engine dodges that by probing each service separately in parallel, because it sits on a command's critical path; here the refresh can simply afford to wait (`PING_TIMEOUT_SECONDS`, 40s). Bounding it below 30s would collapse every partial environment into "not answering at all", losing exactly the distinction the ping exists for.

**⚠️ Reading the ping right matters three times, and each fails silently in the wrong direction.** A healthy service reports an **empty** status, so a truthiness check reads exactly backwards. A healthy service actually sends `"status": null` on the wire (the ADM string is unset) — `entry.get("status") or ""` folds that in deliberately; the equivalent Java trap is that a plain `asText()` on a JSON null returns the *string* `"null"`. And a response listing **no services at all** is not a pass. The shape is `{"datasets":[{"services":{"<name>":{"status":…}}}],"trading":[…]}` — services is an object keyed by name, nested under per-system groups. All nine payloads `ServiceHealthTest` copied off a live foundry are replayed through the Python parse in the test suite, because the agent and the CLI disagreeing about one box's health would be worse than either being wrong alone.

### A stopped intent must not hide a wedged environment (DAT-226)

The accounts panel showed **"ready"** for a box whose CLI said **DEGRADED** (containers
up, deployment API not answering, services needing a relaunch), and showed no environment
type at all. Two causes.

**`derive()` short-circuited on intent.** The `intent == stopped` clause returned READY
**before looking at the observation at all**, so *any* environment on a box with intent
stopped read as ready, wedged or not. The clause is right about what it was written for:
a stopped foundry found SERVING is still ready, because that is **more than intended
rather than less**. But **"broken" is neither more nor less than intended**: nobody
asked for it, and it should never have been folded in with the other two. It was doubly
misleading here because the intent itself was wrong (DAT-222 below): **a misclassified
intent silenced the one signal that would have exposed it.**

**`observe()` could not tell two silences apart.** A quiet deployment API is all the HTTP
probe sees, so a cleanly stopped foundry and one whose containers are up with a dead API
produced the same answer. There is now **`OBSERVED_ABSENT` distinct from `OBSERVED_DOWN`**,
established by asking the CLI, one subprocess, and only on the path where something is
already known to be wrong. Under intent `stopped`, ABSENT is ready and DOWN is not.

**`env_type` is remembered once read** (`_remember_env_type`). It is read from the
deployment API, which was down, so it went blank exactly when someone is staring at a
broken box asking what kind it is. It is a property of what is **PROVISIONED**, not of
whether it answers, and it cannot change without a rebuild.

⚠️ **A truth table is worth printing in full every time it is touched: the row you break
is never the row you were thinking about.** Printing it caught a regression *this* change
introduced: `intent=running` with nothing provisioned fell through to UNKNOWN, where the
old collapsed-DOWN behaviour had correctly said NOT READY. It has its own branch now.

`CLI_PATH` is threaded through `observe_forever` rather than left to the `"datafye"`
default, so an operator who configured `DATAFYE_AGENT_CLI_PATH` does not get a different
binary on this path than everywhere else.

### The environment reports its TYPE alongside its readiness (DAT-217)

`/health`'s `foundry` block carries **`env_type`**: `foundry`, `trading`, or `null`. The
accounts sandbox list used to stack two subjects in one cell (the BOX's status, and
underneath it the ENVIRONMENT's readiness); giving the environment its own column needs it
to carry its own type.

- **Inferred from the deployed SYSTEMS** (`GET /deployment/systems`), not read from the
  deployment descriptor. The descriptor is more authoritative, but the systems list is one
  call on an endpoint the observation is already talking to. The tell is unambiguous: a
  trading environment stands up `datafye-broker-stocks-system` alongside the data systems
  and a foundry never does.
- ⚠️ **Matched on a SUBSTRING**, because system names carry the version
  (`datafye-api-system-2.0.37`). Comparing whole names would work today and break silently
  on the next release.
- ⚠️ **`null` means "could not tell", never "there is no environment"**, and it is passed
  through `derive()` as null rather than defaulted. A column that renders those the same
  way turns "I could not look" into "it is not working", the exact distinction the
  readiness block exists to preserve. An empty systems list is unknown for the same reason:
  it is a real answer about an environment with nothing in it, but it does not say which
  KIND it would be.

### Who may change the foundry intent, and how (DAT-214)

Intent is owned by accounts (DAT-198), and working through every actor that can mutate a foundry leaves **exactly one** that may legitimately change it:

| Actor | Changes intent | Why |
|---|---|---|
| Accounts — explicit stop/start | **No** | box-scoped: stopping your sandbox on Friday should not cost you your environment on Monday |
| Accounts — dormancy | **No** | the decisive test; otherwise a box wakes and declines to restore what nobody asked to lose |
| **User, through the model** | **Yes** | the only actor in the conversation, so the only one that knows |
| User, through the Yukti SPA | **No** | its Stop is box-scoped, same as accounts' |
| Operator, through the CLI | **No** | a debugging `stop` is not a policy statement |

So the agent is the sole production caller of `POST /accounts/{u}/sandbox/foundry-intent`, forwarding the **user's own JWT** — the same self-scoped channel the usage, satisfaction and feedback reporters use. The admin console keeps it as a manual override; the SPA never calls it.

**⚠️ The distinguishing factor is not the command.** `datafye foundry local stop` run because the user said *"shut it down for the month"* and the same command run because the model is working around a wedge are **byte-identical**. Inferring intent from the command is precisely the design that shipped and was reverted — it is what promotes a debugging action into standing policy.

**Two mechanisms, because a prompt rule alone will not hold.** The model has to classify a request as policy *and* remember a second action after it has already performed the first, and this codebase has paid for trusting that before: the prompt told the agent how to run long operations and it backgrounded a provision anyway, which was then orphaned with the session (DAT-185).

- **`set_environment_intent`** — a tool on the same in-process MCP server as `submit_feedback`/`submit_satisfaction`. The explicit path, for a decision the user actually stated.
- **`classify_environment_intent`** — a fourth post-stream Haiku sidecar alongside `generate_title`/`classify_lifecycle`/`analyze_satisfaction`. It **always runs**, so it does not depend on the model choosing anything.

They cannot fight: `_intent_recorded_this_turn` marks a turn where the tool fired, and the sidecar skips that turn and consumes the mark. **Explicit always beats inferred**, because the model's own statement is better evidence than an inference drawn from the same conversation.

**⚠️ The classifier is deliberately biased towards "no decision"**, and the bias is asymmetric for a reason: a wrong `stopped` leaves the user without an environment, while a missed one only means theirs keeps running — which is the default anyway. Pinned in tests: an unrecognised value, unparseable output, an API failure and a missing key all come back as no decision rather than a guess.

⚠️ **An APP is not the ENVIRONMENT (DAT-222).** A user asked Yukti to build a page, then
said *"cool, kill the app"* and later *"stop the first app"*. `_ENVIRONMENT_INTENT_PROMPT`
read that as a standing decision about their environment and recorded
`foundryIntent=stopped` in accounts. That is not cosmetic: `foundry-boot.sh`'s rule is
**intent stopped means do nothing, whatever the observation**, so the next time that box
dorms and wakes, the foundry stays down and the user has lost an environment they never
asked to lose, exactly the harm the classifier's bias exists to prevent.

- **Neither feature is wrong alone.** The classifier was written when the only thing a
  user could ask to stop *was* the environment; DAT-202 and DAT-219 then gave the model
  user-facing web apps, **a whole new noun that "stop" attaches to**. The prompt's
  negative examples were all environment operations performed as a step in work, so
  nothing in it separated an app from the platform underneath it. A composition failure,
  the same shape as DAT-225's.
- The classifier is now told plainly that an app is not the environment, carries the
  phrasings this actually failed on, and answers `none` when it cannot tell which of the
  two the user meant. Pinned by a check in `tests/test_prompt_audit.py`.
- **It now LOGS ITS OWN REASON whenever it decides.** The `reason` field was always asked
  for and always discarded, so the misfire left nothing behind saying what it thought it
  heard: the only evidence was a changed field in another service. **A decision that can
  leave someone without an environment has to be able to explain itself.**

Everything here is best-effort and never fails the turn, in line with every other agent → accounts reporter, and a self-hosted run with no accounts routing simply does not offer the tool.

### Stopping the environment before the box (DAT-125)

The accounts idle monitor used to call StopInstance on a live sandbox directly. That pulls the floor out from under a running foundry twice over: the Rumi applications are killed mid-write, risking unflushed transaction logs, and the containers are never marked stopped — so `--restart unless-stopped` faithfully restores them on the next boot **with no applications inside**, which is the DAT-171 wedge. It was observed doing exactly that on u1, sixteen minutes into a provision.

`POST /v1/foundry/stop` is the fix's agent half. Accounts calls it immediately before StopInstance, for both the idle `Dormant` stop and a deliberate user `Stop`.

**The reply is a decision, not a result.** `foundry.graceful_stop` returns one of four statuses, and only one of them changes what accounts does:

| status | what it means | accounts |
|---|---|---|
| `stopped` | the environment came down cleanly | stop the box |
| `absent` | there is no foundry here | stop the box |
| `failed` | the stop did not complete, but nothing is in flight | stop the box, loudly |
| `busy` | another operation owns the environment | **abort** |

Only `busy` aborts, and the asymmetry is the point: an environment mid-provision must not be cut off, but a stop that merely *failed* protects nothing by keeping the box up — and a sandbox that can never stop cleanly would then bill forever with nobody watching. `failed` is logged at WARNING because that line is the only trace a box went down without a clean stop.

Three details are load-bearing:

- **`busy` is decided from the DAT-183 marker, not from the refusal text.** The DAT-196 lock refuses with a sentence written for humans in another repo; keying on it would be a cross-repo contract with no compiler behind it. The marker is checked twice — before doing anything, and again after a failed stop, which is what catches an operation that started *during* the stop. The lock file is deliberately not used: it is never deleted on release, so its contents describe the *last* holder rather than necessarily a current one. PIDs are confirmed against `/proc/<pid>/cmdline`, since a recycled PID would otherwise leave the box permanently "busy".
- **The agent's timeout is deliberately shorter than the caller's.** The agent bounds the stop at `DATAFYE_AGENT_FOUNDRY_STOP_TIMEOUT` (240s) and accounts reads with a 300s timeout, so *this* side is what expires — an expiry here returns a structured answer naming what happened, while an expiry at the caller returns nothing and cannot be told from an unreachable box. A timed-out CLI is killed rather than left holding the environment lock with nobody waiting on it.
- **The endpoint is gated by a purpose-scoped `agent-lifecycle` token**, not by the user JWT and not left open like `/v1/credentials/update`. That endpoint only writes a cache value; an unauthenticated stop would let anyone who can reach the agent take a user's environment down. A *user* token would be wrong in the other direction — this is accounts acting as accounts, and borrowing a person's identity for a machine call would mint a user-equivalent credential on every dormancy tick. `auth.require_accounts_lifecycle_jwt` demands `purpose=agent-lifecycle` **and** `sub` matching this sandbox; a perfectly valid login token is refused. Both guards now share one `_decode_bearer`, so the signature/issuer/algorithm checks cannot drift between them.

The engine half is in `datafye-deploy`: `stop` no longer aborts when one application fails to shut down, and falls back to the container inventory when the deployment API cannot say which systems are deployed — because the API *is* one of the applications, so the box that most needs stopping was the one where the stop did least.

## Key Design Decisions

- **Native execution**: Agent runs directly on the host (not in Docker) because it needs to manage Docker containers for Datafye environments
- **Per-user instances**: Each user gets their own agent process (not shared)
- **Open source agent**: Agent source is public on GitHub — the value is in the Datafye platform, not the glue code
- **Local docs over MCP**: Datafye docs are on disk, not via a docs MCP server - faster and more reliable
- **Push bootstrap**: The agent learns its identity and its credentials-store encryption key from an accounts-signed JWT pushed to `POST /bootstrap` — it never reads AWS instance metadata. Accounts is the only writer in the relationship
- **Anthropic key as a credential**: The Anthropic key is not a startup env var; it lives in the encrypted credentials store and is delivered via the credentials push channel. The agent starts and stays manageable with no key — chat just returns 503/502 until a valid key arrives
- **Credentials via accounts**: The accounts service pushes credential updates to `/v1/credentials/update`; the old direct-write `/v1/credentials` endpoint is gone
- **Credentials synced to the environment**: `_apply_credentials_env()` exports the data-provider/broker/GitHub credentials from the encrypted store into `os.environ` (under both historical and current names, e.g. `POLYGON_API_KEY`+`MASSIVE_API_KEY`) on bootstrap and after every credentials push, so the Datafye CLI's `${VAR}` substitution in deployment descriptors resolves. (Previously only `ANTHROPIC_API_KEY` was exported.)
- **Summary-generated project titles**: on the first turn of a new conversation, `generate_title()` makes one cheap direct Anthropic call (haiku, `DATAFYE_AGENT_TITLE_MODEL`) to summarize the first message, renames the project, and emits a `title` SSE event that Yukti adopts. It's best-effort — any failure (no key, API error) returns None and the provisional `deduce_name` first-few-words name stays. This is the one place the agent calls the model directly rather than through the Agent SDK
- **Resumable turns (background buffer + resume/stop)**: a chat turn runs as a **background task** buffering its SSE frames (`_Turn`/`_turn_emit`/`_run_turn`/`_drain_turn` + a `_turns` registry), **decoupled from the HTTP response**, so a client that drops mid-turn can reconnect and replay. `POST /v1/chat` mints a `turn_id`, starts the task, and returns a response that **drains the buffer**; the first frame is a `turn` SSE event `{turn_id}`. `GET /v1/chat/resume?turn_id=&after=<seq>` replays buffered frames after `after` then continues live (404 if evicted → client reloads `/history`). `POST /v1/chat/stop?turn_id=` cancels the task; `_run_turn` `aclose()`s the SDK generator (kills `query()`'s subprocess promptly) and emits `Stopped`+`done`. Each frame carries an SSE `id: <seq>` the client uses as the resume cursor. Because a disconnect no longer cancels the turn (hence explicit Stop), a **watchdog** (`_turn_sweeper`) cancels a running turn with no consumer for **300s** (bumped from 90s so a **parked** turn — e.g. the client switching away to another project — survives a detour and can be resumed on switch-back) and evicts finished buffers after 120s. Core algorithm unit-tested.
- **Persisted Tool Detail (command + capped output)**: the machine tool-label commentary entry now carries the raw detail so the frontend's Tool Detail toggle can **replay it on reopen**, not just during a live turn. At `tool_use_start`, `append_commentary` stamps the entry with the `tool_id` + a formatted `command` (`_tool_command_text`, mirroring the frontend's `toolCommandText`); at `tool_result`, `conversations.attach_tool_output` matches by `tool_id` and attaches the output **capped at 2000 chars** (with an `output_error` flag on `is_error`). It rides through `/history` (no new endpoint) and reverses the earlier live-only design. The accounts Conversation view reads the same `command`/`output`/`output_error` fields off the commentary entries
- **Startup route guard**: a module-level check at the bottom of `main.py` asserts that `/health`, `/bootstrap`, and `/v1/chat` are registered, and raises `RuntimeError` at import/boot if any is missing. Otherwise a mis-applied edit that clobbered a route decorator would let the agent serve `/health` 200 while silently 404'ing `/bootstrap`, masking a broken agent as "Running" — a missing load-bearing route now crashes startup loudly
- **Accounts is the project registry**: Accounts mints conversation/project ids; the agent's own `POST`/`GET /v1/conversations` are legacy/unused. New chat threads arrive with an accounts-minted `conversation_id`, and `/v1/chat` materialises a local chat-layer record via `conversations.ensure()`
- **Persistent conversations**: `conversations.py` stores each conversation as one JSON file (name, message history, commentary audit trail, SDK session id). `/v1/chat` persists user+assistant turns and resumes the SDK session from disk, so chat survives an agent restart
- **No `AskUserQuestion` tool**: It's the Claude Code harness's structured-prompt tool with no UI handler in the Datafye workspace, so the model's question would silently vanish. Dropped from `INTERNAL_TOOLS`; the model asks inline in chat text instead
- **No background execution, with exactly one carve-out (DAT-185, DAT-219)**: `prompt.py` forbids `&`, `nohup`, `setsid`, `disown`, any detached wrapper **and the Bash tool's own `run_in_background` parameter** (DAT-218), and requires long environment operations to run in the **foreground** with a generous timeout. The prompt used to offer backgrounding as an alternative, and the agent took it: on a live RC sandbox a backgrounded provision was **orphaned when the session ended**, leaving containers up with their apps never deployed — the DAT-171 wedge, produced by the technique meant to avoid it. It cannot work here for a structural reason as well: `INTERNAL_TOOLS` has no `BashOutput` and no `KillShell`, so there is no way to read a background job's output or kill it — the agent can only poll side effects with shell tricks, and it was observed getting that wrong (a substring match that fired on the deprovision line). This is the same lesson as `AskUserQuestion` and the `Task` family for the third time: **never offer a capability the surface cannot service**. It is also wrong for the product — the user is watching one conversation, and work continuing invisibly after the turn ends appears nowhere in it. **The single exception is a user-facing app server** (DAT-219): every reason for the ban is a *supervision* problem, and for a server orphaning is the required behaviour, finishing is failure, and the listening port is the supervision. The rule handed to the model is *if it produces a result, run it in the foreground; if it answers on a port, detach it* (see *Showing the user an app the model built* above)
- **No `Task` family in `INTERNAL_TOOLS`**: the `Task`/`TaskCreate`/`TaskUpdate`/`TaskList`/`TaskGet`/`TaskStop`/`TaskOutput` tools are harness-only with no backend handler in this agent, so they're removed from `INTERNAL_TOOLS` (which feeds `allowed_tools`) to avoid offering the model dead tools
- **Project = folder**: each conversation/project is a directory under `<state>/projects/<id>/` holding `meta.json` + scaffolded `CLAUDE.md` (per-project memory), `PROJECT.md` (plain-language project narrative), `memory/`, and `.claude/skills/`. That folder is the chat turn's **cwd/workspace**, so the project's code, memory, and skills live together and survive a restart. `conversations.ensure()` materialises the folder for an accounts-minted id; legacy `<id>.json` records migrate into folders on load
- **Skills, three tiers**: the native `Skill` tool is enabled, with skills discovered from local plugins + project source. **System** skills ship read-only in `plugins/datafye` (installer/app-clone managed); **user-global** skills the agent authors into `<state>/plugins/user`; **per-project** skills in the project's `.claude/skills` (loaded via `setting_sources=["project"]`). The `author-skill` system skill teaches scope-aware authoring. Listing via `GET /v1/skills`; execution is chat-driven. We keep the engine-native mechanism for quality (Claude is post-trained for it) — the `SKILL.md` artifacts are engine-portable if we ever hand-roll the loader for another engine
- **Convention-based memory (one model), in three SCOPES**: durable facts are plain markdown the agent writes/reads, guided by a protocol in the system prompt. All of it is the agent's memory, so the distinguishing word is how far the knowledge reaches: **fleet** (`<app>/fleet_memory/`, read-only, ships with the build), **user** (`<state>/memory` + `<state>/CLAUDE.md`, this user across their projects — this was called *global* until fleet memory made that word ambiguous), and **project** (in the project folder).
- **Fleet memory ships with the agent build and is read-only**: `fleet_memory/` holds lessons distilled from across the whole fleet plus **its own `MEMORY.md` index**, rendered as a separate always-on block. Deliberately NOT merged into the user index — merging would mean surgically replacing seeded lines inside a file the agent itself rewrites during normal work. **The installer needs no change**: `clone_or_update_repo` does `git checkout -qf FETCH_HEAD` on the app dir, so the bank updates wholesale and self-prunes files deleted from the repo, arriving by the same path on a fresh install and an auto-upgrade. Read-only is enforced by the OS as well as the prompt — the installer runs as root and never chowns `${INSTALL_DIR}/app` to `datafye`, so the agent cannot write there. **Keep the bank BOUNDED**: its index is paid for on *every* turn, so a few *topic* files rewritten as they accumulate, never one file per lesson. An **unseeded bank is a valid state** — `build_memory_context` treats an index carrying only its header as empty and omits the whole scope, so the scaffold can ship before there is content. Curation is out of band and human-reviewed; never an automatic harvest, since user memory carries the user's own projects.
- **Tool lines name the real work**: `Read` is classified by **path** (memory / docs / samples / project) and `Bash` by **`_bash_activity`**, which splits on shell separators and keys on the **program and its arguments** rather than scanning the command string. The old substring net matched `test` anywhere, so `.../agent/latest/install.sh` and `ls src/test/…` both narrated as "Running the backtest". Now: `pytest`/`python -m pytest` are a test run; `backtest`/`paper`/`replay` as a *token* (split on `/_-.`) is a backtest, so `run_backtest.py` counts and `latest` cannot; the Datafye CLI with a `foundry`/`dataset`/`provision`/`apply` subcommand is environment work. Memory reads/writes narrate as **"Recalling from memory"** / **"Saving to memory"** in any scope (the scope is recoverable from Tool Detail), which is what makes it visible that fleet memory was consulted at all. Only the always-on `MEMORY.md` indexes + the small `CLAUDE.md` notes are injected; memory bodies are read on demand. The CLI's own auto-memory is **disabled** (`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`) because it has no global tier, is git-repo-scoped, and stores under `~/.claude` — it would be a second, uncontrolled store
- **Per-STEP cost attribution (DAT-137)**: each model round emits a `step`-kind commentary badge carrying `{new, carried}` — `new` is what the request appended to the prompt (uncached input **plus** cache creation, summed because a span under the minimum cacheable prefix silently bills as input rather than cache-creation), `carried` is the prefix re-read from cache. There is deliberately **no per-step output figure**: what arrives at `message_start` is a placeholder (1-3), and the real count lands on `message_delta`, which the SDK does not surface here. Whole-turn output stays correct via `model_usage`. **An `AssistantMessage` is not a model round** — the SDK emits one per content block and repeats that round's usage object, so a round is detected by the usage object *changing*; counting messages produced a badge per block and was also silently double-counting the old ticker. `step` is stamped on every entry of a round (an identity, not a dense index — gaps are fine) because one round can emit several narration lines, so the grouping cannot be inferred from how the rail renders.
- **Subagent work stays out of the rail (DAT-138)**: a subagent's messages arrive on the same stream carrying their own conversation's usage, distinguishable only by `parent_tool_use_id` (`None` = main thread). Both its **badge** and its **content** are suppressed — gating only the badge leaves `pending_blocks` thread-blind, so the subagent's prose flushes into the rail as if the agent had said it. Its tool calls are still counted and its tokens still reach accounts via `model_usage`. Delegation itself is off (`Task` absent from `INTERNAL_TOOLS`) because a subagent does **not** inherit `prompt.py`, so no rule about audience or voice governs delegated work.
- **Per-turn usage from `model_usage`**: usage is attributed from `ResultMessage.model_usage` — one delta per `(stage × model)` the turn actually used, idempotency-keyed per model — instead of the old flat single-`usage` read that undercounted a multi-step turn. The two cheap Haiku sidecars (`generate_title`, `classify_lifecycle`, `analyze_satisfaction`) fold their tokens in through a `usage_sink` param (their direct-API calls never appear in `model_usage`; cost is 0, tokens counted). Helpers: `_usage_delta_from_model_entry`, `_accumulate_turn_usage`, `_TURN_USAGE_FIELDS`. `conversations.set_last_message_usage` tags the assistant reply so `/history` carries `messages[].usage`. Falls back to the flat `usage` if the CLI emits no `model_usage`
- **Narration routing + guaranteed closing message**: the streamer buffers unrouted text as a **list of distinct blocks** (`pending_blocks`, one per narration sentence / reply paragraph — NOT a concatenated string, so sentences never glue into a run-on line at their periods). Blocks *followed by a tool call* are work-narration: each is flushed as **its own commentary line at `kind="narration"`** (one line per sentence) — a distinct kind from the machine tool-labels so the frontend's activity rail renders the agent's own voice a shade brighter. The final trailing blocks are the reply and go to the Conversation (joined with `\n\n` into `conversation_text`, flushed + persisted in the ResultMessage branch). If a turn ends on a tool call with **no trailing prose**, `conversation_text` falls back to the SDK's `ResultMessage.result` so the Conversation **always gets a closing message** (never a turn that ends silently on an action). `prompt.py` emits short high-level action lines (no commands/flags/filenames, no "Let me…" openers) + a plain final message, and is instructed to always end the turn with that closing message. The frontend renders work-narration + tool-labels + thinking inline as a dim per-turn **activity rail** above the full-weight reply (the old separate Work panel is gone)
- **Uncapped commentary**: the 400-entry `_MAX_COMMENTARY` cap in `conversations.append_commentary` was removed — commentary is the analytics record accounts persists, so truncating it loses data
- **Plain ASCII punctuation**: `prompt.py` instructs the agent to use ASCII punctuation everywhere (no em/en dashes, curly quotes, ellipsis char) because non-ASCII breaks the accounts `resultJson` storage
- **REPRESENTING DATAFYE prompt section**: a product-expert Q&A framing in `prompt.py` for when the user asks about Datafye itself — authoritative persona, accuracy/anti-confusion guard, graceful can't-answer, honesty — grounded in `{docs_dir}`
- **Downloadable output files**: each project gets an `outputs/` folder (distinct from `uploads/`: uploads are context *into* the agent, outputs are deliverables the user takes away). `conversations.list_outputs`/`output_file_path` (path-safety-guarded); endpoints `GET …/outputs` (list) + `GET …/outputs/{filename}` (download, `FileResponse`, JWT-gated). A post-stream diff of the `outputs/` snapshot emits an `artifact` SSE event per new/changed file. The prompt tells the agent to write deliverables into `outputs/`
- **Environment failures leave a report, and the prompt says to read it (DAT-171)**: a failed `provision`/`apply`/`start`/`stop` used to surface only `Failed to run admin script` wrapping `Command failed with exit code 1: docker exec ...`. The real error is written by the application to a log **inside the container**, and nothing pulled it out — so a root cause needed a live SSH session, which the agent does not have. Two changes outside this repo fixed that: `datafye-deploy` writes `~/.datafye/logs/foundry-<op>-<ts>.log` (cause chain + container inventory + the tail of each container's own app log, found by **searching** the container rather than assuming a path that has drifted), and `datafye-cli` tees every environment command's console output to `~/.datafye/logs/cli-<cmd>-<ts>.log` **flushing on every write** — because a provision killed mid-flight by the idle monitor raises nothing at all, so the only trace is one written as it went. The prompt now tells the agent to READ the newest report before deciding anything, to QUOTE the actual error to the user rather than saying "there is a problem with the platform", and to STOP rather than loop if a rebuild fails the same way (a second identical failure is a defect, not bad luck). ⚠️ The engine deliberately does **not** auto-roll-back a half-built environment: it is the only evidence of why the build failed, and tearing it down destroys the logs just collected. `status` remains the authority on whether the environment is actually partial
- **Foundry resource guard + cheat sheet**: a **RESOURCE GUARD** prompt block tells the agent to estimate a fetch/replay's worst-case (high-volume-day) peak memory + disk, check the instance's real limits (`free -m`/`df -h`), and if it won't fit with headroom (peak <70% RAM, ≥5 GB disk free) STOP and ask the user to resize to a named size first — plus a hard OOM rule (a combined-ticks one-day buffer >~1.3 GB OOMs the fixed 2 GB history heap and writes zero data; resizing doesn't help — fetch trades/quotes separately or split symbols). The empirical numbers (per-symbol-day rates, formula, instance-size map, worked examples) live in a bundled reference file `reference/foundry-resource-cost-cheatsheet.md` (ships with the app clone); `CHEATSHEET_PATH` is passed to `build_system_prompt` as `cheatsheet_path` and the guard points the agent to read it on demand. Measured empirically via a Yukti project (foundry 2.0.28, 2026-07-17); re-measure if the `-Xmx2g` history heap or version changes. Also see the **dataset gotchas** in the prompt's Environment Management section: crypto symbols are **bare** (`BTCUSD`, never `X:BTCUSD` — the crypto dataset prepends `X:` itself, so `X:BTCUSD` becomes `X:X:BTCUSD` and returns zero data); crypto fetch parameters (including `dataset`) go in the **request body** — for crypto you can omit `dataset` (the `/crypto` path implies Crypto); crypto is **trades-only** (quotes come back empty) and a crypto day is **24h**; and **one dataset at a time** (multi-dataset environments are unreliable — they fail partway, often at the crypto launch step — so switch datasets with `dataset remove`/`dataset add`, not deprovision+reprovision). The sandbox boots with a **pre-provisioned empty foundry** (API + MCP up, no datasets), so the agent ADDS a dataset (`foundry local dataset add`/`apply`) rather than running `provision` (which collides with the running platform and fails — the root cause behind DAT-93's "stale container" misdiagnosis). That empty foundry comes from **`datafye-foundry-boot.service`** (see *Foundry reconciliation at boot* below), which reconciles it on every boot in every mode — for a while it came from nowhere at all, which is DAT-170
- **Inferred per-project satisfaction**: `analyze_satisfaction` is a cheap Haiku sidecar (like `classify_lifecycle`, uses `TITLE_MODEL`) that infers a 1-5 rank + short reasons from the recent transcript, run post-stream. `_report_satisfaction_to_accounts` POSTs the *derived signal only* (never the raw conversation) to `POST /accounts/{u}/projects/{id}/satisfaction` (`source=inferred`, forwards the user JWT — the same self-scoped agent→accounts pattern the usage reporter uses). `conversations.set_satisfaction` caches it agent-side; a `"user"` source is sticky over an inferred one
- **In-conversation reporting tools (feedback + explicit satisfaction)**: `_build_reporting_mcp` stands up an in-process SDK-MCP server (`create_sdk_mcp_server`/`@tool`) with two tools the model can call mid-chat — `submit_feedback` (logs a bug/suggestion/general note to `POST /accounts/{u}/feedback`, which routes to Slack + a tracking issue; the response's **`ticket`** key — provider-neutral, `jira` kept as a fallback for older builds — is surfaced as "A tracking ticket was opened (`DAT-NNN`)" when one opens) and `submit_satisfaction` (records an *explicit* user rating with `source=user`, which is sticky over the inferred read). Both forward the user's own JWT — the same self-host-safe channel usage/satisfaction reporting uses, so the agent holds no Slack/JIRA creds. The server is only attached when routing is possible (a platform user with a forwarded JWT); a self-hosted run without accounts skips it, so the model falls back to the app's Send-feedback button. Prompt gains FEEDBACK + SATISFACTION sections: offer to log only after the user agrees, and capture a rating only when the user genuinely gives one (never fish for it). Ported from nvx-sutra-agent `219a09d`+`a155c39` (the explicit half)
- **Report for any registered project (not just `proj-` ids)**: the usage + satisfaction reporters and the post-stream satisfaction gate no longer require a `proj-` id prefix — accounts is the authority, so a **reconciled** browser-local project (a create that failed, imported into the registry via accounts' reconcile endpoint) also gets reported. The reporters accept any id (an unregistered one 404s and the best-effort call just logs it); the post-stream satisfaction gate keys on a **forwarded identity** (`auth_token` + `AGENT_USERNAME`) instead of the prefix, so it still skips a self-hosted run with no accounts. Ported from nvx-sutra-agent `675f63b`
- **Python-only algos**: No SDK/Java algos - all projects are pure Python using REST/WebSocket APIs
- **Conversational config**: Datasets, schemas, and environments are configured through chat, not forms

## Git Commits

Do not include `Co-Authored-By` trailers in commit messages.
