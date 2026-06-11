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
# global memory) under one root — handy to keep local runs out of ~/.datafye
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
| `/v1/conversations/{id}/history` | GET | Replay a conversation's `messages` and `commentary` audit trail |

Every endpoint except `/health` and `/bootstrap` is gated by the
`require_bootstrapped` dependency and returns 503 until the accounts bootstrap
push lands.

## SSE Event Types

| Event | Description |
|-------|-------------|
| `init` | Session initialized |
| `title` | Summary-generated strategy title (`{conversation_id, name}`). Emitted once on the first turn of a new conversation after `generate_title()` summarizes the first message and renames the strategy; Yukti adopts it over the provisional `deduce_name` |
| `content` | Text content chunk |
| `thinking` | Agent reasoning |
| `tool_use_start` | Tool invocation started |
| `tool_result` | Tool execution result |
| `commentary` | Background-activity line for the workspace activity panel (`{text}`). Emitted for notable tool calls — Bash and MCP — and also appended to the conversation's commentary audit trail |
| `result` | Final result with metadata |
| `descriptor` | Raw deployment-descriptor YAML text (`{descriptor}`), relayed by the frontend to accounts. Best-effort read of the deployed environment's deployment REST API after a chat turn |
| `env_status` | Environment state, derived from the descriptor: `{status, env_type, datasets, symbols, broker, mode}`. The environment-type field is keyed **`env_type`** (NOT `type`) so it can't collide with the SSE frame's own `type` discriminator that `sse_event` sets |
| `scorecard_update` | Test results (for frontend) |
| `chart_data` | Chart data push (for frontend) |
| `error` | Error occurred |
| `done` | Stream complete |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DATAFYE_AGENT_ANTHROPIC_API_KEY` | - | Local-dev seed only. The Anthropic key is a *credential* — in production it lives in the encrypted credentials store and is delivered by accounts via `/v1/credentials/update`. This env var only seeds the store the first time it is created |
| `DATAFYE_AGENT_MODEL` | `opus` | Claude model |
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
| `DATAFYE_AGENT_CONNECTTRADE_CLIENT_ID` | - | ConnectTrade client ID |
| `DATAFYE_AGENT_CONNECTTRADE_CLIENT_SECRET` | - | ConnectTrade client secret |
| `DATAFYE_AGENT_CONNECTTRADE_USER_ID` | - | ConnectTrade user ID |
| `DATAFYE_AGENT_CONNECTTRADE_USER_SECRET` | - | ConnectTrade user secret |
| `DATAFYE_AGENT_GITHUB_USER` | - | Personal GitHub username (optional) |
| `DATAFYE_AGENT_GITHUB_TOKEN` | - | Personal GitHub token (optional) |
| `DATAFYE_AGENT_GITHUB_ORG` | `datafye` | GitHub org for algo repos |
| `DATAFYE_AGENT_MCP_SERVERS_ADDITIONAL` | `[]` | Additional MCP servers (JSON) |
| `DATAFYE_AGENT_CONNECTTRADE_API_URL` | `https://api.connecttrade.com` | ConnectTrade REST base URL |
| `DATAFYE_AGENT_BROKER_REDIRECT_URL` | `https://developer.datafye.io/broker-callback.html` | OAuth redirect target |
| `DATAFYE_AGENT_BROKER_STATE_FILE` | `~/.datafye/agent/broker_user.json` | Where the ConnectTrade user_id / user_secret are persisted (TODO: migrate to accounts-manager) |
| `DATAFYE_AGENT_DEPLOYMENT_API_URL` | `http://local-foundry-dev-api.datafye.local:7776` | Datafye deployment REST API base URL — read after a chat turn to derive `descriptor` / `env_status` from the deployment descriptor (`GET .../deployment/{descriptor,datasets,symbols}`) |
| `DATAFYE_AGENT_STATE_DIR` | `~/.datafye/agent` | Single root for ALL per-user writable state (credentials, strategies, user-skill plugin, global memory). Relocate everything with one var — used by local tests to avoid polluting `~/.datafye`. Each narrower var below still overrides when set |
| `DATAFYE_AGENT_STRATEGIES_DIR` | `<state>/strategies` | Base dir holding one FOLDER per strategy (`DATAFYE_AGENT_CONVERSATIONS_DIR` still honored for back-compat; legacy `<id>.json` files migrate into folders on load) |
| `DATAFYE_AGENT_SYSTEM_PLUGIN_DIR` | `<app>/plugins/datafye` | Read-only system-skill plugin (ships with the app clone) |
| `DATAFYE_AGENT_USER_PLUGIN_DIR` | `<state>/plugins/user` | Writable user-global skill plugin (agent authors skills here) |
| `CLAUDE_CODE_DISABLE_AUTO_MEMORY` | `1` (set by the agent) | Disables the `claude` CLI's own auto-memory so the agent runs ONE explicit memory model (see Key Design Decisions). Not a `DATAFYE_` var; `os.environ.setdefault` in main.py, overridable |
| `DATAFYE_AGENT_JWT_LEEWAY_SECONDS` | `60` | Clock-skew tolerance applied to time-based JWT claims (iat/nbf/exp) when verifying accounts-signed tokens — avoids "token not yet valid (iat)" failures from clock drift |

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
- **Startup route guard**: a module-level check at the bottom of `main.py` asserts that `/health`, `/bootstrap`, and `/v1/chat` are registered, and raises `RuntimeError` at import/boot if any is missing. Otherwise a mis-applied edit that clobbered a route decorator would let the agent serve `/health` 200 while silently 404'ing `/bootstrap`, masking a broken agent as "Running" — a missing load-bearing route now crashes startup loudly
- **Accounts is the project registry**: Accounts mints conversation/project ids; the agent's own `POST`/`GET /v1/conversations` are legacy/unused. New chat threads arrive with an accounts-minted `conversation_id`, and `/v1/chat` materialises a local chat-layer record via `conversations.ensure()`
- **Persistent conversations**: `conversations.py` stores each conversation as one JSON file (name, message history, commentary audit trail, SDK session id). `/v1/chat` persists user+assistant turns and resumes the SDK session from disk, so chat survives an agent restart
- **No `AskUserQuestion` tool**: It's the Claude Code harness's structured-prompt tool with no UI handler in the Datafye workspace, so the model's question would silently vanish. Dropped from `INTERNAL_TOOLS`; the model asks inline in chat text instead
- **Strategy = folder**: each conversation/strategy is a directory under `<state>/strategies/<id>/` holding `meta.json` + scaffolded `CLAUDE.md` (per-strategy memory), `PROJECT.md` (plain-language strategy narrative), `memory/`, and `.claude/skills/`. That folder is the chat turn's **cwd/workspace**, so the strategy's code, memory, and skills live together and survive a restart. `conversations.ensure()` materialises the folder for an accounts-minted id; legacy `<id>.json` records migrate into folders on load
- **Skills, three tiers**: the native `Skill` tool is enabled, with skills discovered from local plugins + project source. **System** skills ship read-only in `plugins/datafye` (installer/app-clone managed); **user-global** skills the agent authors into `<state>/plugins/user`; **per-strategy** skills in the strategy's `.claude/skills` (loaded via `setting_sources=["project"]`). The `author-skill` system skill teaches scope-aware authoring. Listing via `GET /v1/skills`; execution is chat-driven. We keep the engine-native mechanism for quality (Claude is post-trained for it) — the `SKILL.md` artifacts are engine-portable if we ever hand-roll the loader for another engine
- **Convention-based memory (one model)**: durable facts are plain markdown the agent writes/reads, guided by a protocol in the system prompt — **global** (cross-strategy, `<state>/memory` + `CLAUDE.md`) and **per-strategy** (in the strategy folder). Only the always-on `MEMORY.md` indexes + the small `CLAUDE.md` notes are injected; memory bodies are read on demand. The CLI's own auto-memory is **disabled** (`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`) because it has no global tier, is git-repo-scoped, and stores under `~/.claude` — it would be a second, uncontrolled store
- **Python-only algos**: No SDK/Java algos - all strategies are pure Python using REST/WebSocket APIs
- **Conversational config**: Datasets, schemas, and environments are configured through chat, not forms

## Git Commits

Do not include `Co-Authored-By` trailers in commit messages.
