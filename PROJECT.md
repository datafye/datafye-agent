# Datafye Agent

## What Is This?

This is the brain behind Datafye's AI-powered algo development experience. When a user sits down in the Datafye Agent App (the frontend) and says "I want to build a strategy that buys stocks when their 10-day moving average crosses above the 50-day," this backend is what turns that sentence into a working, testable trading algorithm.

Think of it as giving every algo developer their own personal quant assistant — one that knows the Datafye platform inside out, can spin up data environments, write Python code, test strategies against historical data, and set up simulated trading. All through conversation.

## How It Works

### The Big Picture

```
User (browser)
    ↓ chat message
Datafye Agent App (frontend)
    ↓ POST /v1/chat (SSE)
Datafye Agent (this service)
    ↓ Claude Agent SDK
Claude (Anthropic)
    ↓ tool calls
Local Machine:
  ├── Datafye Docs (on disk)
  ├── Datafye CLI (foundry, trading, data)
  ├── User's Python algo code (workspace)
  └── GitHub (algo repos)
```

Each user gets their own instance of this service. That's not an accident — algo development is stateful. The agent needs to remember what you're building, what environment is running, what you tried last. A shared service would be a concurrency nightmare and a security risk (user A's broker credentials leaking to user B).

### The Claude Agent SDK

At the heart of this service is the Claude Agent SDK's `query()` function. It's an async generator that yields a stream of messages as Claude thinks, calls tools, and generates responses. We wrap this in a FastAPI SSE endpoint that the frontend consumes.

The SDK gives Claude access to tools — file operations (Read, Write, Edit), shell execution (Bash), search (Glob, Grep), and planning tools. We add the Datafye-specific capabilities through the system prompt and the Bash tool: the agent can run `datafye foundry local provision`, `curl` the REST API, execute Python scripts, and manage git repos.

### The System Prompt

The system prompt (in `prompt.py`) is assembled at runtime from the current state:
- Where the docs are on disk
- Where the CLI is
- What credentials the user has configured
- Which algo they're currently working on

This means the agent always knows what it can and can't do. If the user hasn't configured their Massive (Polygon) API key, the agent knows it can't provision a SIP dataset and will tell the user to add their key in Settings rather than failing silently.

### Session Continuity

The Claude Agent SDK supports session resumption. When a user sends a message, we check if there's an existing session for their conversation. If so, we resume it — Claude remembers the entire conversation history, what files it created, what environments are running. This is critical for algo development where a single strategy might take dozens of back-and-forth exchanges to refine.

### Credentials Management

User credentials (data provider API keys, broker credentials) flow through in two ways:

1. **At launch**: Environment variables set when the user's instance starts
2. **At runtime**: The frontend's Settings modal calls `POST /v1/credentials` to update keys without restarting the service

The agent's system prompt is rebuilt on every chat request, so credential changes are immediately reflected in what the agent tells the user it can do.

## Architecture Decisions & Why

### Why Per-User Instances (Not Shared)?

Three reasons:
1. **Security**: Each user's broker credentials, API keys, and algo code live in their own process
2. **State isolation**: The agent's session, working directory, and environment are user-specific
3. **Resource control**: One user's heavy backtest doesn't starve another's chat

The tradeoff is operational complexity — you need orchestration to spin up/down instances. But for algo development with real financial credentials, isolation isn't optional.

### Why Local Docs Instead of an MCP Server?

The docchat backend uses a GitBook MCP server for documentation. We deliberately chose to put the docs on disk instead:
- **Faster**: No HTTP round-trips for every doc lookup
- **Reliable**: No dependency on GitBook's uptime
- **Complete**: The agent can Glob and Grep across the entire doc set, not just search by query

The docs are synced from the `datafye-docs` repo.

### Why Python-Only Algos (Not SDK/Java)?

The Datafye platform has a Java-based Algo SDK for high-performance, integrated strategies. We're not using it here because:
- The target user may not know Java
- Python is the lingua franca of quant finance
- Data Cloud Only mode (REST/WebSocket APIs) is more accessible
- SDK-based algos can be added later as a separate path

### Why Conversational Dataset Config (Not Forms)?

A dropdown can't capture "I want to use daily OHLC and EMA data for US tech stocks, plus some alternative sentiment data." The agent can. It understands intent, maps it to the right datasets and schemas, and provisions the environment — all in one conversation. This is particularly important for users who don't know what datasets exist or what schemas they need.

## The Algo Development Flow

Here's what a typical session looks like from the agent's perspective:

1. **User describes idea**: "I want to build a mean-reversion strategy on AAPL and MSFT"
2. **Agent determines data needs**: SIP dataset, ohlc-1d and sma-1d schemas, symbols AAPL and MSFT
3. **Agent checks credentials**: Massive API key configured? Yes → proceed. No → "Please add your Massive API key in Settings"
4. **Agent builds descriptor**: Creates a YAML deployment descriptor
5. **Agent provisions environment**: Runs `datafye foundry local provision -x descriptor.yaml`
6. **Agent writes algo code**: Creates Python files in the workspace
7. **Agent tests**: Downloads historical data, runs the algo, collects results
8. **Agent presents results**: Returns, win rate, trade count — the frontend shows these in the scorecard and charts
9. **Iteration**: User says "try a shorter lookback period" → agent modifies and re-tests
10. **Simulated trading**: If broker is configured, agent provisions a trading environment

## Potential Pitfalls

### The Agent Can Run Arbitrary Bash Commands

We use `permission_mode="bypassPermissions"` which means the agent can execute anything. This is necessary for CLI operations and Python execution, but it means:
- A malicious prompt could potentially access the host system
- The per-user isolation is critical — each user gets their own EC2 instance in a Rumi private cloud

### Session Memory Has Limits

The Claude Agent SDK session stores conversation history, but there's a context window limit. Long algo development sessions might hit it. The SDK handles this with automatic context compression, but be aware that very early conversation context might get summarized or dropped.

### Credential Rotation

If a user updates their API key in Settings while an environment is running with the old key, the environment won't automatically pick up the new key. The agent would need to re-provision. This is a known edge case.

## Deployment

The agent runs **natively** on the host (not in a Docker container). This was a deliberate decision — the agent uses the Datafye CLI to spin up Datafye environment containers via Docker, and Docker-in-Docker is painful. Since the whole instance is dedicated to one user, there's no isolation benefit from containerizing the agent. The AMI is the packaging.

Two deployment modes:
- **Hosted**: Pre-baked AMI in a Rumi private cloud. Each user gets a sandbox instance at `{username}.app.datafye.io`, proxied through a jump server with wildcard SSL. Managed by the datafye-accounts service (elastic stop/start based on activity).
- **Standalone (Marketplace)**: Minimal AMI with a first-boot script. User provides their Anthropic key and DNS via EC2 user data. Everything downloads and installs on first boot.

The agent source is open source — the value is in the Datafye platform, not the glue code. Power users can fork and customize the prompt, add tools, tweak behavior.

## What's Next

- **Wire to accounts service**: Sandbox provisioning, ensure endpoint, elastic idle management
- **Authentication**: JWT validation from the frontend (SSO with developer.datafye.io)
- **Health endpoint for idle detection**: Report `lastChatActivityAt`, `runningJobs`, active proxied apps
- **SDK-based algos**: Java/Datafye SDK path alongside Python
- **Live trading**: Currently capped at simulated trading (paper); live trading is a future capability
