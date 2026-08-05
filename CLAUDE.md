# CLAUDE.md

This file provides guidance to Claude Code when working with this repository.

## Project Overview

Datafye Agent is a dedicated per-user AI backend for algorithmic trading strategy development. It wraps the Claude Agent SDK in a FastAPI service, giving each user an interactive agent session with access to Datafye documentation, the Datafye CLI, and file system tools for building Python-based algos.

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
├── conversations.py # Per-user strategy store — one FOLDER per strategy (meta.json + CLAUDE.md + PROJECT.md + memory/ + .claude/skills/)
├── memory.py        # Cross-session memory: global store + the memory-protocol block injected into the prompt
├── skills.py        # Skill plugin wiring (system + user-global plugins) and GET /v1/skills listing
├── paths.py         # Single agent state-root (DATAFYE_AGENT_STATE_DIR) all per-user state derives from
├── plugins/datafye/ # System (predefined) skills, installer-managed/read-only — ship with the app clone
├── tests/sanity_e2e.py  # Manual end-to-end sanity suite (real agent + real model calls; not CI)
├── requirements.txt # Python dependencies (incl. pyyaml for env_status descriptor parsing)
├── Dockerfile       # Legacy (agent now runs natively, Docker used for Datafye env containers)
├── install/
│   ├── install_template.sh   # Installer/upgrader template (--mode hosted|standalone, --ami-cleanup)
│   ├── first-boot.sh         # Marketplace first-boot script (reads EC2 user data, runs installer)
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
# Optional: relocate ALL writable state (credentials, strategies, user skills,
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

The auto-upgrade cron runs **every minute under `flock -n`** (a tick is a no-op while a prior check or an in-flight install still holds the lock), replacing the old blind `*/5 * * * *`. `upgrade-check.sh` **idle-gates** before it downloads/runs `install.sh`: it proceeds only when the agent's own `/health` reports `running_jobs==0` AND `active_proxied_apps==[]` AND `now - last_chat_activity_at >= DATAFYE_UPGRADE_INACTIVITY_WINDOW` (default 120s); otherwise it logs "deferred" and retries next tick. It adds a download **jitter** (`DATAFYE_UPGRADE_JITTER_SECONDS`, default 60 — `downloads.n5corp.com` is a single origin/no CDN), and the **top of `install.sh` does a last-moment `running_jobs` re-check that aborts the upgrade** if a turn started in the meantime — armed ONLY on the auto-upgrade path via the env flag `DATAFYE_AUTO_UPGRADE=1` (set when upgrade-check pipes `curl install.sh | DATAFYE_AUTO_UPGRADE=1 bash`), so fresh/manual installs are never blocked. Net: the agent never restarts mid-turn (which would drop the in-flight resumable-turn buffer). Unreachable `/health` → proceed (nothing to protect). Caveat: one transitional blind upgrade per box before it's gated; takes effect on the next publish + re-bake/auto-upgrade.

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
| `/health` | GET | Health check — `bootstrapped`, `anthropic_key_status`, credential status, idle signals. Always available, including before bootstrap |
| `/v1/bom` | GET | Dependency bill-of-materials — the single Datafye version this agent is built against (platform/samples/CLI/docs share one version). Reads `bom.json`; unauthenticated like `/health`; rendered on the Yukti agent surface |
| `/bootstrap` | POST | Accounts-only. Bootstrap the agent's identity + credentials-store key from an accounts-signed JWT (`Authorization: Bearer`, `purpose=agent-bootstrap`). Idempotent for the same user; 409 on rebind |
| `/v1/chat` | POST | SSE streaming chat with agent. JWT-protected; 503 if no Anthropic key, 502 if invalid |
| `/v1/credentials` | POST | REMOVED — returns 410 Gone; credential writes go through the accounts service |
| `/v1/credentials/update` | POST | Accounts-only. Push a single credential `{provider, value}` into the encrypted store; 204 |
| `/v1/credentials/status` | GET | Check which credentials are configured (JWT-protected) |
| `/v1/broker/brokers` | GET | List brokers Datafye supports (StocksBroker enum) |
| `/v1/broker/connections` | GET | List the user's brokerage connections with linked accounts |
| `/v1/broker/connections` | POST | Create a ConnectTrade OAuth URL for a chosen broker; body `{type, broker}` |
| `/v1/skills` | GET | List skills available to the agent across all tiers: `system` (predefined, read-only), `user-global` (agent-authored, reusable), `user-strategy` (per-strategy; pass `?conversation_id=`). JWT-protected. Execution is chat-driven ("use the `<name>` skill"), no separate run endpoint |
| `/v1/broker/connections/{id}` | DELETE | Revoke a brokerage connection |
| `/v1/conversations` | GET | List conversations (projects), most-recently-updated first. **LEGACY/UNUSED** — accounts is the authoritative project registry; the frontend lists from accounts |
| `/v1/conversations` | POST | Create a conversation (agent mints the id, deduces a name). **LEGACY/UNUSED** — accounts mints project ids; new chat threads arrive with an accounts-minted `conversation_id` that `/v1/chat` materialises via `conversations.ensure()` |
| `/v1/conversations/{id}` | PATCH | Rename a conversation; 404 if absent |
| `/v1/conversations/{id}` | DELETE | Permanently delete a strategy's agent-side folder via `conversations.delete()` (path-safety guard refuses anything outside the strategies base); 204 on success, 404 if the agent never materialised it. Accounts deletes its own project record separately |
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
| `title` | Summary-generated strategy title (`{conversation_id, name}`). Emitted once on the first turn of a new conversation after `generate_title()` summarizes the first message and renames the strategy; Yukti adopts it over the provisional `deduce_name` |
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
| `DATAFYE_AGENT_TITLE_MODEL` | `claude-haiku-4-5` | Cheap model used only by `generate_title()` to summarize a new strategy's first message into a title (direct Anthropic `/v1/messages` httpx call, never the main reasoning model) |
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
| `DATAFYE_AGENT_BROKER_REDIRECT_URL` | `https://developer.datafye.io/broker-callback.html` | OAuth redirect target |
| `DATAFYE_AGENT_BROKER_STATE_FILE` | `~/.datafye/agent/broker_user.json` | Where the ConnectTrade user_id / user_secret are persisted (TODO: migrate to accounts-manager) |
| `DATAFYE_AGENT_DEPLOYMENT_API_URL` | `http://local-foundry-dev-api.datafye.local:7776` | Datafye deployment REST API base URL — read after a chat turn to derive `descriptor` / `env_status` from the deployment descriptor (`GET .../deployment/{descriptor,datasets,symbols}`) |
| `DATAFYE_AGENT_STATE_DIR` | `~/.datafye/agent` | Single root for ALL per-user writable state (credentials, strategies, user-skill plugin, user memory). Relocate everything with one var — used by local tests to avoid polluting `~/.datafye`. Each narrower var below still overrides when set |
| `DATAFYE_AGENT_STRATEGIES_DIR` | `<state>/strategies` | Base dir holding one FOLDER per strategy (`DATAFYE_AGENT_CONVERSATIONS_DIR` still honored for back-compat; legacy `<id>.json` files migrate into folders on load) |
| `DATAFYE_AGENT_SYSTEM_PLUGIN_DIR` | `<app>/plugins/datafye` | Read-only system-skill plugin (ships with the app clone) |
| `DATAFYE_AGENT_USER_PLUGIN_DIR` | `<state>/plugins/user` | Writable user-global skill plugin (agent authors skills here) |
| `CLAUDE_CODE_DISABLE_AUTO_MEMORY` | `1` (set by the agent) | Disables the `claude` CLI's own auto-memory so the agent runs ONE explicit memory model (see Key Design Decisions). Not a `DATAFYE_` var; `os.environ.setdefault` in main.py, overridable |
| `DATAFYE_AGENT_JWT_LEEWAY_SECONDS` | `60` | Clock-skew tolerance applied to time-based JWT claims (iat/nbf/exp) when verifying accounts-signed tokens — avoids "token not yet valid (iat)" failures from clock drift |
| `DATAFYE_UPGRADE_INACTIVITY_WINDOW` | `120` | Auto-upgrade idle gate (seconds). `upgrade-check.sh` proceeds only when `now - last_chat_activity_at >= this` (plus `running_jobs==0` and `active_proxied_apps==[]`); otherwise it defers to the next tick. See *Auto-upgrade never restarts mid-turn* |
| `DATAFYE_UPGRADE_JITTER_SECONDS` | `60` | Random pre-download sleep in `upgrade-check.sh` to spread fleet load on `downloads.n5corp.com` (single origin/no CDN) |
| `DATAFYE_AUTO_UPGRADE` | - | Set to `1` by `upgrade-check.sh` when it runs the freshly-downloaded `install.sh`. Arms the last-moment `running_jobs` re-check at the top of `install.sh` that aborts a mid-turn restart. Unset on fresh/manual installs (never blocked) |

## Key Design Decisions

- **Native execution**: Agent runs directly on the host (not in Docker) because it needs to manage Docker containers for Datafye environments
- **Per-user instances**: Each user gets their own agent process (not shared)
- **Open source agent**: Agent source is public on GitHub — the value is in the Datafye platform, not the glue code
- **Local docs over MCP**: Datafye docs are on disk, not via a docs MCP server - faster and more reliable
- **Push bootstrap**: The agent learns its identity and its credentials-store encryption key from an accounts-signed JWT pushed to `POST /bootstrap` — it never reads AWS instance metadata. Accounts is the only writer in the relationship
- **Anthropic key as a credential**: The Anthropic key is not a startup env var; it lives in the encrypted credentials store and is delivered via the credentials push channel. The agent starts and stays manageable with no key — chat just returns 503/502 until a valid key arrives
- **Credentials via accounts**: The accounts service pushes credential updates to `/v1/credentials/update`; the old direct-write `/v1/credentials` endpoint is gone
- **Credentials synced to the environment**: `_apply_credentials_env()` exports the data-provider/broker/GitHub credentials from the encrypted store into `os.environ` (under both historical and current names, e.g. `POLYGON_API_KEY`+`MASSIVE_API_KEY`) on bootstrap and after every credentials push, so the Datafye CLI's `${VAR}` substitution in deployment descriptors resolves. (Previously only `ANTHROPIC_API_KEY` was exported.)
- **Summary-generated strategy titles**: on the first turn of a new conversation, `generate_title()` makes one cheap direct Anthropic call (haiku, `DATAFYE_AGENT_TITLE_MODEL`) to summarize the first message, renames the strategy, and emits a `title` SSE event that Yukti adopts. It's best-effort — any failure (no key, API error) returns None and the provisional `deduce_name` first-few-words name stays. This is the one place the agent calls the model directly rather than through the Agent SDK
- **Resumable turns (background buffer + resume/stop)**: a chat turn runs as a **background task** buffering its SSE frames (`_Turn`/`_turn_emit`/`_run_turn`/`_drain_turn` + a `_turns` registry), **decoupled from the HTTP response**, so a client that drops mid-turn can reconnect and replay. `POST /v1/chat` mints a `turn_id`, starts the task, and returns a response that **drains the buffer**; the first frame is a `turn` SSE event `{turn_id}`. `GET /v1/chat/resume?turn_id=&after=<seq>` replays buffered frames after `after` then continues live (404 if evicted → client reloads `/history`). `POST /v1/chat/stop?turn_id=` cancels the task; `_run_turn` `aclose()`s the SDK generator (kills `query()`'s subprocess promptly) and emits `Stopped`+`done`. Each frame carries an SSE `id: <seq>` the client uses as the resume cursor. Because a disconnect no longer cancels the turn (hence explicit Stop), a **watchdog** (`_turn_sweeper`) cancels a running turn with no consumer for **300s** (bumped from 90s so a **parked** turn — e.g. the client switching away to another project — survives a detour and can be resumed on switch-back) and evicts finished buffers after 120s. Core algorithm unit-tested.
- **Persisted Tool Detail (command + capped output)**: the machine tool-label commentary entry now carries the raw detail so the frontend's Tool Detail toggle can **replay it on reopen**, not just during a live turn. At `tool_use_start`, `append_commentary` stamps the entry with the `tool_id` + a formatted `command` (`_tool_command_text`, mirroring the frontend's `toolCommandText`); at `tool_result`, `conversations.attach_tool_output` matches by `tool_id` and attaches the output **capped at 2000 chars** (with an `output_error` flag on `is_error`). It rides through `/history` (no new endpoint) and reverses the earlier live-only design. The accounts Conversation view reads the same `command`/`output`/`output_error` fields off the commentary entries
- **Startup route guard**: a module-level check at the bottom of `main.py` asserts that `/health`, `/bootstrap`, and `/v1/chat` are registered, and raises `RuntimeError` at import/boot if any is missing. Otherwise a mis-applied edit that clobbered a route decorator would let the agent serve `/health` 200 while silently 404'ing `/bootstrap`, masking a broken agent as "Running" — a missing load-bearing route now crashes startup loudly
- **Accounts is the project registry**: Accounts mints conversation/project ids; the agent's own `POST`/`GET /v1/conversations` are legacy/unused. New chat threads arrive with an accounts-minted `conversation_id`, and `/v1/chat` materialises a local chat-layer record via `conversations.ensure()`
- **Persistent conversations**: `conversations.py` stores each conversation as one JSON file (name, message history, commentary audit trail, SDK session id). `/v1/chat` persists user+assistant turns and resumes the SDK session from disk, so chat survives an agent restart
- **No `AskUserQuestion` tool**: It's the Claude Code harness's structured-prompt tool with no UI handler in the Datafye workspace, so the model's question would silently vanish. Dropped from `INTERNAL_TOOLS`; the model asks inline in chat text instead
- **No `Task` family in `INTERNAL_TOOLS`**: the `Task`/`TaskCreate`/`TaskUpdate`/`TaskList`/`TaskGet`/`TaskStop`/`TaskOutput` tools are harness-only with no backend handler in this agent, so they're removed from `INTERNAL_TOOLS` (which feeds `allowed_tools`) to avoid offering the model dead tools
- **Strategy = folder**: each conversation/strategy is a directory under `<state>/strategies/<id>/` holding `meta.json` + scaffolded `CLAUDE.md` (per-strategy memory), `PROJECT.md` (plain-language strategy narrative), `memory/`, and `.claude/skills/`. That folder is the chat turn's **cwd/workspace**, so the strategy's code, memory, and skills live together and survive a restart. `conversations.ensure()` materialises the folder for an accounts-minted id; legacy `<id>.json` records migrate into folders on load
- **Skills, three tiers**: the native `Skill` tool is enabled, with skills discovered from local plugins + project source. **System** skills ship read-only in `plugins/datafye` (installer/app-clone managed); **user-global** skills the agent authors into `<state>/plugins/user`; **per-strategy** skills in the strategy's `.claude/skills` (loaded via `setting_sources=["project"]`). The `author-skill` system skill teaches scope-aware authoring. Listing via `GET /v1/skills`; execution is chat-driven. We keep the engine-native mechanism for quality (Claude is post-trained for it) — the `SKILL.md` artifacts are engine-portable if we ever hand-roll the loader for another engine
- **Convention-based memory (one model), in three SCOPES**: durable facts are plain markdown the agent writes/reads, guided by a protocol in the system prompt. All of it is the agent's memory, so the distinguishing word is how far the knowledge reaches: **fleet** (`<app>/fleet_memory/`, read-only, ships with the build), **user** (`<state>/memory` + `<state>/CLAUDE.md`, this user across their strategies — this was called *global* until fleet memory made that word ambiguous), and **strategy** (in the strategy folder).
- **Fleet memory ships with the agent build and is read-only**: `fleet_memory/` holds lessons distilled from across the whole fleet plus **its own `MEMORY.md` index**, rendered as a separate always-on block. Deliberately NOT merged into the user index — merging would mean surgically replacing seeded lines inside a file the agent itself rewrites during normal work. **The installer needs no change**: `clone_or_update_repo` does `git checkout -qf FETCH_HEAD` on the app dir, so the bank updates wholesale and self-prunes files deleted from the repo, arriving by the same path on a fresh install and an auto-upgrade. Read-only is enforced by the OS as well as the prompt — the installer runs as root and never chowns `${INSTALL_DIR}/app` to `datafye`, so the agent cannot write there. **Keep the bank BOUNDED**: its index is paid for on *every* turn, so a few *topic* files rewritten as they accumulate, never one file per lesson. An **unseeded bank is a valid state** — `build_memory_context` treats an index carrying only its header as empty and omits the whole scope, so the scaffold can ship before there is content. Curation is out of band and human-reviewed; never an automatic harvest, since user memory carries the user's own strategies.
- **Tool lines name the real work**: `Read` is classified by **path** (memory / docs / samples / project) and `Bash` by **`_bash_activity`**, which splits on shell separators and keys on the **program and its arguments** rather than scanning the command string. The old substring net matched `test` anywhere, so `.../agent/latest/install.sh` and `ls src/test/…` both narrated as "Running the backtest". Now: `pytest`/`python -m pytest` are a test run; `backtest`/`paper`/`replay` as a *token* (split on `/_-.`) is a backtest, so `run_backtest.py` counts and `latest` cannot; the Datafye CLI with a `foundry`/`dataset`/`provision`/`apply` subcommand is environment work. Memory reads/writes narrate as **"Recalling from memory"** / **"Saving to memory"** in any scope (the scope is recoverable from Tool Detail), which is what makes it visible that fleet memory was consulted at all. Only the always-on `MEMORY.md` indexes + the small `CLAUDE.md` notes are injected; memory bodies are read on demand. The CLI's own auto-memory is **disabled** (`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`) because it has no global tier, is git-repo-scoped, and stores under `~/.claude` — it would be a second, uncontrolled store
- **Per-STEP cost attribution (DAT-137)**: each model round emits a `step`-kind commentary badge carrying `{new, carried}` — `new` is what the request appended to the prompt (uncached input **plus** cache creation, summed because a span under the minimum cacheable prefix silently bills as input rather than cache-creation), `carried` is the prefix re-read from cache. There is deliberately **no per-step output figure**: what arrives at `message_start` is a placeholder (1-3), and the real count lands on `message_delta`, which the SDK does not surface here. Whole-turn output stays correct via `model_usage`. **An `AssistantMessage` is not a model round** — the SDK emits one per content block and repeats that round's usage object, so a round is detected by the usage object *changing*; counting messages produced a badge per block and was also silently double-counting the old ticker. `step` is stamped on every entry of a round (an identity, not a dense index — gaps are fine) because one round can emit several narration lines, so the grouping cannot be inferred from how the rail renders.
- **Subagent work stays out of the rail (DAT-138)**: a subagent's messages arrive on the same stream carrying their own conversation's usage, distinguishable only by `parent_tool_use_id` (`None` = main thread). Both its **badge** and its **content** are suppressed — gating only the badge leaves `pending_blocks` thread-blind, so the subagent's prose flushes into the rail as if the agent had said it. Its tool calls are still counted and its tokens still reach accounts via `model_usage`. Delegation itself is off (`Task` absent from `INTERNAL_TOOLS`) because a subagent does **not** inherit `prompt.py`, so no rule about audience or voice governs delegated work.
- **Per-turn usage from `model_usage`**: usage is attributed from `ResultMessage.model_usage` — one delta per `(stage × model)` the turn actually used, idempotency-keyed per model — instead of the old flat single-`usage` read that undercounted a multi-step turn. The two cheap Haiku sidecars (`generate_title`, `classify_lifecycle`, `analyze_satisfaction`) fold their tokens in through a `usage_sink` param (their direct-API calls never appear in `model_usage`; cost is 0, tokens counted). Helpers: `_usage_delta_from_model_entry`, `_accumulate_turn_usage`, `_TURN_USAGE_FIELDS`. `conversations.set_last_message_usage` tags the assistant reply so `/history` carries `messages[].usage`. Falls back to the flat `usage` if the CLI emits no `model_usage`
- **Narration routing + guaranteed closing message**: the streamer buffers unrouted text as a **list of distinct blocks** (`pending_blocks`, one per narration sentence / reply paragraph — NOT a concatenated string, so sentences never glue into a run-on line at their periods). Blocks *followed by a tool call* are work-narration: each is flushed as **its own commentary line at `kind="narration"`** (one line per sentence) — a distinct kind from the machine tool-labels so the frontend's activity rail renders the agent's own voice a shade brighter. The final trailing blocks are the reply and go to the Conversation (joined with `\n\n` into `conversation_text`, flushed + persisted in the ResultMessage branch). If a turn ends on a tool call with **no trailing prose**, `conversation_text` falls back to the SDK's `ResultMessage.result` so the Conversation **always gets a closing message** (never a turn that ends silently on an action). `prompt.py` emits short high-level action lines (no commands/flags/filenames, no "Let me…" openers) + a plain final message, and is instructed to always end the turn with that closing message. The frontend renders work-narration + tool-labels + thinking inline as a dim per-turn **activity rail** above the full-weight reply (the old separate Work panel is gone)
- **Uncapped commentary**: the 400-entry `_MAX_COMMENTARY` cap in `conversations.append_commentary` was removed — commentary is the analytics record accounts persists, so truncating it loses data
- **Plain ASCII punctuation**: `prompt.py` instructs the agent to use ASCII punctuation everywhere (no em/en dashes, curly quotes, ellipsis char) because non-ASCII breaks the accounts `resultJson` storage
- **REPRESENTING DATAFYE prompt section**: a product-expert Q&A framing in `prompt.py` for when the user asks about Datafye itself — authoritative persona, accuracy/anti-confusion guard, graceful can't-answer, honesty — grounded in `{docs_dir}`
- **Downloadable output files**: each project gets an `outputs/` folder (distinct from `uploads/`: uploads are context *into* the agent, outputs are deliverables the user takes away). `conversations.list_outputs`/`output_file_path` (path-safety-guarded); endpoints `GET …/outputs` (list) + `GET …/outputs/{filename}` (download, `FileResponse`, JWT-gated). A post-stream diff of the `outputs/` snapshot emits an `artifact` SSE event per new/changed file. The prompt tells the agent to write deliverables into `outputs/`
- **Foundry resource guard + cheat sheet**: a **RESOURCE GUARD** prompt block tells the agent to estimate a fetch/replay's worst-case (high-volume-day) peak memory + disk, check the instance's real limits (`free -m`/`df -h`), and if it won't fit with headroom (peak <70% RAM, ≥5 GB disk free) STOP and ask the user to resize to a named size first — plus a hard OOM rule (a combined-ticks one-day buffer >~1.3 GB OOMs the fixed 2 GB history heap and writes zero data; resizing doesn't help — fetch trades/quotes separately or split symbols). The empirical numbers (per-symbol-day rates, formula, instance-size map, worked examples) live in a bundled reference file `reference/foundry-resource-cost-cheatsheet.md` (ships with the app clone); `CHEATSHEET_PATH` is passed to `build_system_prompt` as `cheatsheet_path` and the guard points the agent to read it on demand. Measured empirically via a Yukti project (foundry 2.0.28, 2026-07-17); re-measure if the `-Xmx2g` history heap or version changes. Also see the **dataset gotchas** in the prompt's Environment Management section: crypto symbols are **bare** (`BTCUSD`, never `X:BTCUSD` — the crypto dataset prepends `X:` itself, so `X:BTCUSD` becomes `X:X:BTCUSD` and returns zero data); crypto fetch parameters (including `dataset`) go in the **request body** — for crypto you can omit `dataset` (the `/crypto` path implies Crypto); crypto is **trades-only** (quotes come back empty) and a crypto day is **24h**; and **one dataset at a time** (multi-dataset environments are unreliable — they fail partway, often at the crypto launch step — so switch datasets with `dataset remove`/`dataset add`, not deprovision+reprovision). The sandbox boots with a **pre-provisioned empty foundry** (API + MCP up, no datasets), so the agent ADDS a dataset (`foundry local dataset add`/`apply`) rather than running `provision` (which collides with the running platform and fails — the root cause behind DAT-93's "stale container" misdiagnosis)
- **Inferred per-project satisfaction**: `analyze_satisfaction` is a cheap Haiku sidecar (like `classify_lifecycle`, uses `TITLE_MODEL`) that infers a 1-5 rank + short reasons from the recent transcript, run post-stream. `_report_satisfaction_to_accounts` POSTs the *derived signal only* (never the raw conversation) to `POST /accounts/{u}/projects/{id}/satisfaction` (`source=inferred`, forwards the user JWT — the same self-scoped agent→accounts pattern the usage reporter uses). `conversations.set_satisfaction` caches it agent-side; a `"user"` source is sticky over an inferred one
- **In-conversation reporting tools (feedback + explicit satisfaction)**: `_build_reporting_mcp` stands up an in-process SDK-MCP server (`create_sdk_mcp_server`/`@tool`) with two tools the model can call mid-chat — `submit_feedback` (logs a bug/suggestion/general note to `POST /accounts/{u}/feedback`, which routes to Slack + a tracking issue; the response's **`ticket`** key — provider-neutral, `jira` kept as a fallback for older builds — is surfaced as "A tracking ticket was opened (`DAT-NNN`)" when one opens) and `submit_satisfaction` (records an *explicit* user rating with `source=user`, which is sticky over the inferred read). Both forward the user's own JWT — the same self-host-safe channel usage/satisfaction reporting uses, so the agent holds no Slack/JIRA creds. The server is only attached when routing is possible (a platform user with a forwarded JWT); a self-hosted run without accounts skips it, so the model falls back to the app's Send-feedback button. Prompt gains FEEDBACK + SATISFACTION sections: offer to log only after the user agrees, and capture a rating only when the user genuinely gives one (never fish for it). Ported from nvx-sutra-agent `219a09d`+`a155c39` (the explicit half)
- **Report for any registered project (not just `proj-` ids)**: the usage + satisfaction reporters and the post-stream satisfaction gate no longer require a `proj-` id prefix — accounts is the authority, so a **reconciled** browser-local project (a create that failed, imported into the registry via accounts' reconcile endpoint) also gets reported. The reporters accept any id (an unregistered one 404s and the best-effort call just logs it); the post-stream satisfaction gate keys on a **forwarded identity** (`auth_token` + `AGENT_USERNAME`) instead of the prefix, so it still skips a self-hosted run with no accounts. Ported from nvx-sutra-agent `675f63b`
- **Python-only algos**: No SDK/Java algos - all strategies are pure Python using REST/WebSocket APIs
- **Conversational config**: Datasets, schemas, and environments are configured through chat, not forms

## Git Commits

Do not include `Co-Authored-By` trailers in commit messages.
