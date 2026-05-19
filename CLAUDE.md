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
├── prompt.py        # System prompt builder (assembled from runtime context)
├── requirements.txt # Python dependencies
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
| `standalone` | AWS Marketplace / DIY | First-boot script only. Downloads and installs everything on first boot from user data. Includes nginx + SSL. |

### Installer

The version is baked into `install.sh` by `publish_installer.sh` — no `--version` flag needed.

```bash
# Hosted mode (Rumi cloud sandbox)
sudo ./install.sh --mode hosted

# Standalone mode (marketplace)
sudo ./install.sh --mode standalone --dns agent.mycompany.com --anthropic-key sk-ant-...

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
| `/bootstrap` | POST | Accounts-only. Bootstrap the agent's identity + credentials-store key from an accounts-signed JWT (`Authorization: Bearer`, `purpose=agent-bootstrap`). Idempotent for the same user; 409 on rebind |
| `/v1/chat` | POST | SSE streaming chat with agent. JWT-protected; 503 if no Anthropic key, 502 if invalid |
| `/v1/credentials` | POST | REMOVED — returns 410 Gone; credential writes go through the accounts service |
| `/v1/credentials/update` | POST | Accounts-only. Push a single credential `{provider, value}` into the encrypted store; 204 |
| `/v1/credentials/status` | GET | Check which credentials are configured (JWT-protected) |
| `/v1/broker/brokers` | GET | List brokers Datafye supports (StocksBroker enum) |
| `/v1/broker/connections` | GET | List the user's brokerage connections with linked accounts |
| `/v1/broker/connections` | POST | Create a ConnectTrade OAuth URL for a chosen broker; body `{type, broker}` |
| `/v1/broker/connections/{id}` | DELETE | Revoke a brokerage connection |

Every endpoint except `/health` and `/bootstrap` is gated by the
`require_bootstrapped` dependency and returns 503 until the accounts bootstrap
push lands.

## SSE Event Types

| Event | Description |
|-------|-------------|
| `init` | Session initialized |
| `content` | Text content chunk |
| `thinking` | Agent reasoning |
| `tool_use_start` | Tool invocation started |
| `tool_result` | Tool execution result |
| `result` | Final result with metadata |
| `descriptor` | Raw deployment-descriptor YAML text (relayed to accounts) |
| `env_status` | Environment state change (for frontend) |
| `scorecard_update` | Test results (for frontend) |
| `chart_data` | Chart data push (for frontend) |
| `error` | Error occurred |
| `done` | Stream complete |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DATAFYE_AGENT_ANTHROPIC_API_KEY` | - | Local-dev seed only. The Anthropic key is a *credential* — in production it lives in the encrypted credentials store and is delivered by accounts via `/v1/credentials/update`. This env var only seeds the store the first time it is created |
| `DATAFYE_AGENT_MODEL` | `opus` | Claude model |
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
| `DATAFYE_AGENT_DEPLOYMENT_API_URL` | `http://local-foundry-dev-api.datafye.local:7776` | Datafye deployment REST API base URL — read after a chat turn to derive `env_status` from the deployment descriptor |

## Key Design Decisions

- **Native execution**: Agent runs directly on the host (not in Docker) because it needs to manage Docker containers for Datafye environments
- **Per-user instances**: Each user gets their own agent process (not shared)
- **Open source agent**: Agent source is public on GitHub — the value is in the Datafye platform, not the glue code
- **Local docs over MCP**: Datafye docs are on disk, not via a docs MCP server - faster and more reliable
- **Push bootstrap**: The agent learns its identity and its credentials-store encryption key from an accounts-signed JWT pushed to `POST /bootstrap` — it never reads AWS instance metadata. Accounts is the only writer in the relationship
- **Anthropic key as a credential**: The Anthropic key is not a startup env var; it lives in the encrypted credentials store and is delivered via the credentials push channel. The agent starts and stays manageable with no key — chat just returns 503/502 until a valid key arrives
- **Credentials via accounts**: The accounts service pushes credential updates to `/v1/credentials/update`; the old direct-write `/v1/credentials` endpoint is gone
- **Python-only algos**: No SDK/Java algos - all strategies are pure Python using REST/WebSocket APIs
- **Conversational config**: Datasets, schemas, and environments are configured through chat, not forms

## Git Commits

Do not include `Co-Authored-By` trailers in commit messages.
