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

**ConnectTrade credentials are a special case.** Most credentials are symmetric — one key, one user, done. ConnectTrade has two layers: a *client* identity (who Datafye is, as a ConnectTrade tenant) and a *user* identity (who this particular sandbox's human owner is inside that tenant). They have very different lifetimes:

- **Client creds** (`client_id` / `client_secret`): today these come from env vars (`DATAFYE_AGENT_CONNECTTRADE_CLIENT_ID/_SECRET`). They're the same for every sandbox. TODO: fetch them from the Datafye accounts-manager so we don't have to bake them into AMIs or distribute them through env files.
- **User creds** (`user_id` / `user_secret`): lazy-provisioned on the first `POST /v1/broker/connections` call by hitting ConnectTrade's `POST /users`, then persisted to `~/.datafye/agent/broker_user.json` (mode `0600`). A single file is fine here because every sandbox is single-user by design — there's nobody else to collide with. TODO: migrate this into accounts-manager per-user storage so a user who blows away their sandbox and gets a new one doesn't end up with an orphaned ConnectTrade user.

The broker module binds the same shared `credentials` dict that the rest of the service uses, so env-provided creds, runtime `/v1/credentials` updates, and lazy-provisioned user creds all converge in one place.

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

## Broker Integration

Eventually a strategy has to touch real money — or at least money-shaped money (paper trading). That means connecting the user's brokerage account. We don't want to be in the business of collecting and storing broker OAuth tokens ourselves, so we punt that to **ConnectTrade**, a broker-aggregator that speaks a single API and handles the OAuth dance for every supported broker.

The agent exposes four endpoints under `/v1/broker/*`:

- `GET /v1/broker/brokers` — returns the list of brokers Datafye supports. This mirrors the `StocksBroker` enum in `datafye-roe` (ALPACA, LIGHTSPEED, TASTYTRADE, TRADESTATION, TRADEZERO, WEBULL) so the frontend and the trading engine can't drift apart.
- `GET /v1/broker/connections` — proxies ConnectTrade's `GET /connections` and flattens the response to `{connection_id, broker, status, type, accounts}` so the frontend doesn't have to know ConnectTrade's wire format.
- `POST /v1/broker/connections` — takes `{type, broker}`, validates the broker, and calls ConnectTrade's `POST /connections` with the broker pre-selected and our `redirect_url` set. ConnectTrade returns a `connection_request_url`; we remap that to `authorization_url` and hand it back to the caller.
- `DELETE /v1/broker/connections/{id}` — proxies the delete.

### Who opens the popup?

A subtle design point: the agent only *produces* the OAuth URL. It does **not** open a browser window. Two reasons — a backend process literally can't open a browser window on the user's machine, and even if it could, browsers only allow popups in response to a user gesture (a click). So the flow is: frontend clicks "Connect Alpaca" → calls `POST /v1/broker/connections` → gets back `authorization_url` → opens the popup itself. The agent's job ends at producing the URL.

### Data flow

```
frontend                agent                        ConnectTrade
   │  POST /v1/broker/connections
   │ ───────────────────▶
   │                        POST /connections
   │                        (with client+user creds,
   │                         broker, redirect_url)
   │                       ──────────────────────────▶
   │                       ◀──────────────────────────
   │                        connection_request_url
   │ ◀───────────────────
   │  authorization_url
   │
   │  window.open(authorization_url)  [user gesture]
   ▼
(user completes OAuth on ConnectTrade's hosted UI)
```

The default `redirect_url` is `https://developer.datafye.io/broker-callback.html` — a static page that just signals the parent window that OAuth finished.

We use `permission_mode="bypassPermissions"` which means the agent can execute anything. This is necessary for CLI operations and Python execution, but it means:
- A malicious prompt could potentially access the host system
- The per-user isolation is critical — each user gets their own EC2 instance in a Rumi private cloud

### Session Memory Has Limits

The Claude Agent SDK session stores conversation history, but there's a context window limit. Long algo development sessions might hit it. The SDK handles this with automatic context compression, but be aware that very early conversation context might get summarized or dropped.

### Credential Rotation

If a user updates their API key in Settings while an environment is running with the old key, the environment won't automatically pick up the new key. The agent would need to re-provision. This is a known edge case.

### ConnectTrade Gotchas (Learned the Hard Way)

Two surprises from wiring up the broker module that are worth writing down so the next person doesn't spend a Saturday on them:

1. **`user_secret` can go stale.** We observed a case where a previously-working `user_secret` stopped authenticating — ConnectTrade would reject it with an auth error even though nothing on our side had changed. The recovery path is: call ConnectTrade's `rotate-secret` endpoint to mint a fresh secret, overwrite the one in `broker_user.json`, and retry. The agent doesn't do this automatically yet; if you see auth failures on a previously-working sandbox, rotate first, blame yourself second. (Moving user-secret storage into the accounts-manager will let us rotate once and fan out to every sandbox the user owns, instead of per-sandbox manual fixes.)
2. **A brokerage account can only be linked to one ConnectTrade user per tenant.** If `POST /connections` comes back with a 409, the instinct is "my request is malformed." It usually isn't. It means another ConnectTrade user in the Datafye tenant has already linked that same Alpaca (or Lightspeed, or whatever) account. This shows up in practice when a developer tests with their personal Alpaca account across multiple sandbox users — the second sandbox gets a 409 that looks like a code bug but is actually ConnectTrade doing the right thing. Surface this to the user as "this brokerage account is already linked to another Datafye user," not as a generic error.

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
- **Migrate ConnectTrade creds to accounts-manager**: Today the client `id`/`secret` come from env vars and the user `id`/`secret` are persisted to a per-sandbox JSON file. Both should live in the Datafye accounts-manager — client creds as a tenant-wide secret, user creds as per-user storage that survives sandbox recreation.
- **SDK-based algos**: Java/Datafye SDK path alongside Python
- **Live trading**: Currently capped at simulated trading (paper); live trading is a future capability
