# Datafye Agent

A per-user AI backend for quantitative trading development on the [Datafye platform](https://docs.datafye.io). Wraps the [Claude Agent SDK](https://docs.claude.com/claude-code) in a FastAPI service, giving each user their own agent session that can **research market data, build trading signals and algos, package them into apps, and deploy and run all of it on Datafye** — end-to-end through natural-language conversation.

The agent is equipped with a purpose-built toolkit that turns Claude into a capable quant developer: Datafye's documentation, CLI, and data/trading APIs; general-purpose software tools (file system, code execution, git, shells); and third-party integrations for market data, brokers, and anything else a quant workflow needs. New tools can be added as MCP servers without changing the agent itself.

This repository contains the **agent backend only**. It is the glue code that connects a Claude-powered conversational agent to the Datafye platform — the value lives in the platform, not the glue. It is open source so you can self-host, fork, extend, or audit it.

> ⚠️ **Alpha.** This is pre-release software under active development. Install flows, configuration, and the API surface will change without notice. Not suitable for production use. Bug reports and feedback are very welcome — file them in this repo's [Issues](../../issues).

## How the pieces fit

The Datafye App at [developer.datafye.io](https://developer.datafye.io) is a **static frontend** (HTML/CSS/JS, no backend of its own). Your browser connects **directly** to the agent over HTTP + SSE — there is no intermediary server. This is true whether you're on a Datafye-managed sandbox or your own self-hosted agent: chat messages, credentials, and broker traffic never flow through any Datafye-operated app tier. The only server-side pieces Datafye runs for you are (a) serving the static app and (b) in the hosted model, the accounts service that provisions your sandbox. Once the sandbox is up, your browser talks straight to it.

```
Browser ──HTTP + SSE──▶ Your agent (sandbox or self-hosted) ──▶ ConnectTrade, Anthropic, etc.
```

## Status

**Working today**
- Native install on Linux (hosted and standalone modes) with systemd service management
- Automatic upgrades via cron, or pin to a specific version with `--version`
- SSE-streamed chat against the agent, JWT-authenticated — the agent verifies tokens against the accounts service's JWKS and checks the subject matches its own identity
- Sandbox lifecycle (provision / start / stop / idle detection) driven by the Datafye accounts service
- Credentials pushed from the accounts service; the agent is a write-through cache, never written to directly by the browser
- Local docs + CLI + samples integration so the agent can research, build, test, and deploy
- Static `developer.datafye.io` frontend connects directly to the agent backend (hosted sandbox or self-hosted), no Datafye-operated app tier in the middle

**Not yet wired up**
- TradingView Lightweight Charts integration for the Test mode dashboard
- Free-tier usage metering for `developer.datafye.io` hosted sandboxes

## Three ways to run it

The Datafye platform is free for everything up to live trading — research, signal development, algo development, backtesting, and paper trading all cost nothing. **Live trading is the sole paid tier and runs exclusively on Datafye's cloud.** The agent itself is always free and open source.

| | developer.datafye.io (free tier) | developer.datafye.io (paid) | Self-hosted |
|---|---|---|---|
| **Who runs the agent** | Datafye, in a managed sandbox | Datafye, in a managed sandbox | You, on your own infra |
| **Anthropic key** | Datafye provides | Datafye provides | You bring your own |
| **Usage limit** | Capped by time and token quotas — whichever hits first (TBD) | None (within reason) | None — you pay Anthropic directly |
| **Price** | Free | Subscription | Free (agent is Apache 2.0) |
| **Frontend** | `developer.datafye.io` (static, direct to your sandbox agent) | `developer.datafye.io` (static, direct to your sandbox agent) | `developer.datafye.io` pointed at your agent URL, or call the API directly |

When you exhaust the free hours on `developer.datafye.io`, you have two graceful options: upgrade to a paid Datafye tier, or point `developer.datafye.io` at your own self-hosted backend and keep going for free. The custom-backend-URL setting in Settings is the graduation path.

## Self-hosted install

### AWS (Marketplace AMI)

Launch the Datafye Agent AMI from AWS Marketplace. Supply your Anthropic API key and DNS name as EC2 user data. The first boot script installs everything, provisions SSL via Let's Encrypt, and starts the agent.

### Linux (any cloud or bare metal)

```bash
curl -fsSL https://downloads.n5corp.com/datafye/agent/latest/install.sh -o install.sh
sudo ./install.sh --mode standalone \
    --dns agent.mycompany.com \
    --anthropic-key sk-ant-...
```

Requirements: a clean Linux host with `systemd` and root access, plus a public DNS name pointing to the machine. The installer supports Amazon Linux, Ubuntu, Debian, RHEL, CentOS, Fedora, Rocky, and AlmaLinux.

The installer:
- Creates a dedicated `datafye` user and workspace
- Installs Python 3.11, Java 17, Docker (with the Compose plugin), Maven, and the Claude Code CLI (installed for the `datafye` user)
- Installs the Datafye CLI, docs, samples, and the agent source
- Sets up a systemd service (`datafye-agent.service`) on port 18780
- Configures nginx as a reverse proxy with automatic Let's Encrypt SSL
- Sets up a 5-minute auto-upgrade cron

See [`install/install_template.sh`](install/install_template.sh) for all flags.

### Updating

Updates are automatic — a cron job polls `downloads.n5corp.com/datafye/agent/latest/version.txt` every 5 minutes and re-runs the installer when a new version is published. Credentials, mode, DNS, and port are preserved across upgrades.

## Connecting a frontend

### Option 1 — Use developer.datafye.io

Sign in at [developer.datafye.io](https://developer.datafye.io), open **Settings → Agent Backend**, and enter your agent URL (e.g. `https://agent.mycompany.com`). The frontend is static HTML/JS and SSEs directly to the URL you set — there is no Datafye-operated app backend in the path, so chat messages, credentials, and broker traffic flow only between your browser and your agent.

### Option 2 — API directly

The agent exposes an HTTP + SSE API. Useful if you want to wire it into your own tooling:

| Endpoint | Method | Description |
|---|---|---|
| `/health` | GET | Health check with credential + sandbox activity status |
| `/v1/chat` | POST | SSE streaming chat with the agent (JWT required) |
| `/v1/broker/*` | GET / POST / DELETE | Broker (ConnectTrade) connection management (JWT required) |
| `/v1/credentials/status` | GET | Check which credentials are configured (JWT required) |
| `/v1/credentials/update` | POST | Single-credential push from the accounts service |

Chat and broker endpoints require an `Authorization: Bearer <jwt>` header
(the accounts service issues the token). In the hosted model the accounts
service writes credentials via `/v1/credentials/update`; the browser never
writes them directly. See [`CLAUDE.md`](CLAUDE.md) for the full list of SSE
event types.

## Configuration

Runtime configuration lives in `/opt/datafye/agent/agent.env` (generated by the installer). The most commonly set values:

| Variable | Description |
|---|---|
| `DATAFYE_AGENT_ANTHROPIC_API_KEY` | Required. Your Anthropic API key. |
| `DATAFYE_AGENT_MODEL` | Claude model, defaults to `opus`. |
| `DATAFYE_AGENT_PORT` | HTTP port, defaults to `18780`. |
| `DATAFYE_AGENT_MASSIVE_API_KEY` | Optional. Massive (Polygon) market data key. |
| `DATAFYE_AGENT_CONNECTTRADE_CLIENT_ID` | Optional. ConnectTrade broker credentials (4 values). |

For a self-hosted agent, set provider credentials in `agent.env`. In the
hosted model the accounts service pushes them to the agent at runtime
(driven by the frontend Settings panel) — no restart needed either way.

Full reference: [`CLAUDE.md`](CLAUDE.md).

## Local development

```bash
pip install -r requirements.txt

export DATAFYE_AGENT_ANTHROPIC_API_KEY="sk-ant-..."
export DATAFYE_AGENT_DOCS_DIR="/path/to/datafye-docs"
export DATAFYE_AGENT_CLI_PATH="/path/to/datafye"
export DATAFYE_AGENT_WORKSPACE="/tmp/datafye-workspace"
export DATAFYE_AGENT_SAMPLES_DIR="/path/to/datafye-samples"

python main.py
```

Service starts on port `18780`. Try `curl http://localhost:18780/health` to verify.

## Architecture

See [`PROJECT.md`](PROJECT.md) for the architecture rationale, design decisions, and pitfalls — written in plain language, not boilerplate documentation.

## License

Licensed under the [Apache License, Version 2.0](LICENSE).
