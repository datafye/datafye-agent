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
├── CLAUDE.md        # This file
└── PROJECT.md       # Detailed project documentation
```

## Running Locally

```bash
# Install dependencies
pip install -r requirements.txt

# Required environment variables
export ANTHROPIC_API_KEY="sk-ant-..."
export DATAFYE_DOCS_DIR="/path/to/datafye-docs"
export DATAFYE_CLI_PATH="/path/to/datafye"
export DATAFYE_AGENT_WORKSPACE="/path/to/workspace"

# Optional: user credentials
export MASSIVE_API_KEY="..."
export CONNECTTRADE_CLIENT_ID="..."
# ... etc

# Run
python main.py
```

Service starts on port 18780 by default (`DATAFYE_AGENT_PORT`).

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check with credential status |
| `/v1/chat` | POST | SSE streaming chat with agent |
| `/v1/credentials` | POST | Update user credentials at runtime |
| `/v1/credentials/status` | GET | Check which credentials are configured |

## SSE Event Types

| Event | Description |
|-------|-------------|
| `init` | Session initialized |
| `content` | Text content chunk |
| `thinking` | Agent reasoning |
| `tool_use_start` | Tool invocation started |
| `tool_result` | Tool execution result |
| `result` | Final result with metadata |
| `env_status` | Environment state change (for frontend) |
| `scorecard_update` | Test results (for frontend) |
| `chart_data` | Chart data push (for frontend) |
| `error` | Error occurred |
| `done` | Stream complete |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | Required | Anthropic API key |
| `DATAFYE_AGENT_MODEL` | `opus` | Claude model |
| `DATAFYE_AGENT_PORT` | `18780` | HTTP port |
| `DATAFYE_AGENT_WORKSPACE` | `/home/datafye/workspace` | User workspace directory |
| `DATAFYE_DOCS_DIR` | `/home/datafye/docs` | Path to Datafye docs |
| `DATAFYE_CLI_PATH` | `datafye` | Path to Datafye CLI |
| `DATAFYE_SAMPLES_DIR` | `/home/datafye/samples` | Path to datafye-samples (API reference) |
| `DATAFYE_AGENT_ALLOWED_ORIGINS` | `*` | CORS origins |
| `MASSIVE_API_KEY` | - | Massive (Polygon) API key |
| `PALPHA_API_KEY` | - | Precision Alpha API key |
| `HWAI_API_KEY` | - | HWAI API key |
| `CONNECTTRADE_CLIENT_ID` | - | ConnectTrade client ID |
| `CONNECTTRADE_CLIENT_SECRET` | - | ConnectTrade client secret |
| `CONNECTTRADE_USER_ID` | - | ConnectTrade user ID |
| `CONNECTTRADE_USER_SECRET` | - | ConnectTrade user secret |
| `GITHUB_USER` | - | Personal GitHub username (optional) |
| `GITHUB_TOKEN` | - | Personal GitHub token (optional) |
| `GITHUB_ORG` | `datafye` | GitHub org for algo repos |

## Key Design Decisions

- **Per-user instances**: Each user gets their own agent process (not shared)
- **Local docs over MCP**: Datafye docs are on disk, not via a docs MCP server - faster and more reliable
- **Credentials at runtime**: Frontend can update credentials via `/v1/credentials` without restarting
- **Python-only algos**: No SDK/Java algos - all strategies are pure Python using REST/WebSocket APIs
- **Conversational config**: Datasets, schemas, and environments are configured through chat, not forms

## Git Commits

Do not include `Co-Authored-By` trailers in commit messages.
