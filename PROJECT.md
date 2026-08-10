# Datafye Agent

> **Tracking:** Datafye open work lives in **Linear** (GTM project, team Datafye: https://linear.app/datafye). The `gtm/datafye/TODO.md` file is retired — moved to Linear 2026-06-24.

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

The SDK gives Claude access to tools — file operations (Read, Write, Edit), shell execution (Bash), search (Glob, Grep), and planning tools. We add the Datafye-specific capabilities through the system prompt and the Bash tool: the agent can run `datafye foundry local provision`, `curl` the REST API, execute Python scripts, and manage git repos. The exact allowed set lives in `INTERNAL_TOOLS` in `main.py`.

**One tool we deliberately removed: `AskUserQuestion`.** It's the Claude Code harness's *structured-prompt* tool — in the Claude Code CLI it renders an interactive multiple-choice question. The Datafye workspace has no UI handler for it, so when the model reached for it, the question simply vanished into the void — the user saw the agent go quiet instead of being asked anything. Dropping it from `INTERNAL_TOOLS` forces the model to fall back to asking its question inline as ordinary chat text, which the frontend already renders. The broader lesson: an agent's tool list has to match the *surface it's actually running on*, not the SDK's full menu — a tool with no handler is worse than no tool, because it fails silently. For the same reason we also dropped the harness-only `Task` family (`Task`/`TaskCreate`/`TaskUpdate`/`TaskList`/`TaskGet`/`TaskStop`/`TaskOutput`) from `INTERNAL_TOOLS` — they have no backend handler in this agent, so offering them only invites the model to call into a dead end. That was the original reason. A second and stronger one turned up later, when we looked at what a subagent's output actually does to a user-facing surface; it's written up in [A Subagent Is Not the Agent](#a-subagent-is-not-the-agent-dat-138) below, and it's why `Task` stays out even if a handler ever appeared.

### The Anthropic Key Is a Credential, Not a Setting

It would be tempting to treat the Anthropic API key as plumbing — a startup env var, set once, assumed present. We deliberately don't. The key is just another **credential**: it lives in the same encrypted credentials store as the user's data-provider keys, under `anthropic_api_key`, and it arrives through the same accounts push channel as everything else.

The Claude Agent SDK runs Claude in a subprocess and that subprocess reads `ANTHROPIC_API_KEY` from the environment. So whenever the key changes — at bootstrap, or on a later credentials push — `_apply_anthropic_key()` syncs the stored value into `os.environ["ANTHROPIC_API_KEY"]` and then validates it against the Anthropic API. The agent tracks `anthropic_key_status` — one of `missing`, `ok`, `invalid`, or `unvalidated` (a network blip the agent treats as a soft pass) — and reports it on `/health`.

The payoff is that the agent **starts and stays manageable with no Anthropic key at all**. It can be bootstrapped, accept credential pushes, answer health probes — everything except chat. `/v1/chat` returns 503 when the key is missing and 502 when it's invalid, so the frontend can show a precise "add an Anthropic key" message instead of the agent failing to boot or crashing mid-stream. This matters because a sandbox might be provisioned before the user has chosen a billing plan or entered a bring-your-own key.

### The System Prompt

The system prompt (in `prompt.py`) is assembled at runtime from the current state:
- Where the docs are on disk
- Where the CLI is
- What credentials the user has configured
- Which algo they're currently working on

This means the agent always knows what it can and can't do. If the user hasn't configured their Massive (Polygon) API key, the agent knows it can't provision a SIP dataset and will tell the user to add their key in Settings rather than failing silently.

### Session Continuity

The Claude Agent SDK supports session resumption. When a user sends a message, we check if there's an existing session for their conversation. If so, we resume it — Claude remembers the entire conversation history, what files it created, what environments are running. This is critical for algo development where a single strategy might take dozens of back-and-forth exchanges to refine.

Originally that session-id lookup was an in-memory dict, which meant a restarted agent forgot every session — a fresh sandbox boot would start every conversation from scratch even though the SDK's own transcript was still on disk. Now the SDK session id is persisted (see [Conversations: Projects That Survive a Restart](#conversations-projects-that-survive-a-restart)), so `/v1/chat` resumes the right SDK session even across a process restart. The in-memory dict is kept only as a fallback for conversations that have no on-disk record.

### Conversations: Projects That Survive a Restart

> **Superseded** — a conversation is now a *folder*, not a single JSON file. See [Strategies, Skills, and Memory](#strategies-skills-and-memory) below. The `ensure()`/`create()` identity story in this section still holds; only the on-disk shape changed (`<state>/strategies/<id>/meta.json` plus scaffolded files, with legacy `<id>.json` records migrated in on load).

What the frontend calls a **project** the agent calls a **conversation**, and `conversations.py` is its little on-disk database — one JSON file per conversation under `~/.datafye/agent/conversations/<id>.json`. Each record holds the human-readable name, the message history (user + assistant turns), a *commentary* log (the audit trail of background activity — see below), and the SDK session id.

Three design choices are worth calling out:

- **Plain JSON, not the encrypted store.** Conversation content isn't a secret key, and the Claude Agent SDK already writes its own transcripts to disk unencrypted — encrypting an *index* of those would buy nothing. Files are mode `0600` and written via temp-file-plus-atomic-rename, so a crash mid-write can't truncate an existing file. (Contrast with `credentials.bin`, which *is* encrypted because it holds Fernet-protected secrets.)
- **`ensure()` vs `create()` — who mints the id.** This is the load-bearing distinction. The accounts service is now the authoritative project registry: it mints project ids and the frontend creates/lists projects against accounts, not the agent. So a chat turn arrives with an *accounts-minted* `conversation_id` the agent has never seen. `conversations.ensure(id)` lazily materialises a local record for that exact id (never minting its own), which is what makes `append_message`/`append_commentary` actually persist — those helpers no-op when no file exists, so `/v1/chat` calls `ensure()` first. The agent's own `create()` (and the `POST`/`GET /v1/conversations` endpoints that use it) are now **legacy/unused** — left in place because they're harmless, but no longer on the frontend's path.
- **No per-user namespacing or locking.** The agent serves exactly one user, so there's nobody to collide with; the atomic rename is the only concurrency control needed.

The lesson here is a recurring one in this codebase: **decide who owns identity, then make every other component a follower.** Just as accounts owns the agent's *identity* (bootstrap push) and its *credentials* (credentials push), it now owns the *project registry* too. The agent's job is to materialise local state for ids it's handed, never to invent them — which keeps the agent and accounts from drifting into two competing lists of "what projects exist."

## Strategies, Skills, and Memory

Three capabilities turn the agent from a stateless chat endpoint into a workspace that *remembers* and *learns*. They share one idea — **progressive disclosure**: keep a tiny always-on index in context, fetch the detail on demand.

### A strategy is a folder

What the frontend calls a project, the agent stores as a **directory** — `<state>/strategies/<id>/` — and that directory *is* the working directory for the strategy's chat turns. Inside live the algo's code, a `meta.json` (history + SDK session id), a `CLAUDE.md` (the agent's concise working memory for this strategy, auto-loaded by the engine as project memory), a `PROJECT.md` (a plain-language story of the strategy for the user), a `memory/` folder, and a `.claude/skills/` folder. Everything about one strategy sits in one place and survives a restart. `conversations.ensure(id)` materialises the folder for an accounts-minted id; old single-file conversations migrate into folders on first load. The lesson echoes the rest of the codebase: decide who owns identity (accounts mints the id), then make every other component a follower — the agent just creates a folder for whatever id it's handed.

### Skills in three tiers

The agent has *skills* — named, reusable procedures the model invokes via the native `Skill` tool — owned at three levels. **System** skills ship read-only inside the repo (`plugins/datafye`) and upgrade with the agent. **User-global** skills the agent authors for the user and reuses across every strategy. **Per-strategy** skills live in a strategy's `.claude/skills`. The first two are delivered as local plugins (`--plugin-dir`); the third via the engine's `project` setting-source. A meta-skill, `author-skill`, teaches the agent to write a new skill into the right scope, so "make me a skill that sets up my momentum strategy" just works; `GET /v1/skills` lists them, and running one is a normal chat turn.

We deliberately leaned on the engine's native skill machinery instead of hand-rolling it: Claude is post-trained to use the `Skill` tool well, and quality comes first. But the `SKILL.md` files are plain markdown — engine-agnostic *artifacts* — so if the agent ever runs on a different model engine, only the *loader* changes, not the skills. A wrinkle worth remembering: the `claude` CLI ships its own bundled developer skills (`update-config`, `loop`, …) that surface alongside ours, and the blunt switches to hide them (`--bare`, `--disable-slash-commands`) also kill *our* skills or the `Skill` tool itself — so we leave the bundled ones be (harmless for a developer audience).

### Memory that survives sessions

The agent keeps durable facts as plain markdown it writes and reads itself — no database, no special tool. Two scopes: **global** (cross-strategy: the user's preferences and reusable lessons) and **per-strategy**. The mechanism is pure convention, driven by a protocol in the system prompt: only the one-line `MEMORY.md` *indexes* (and the small `CLAUDE.md` notes) are always in context; the detailed memory files are read on demand when an index line looks relevant. That keeps the per-turn cost flat (~450 tokens, mostly the fixed protocol) even as the store grows.

The interesting decision was *not* to use the CLI's own auto-memory. It sounds like leaving sophistication on the table — until you look: auto-memory is scoped per-git-repository with **no global tier** (so a "2% stop on everything" preference learned in one strategy is invisible in the next), it stores under `~/.claude`, and its "recall" is just the first 200 lines of a `MEMORY.md` prefix — the *same* index-plus-on-demand shape we built. So we run one explicit model we control (`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`) that fits our two-scope, folder-per-strategy layout. The honest limitation: *whether* to remember and *what* to fetch are both model judgments guided by the protocol, not a guaranteed retrieval engine — a future RAG layer over the same markdown files is the planned upgrade for scale.

### Commentary: A Live Activity Feed

While Claude works, it calls a lot of tools — reading files, grepping, running shell commands, hitting the Datafye environment. Streaming every one of those to the user would be noise. So `_tool_commentary()` filters: only *meaningful background work* — `Bash` commands and MCP calls — becomes a human-readable commentary line ("Running: datafye foundry local provision ...", "Querying the Datafye environment"). File-level tools (Read/Edit/Grep) are too granular and are skipped.

Each commentary line is both emitted live as a `commentary` SSE event *and* appended to the conversation's on-disk commentary log, so the activity rail can be replayed when a user reopens a project (`GET /v1/conversations/{id}/history` returns both `messages` and `commentary`, each timestamped with `at` so the frontend can interleave them). We used to cap that log at 400 entries (`_MAX_COMMENTARY`); we removed the cap. The commentary trail is the *analytics record* accounts persists — a long session's early activity is exactly the part you don't want silently dropped — so it now grows unbounded.

The tool-label commentary entries also carry the **raw detail** behind each tool line, so the frontend's "Tool Detail" toggle can replay the exact command and output when a project is reopened — not just while a turn is live. At `tool_use_start` the entry is stamped with the `tool_id` + a formatted `command` (`_tool_command_text`, mirroring the frontend's own `toolCommandText`); at `tool_result`, `conversations.attach_tool_output` finds that entry by `tool_id` and attaches the output, **capped at 2000 characters** (with an `output_error` flag when the tool errored). None of this needs a new endpoint — it rides through `/history` on the commentary entries — and the accounts Conversation view reads the same fields. This reverses the earlier "live-only, never persisted" design: the persistence cost is bounded (2000 chars a tool), and being able to reopen a strategy and still see *what the agent actually ran* is worth it.

### Narration routing: two registers in one conversation

The agent narrates as it works ("Provisioning the SIP dataset…", "Running the backtest…") and then gives a final answer. Left alone, all of that text lands in the Conversation as one run-on wall — the play-by-play tangled up with the actual reply. We separate the two by watching the *shape* of the stream, and the frontend renders them as two registers of one conversation: the machine's activity in a dim per-turn **activity rail**, the reply at full weight below it.

The insight: a text burst that is **followed by a tool call** is almost always work-narration — Claude saying what it's about to do — whereas the **final trailing burst**, the one with no tool call after it, is the actual reply. So `/v1/chat` buffers unrouted text as a **list of distinct blocks** (`pending_blocks`) — deliberately *not* a single concatenated string. When a tool call arrives, each buffered block is flushed as **its own** commentary line at **`kind="narration"`** (`append_commentary(.., "narration")`) — a distinct kind from the machine tool-labels (`muted`/`notable`/`check`/`error`) so the frontend can render the agent's own voice a shade brighter than a canned "Working in the Datafye environment". A multi-sentence narration shows up as one rail line per sentence instead of a run-on blob with the sentences jammed together at the periods (the bug we got from string concatenation).

Whatever blocks are still buffered when the turn ends (the `ResultMessage` branch) are the reply: joined with `\n\n` into `conversation_text`, emitted as `content`, persisted as the assistant message. And a subtle guarantee: if a turn ends **on a tool call with no trailing prose**, `conversation_text` falls back to the SDK's `ResultMessage.result`, so the Conversation **always gets a closing message** — a turn never ends silently on an action (a tool call is not a reply). `prompt.py` cooperates: short high-level action lines while working (no commands/flags/filenames, no "Let me…" openers), a plain final message, and an explicit instruction to always close the turn with it. The lesson: you can route a stream by its *structure* (what follows what) without the model tagging each chunk — and a small server-side fallback is cheaper insurance than hoping the model never forgets to sign off.

### The live token ticker

While a turn runs, the frontend's status bar shows an ephemeral ticker — `<action> · <elapsed>s · <tokens> tokens` — that answers "is it alive, and what is this costing right now". Elapsed time the frontend can count on its own, but tokens it can't: the authoritative `usage` event only arrives at turn end. So the streamer emits a lightweight **`ticker` event once per model round**, read off each `AssistantMessage.usage`. What that number *means* has since changed twice over: it started as a running tally of fresh input+output tokens, and it now reports the conversation's **context size**, for reasons worth reading in [Attributing Cost Step by Step](#attributing-cost-step-by-step) below. The durable *thinking content* is a separate thing: it streams as the existing `thinking` event and renders as a collapsible line in the activity rail (and is persisted now too, see [Thinking, Made Visible and Made Safe](#thinking-made-visible-and-made-safe-dat-139)). The ephemeral ticker and the durable reasoning are deliberately kept apart — one is pinned to the status bar and thrown away, the other lives in the transcript.

### Surviving a Dropped Connection: Turns Run in the Background

Originally a chat turn *was* the HTTP response — the SDK loop streamed straight into the open `POST /v1/chat` connection. That coupling had a nasty consequence: if the client's connection dropped mid-turn (a flaky network, the laptop sleeping, the user switching to another project), the turn was cancelled and its work lost — and there was no way to reconnect to a turn already in flight.

So a turn now runs as a **background task, decoupled from the HTTP response**. `POST /v1/chat` mints a `turn_id`, starts the task, and returns a stream that simply *drains a buffer* the task fills (`_Turn`/`_turn_emit`/`_run_turn`/`_drain_turn`, tracked in a `_turns` registry). Every SSE frame is tagged with a monotonic `id: <seq>`, and the very first frame is a `turn` event carrying the `turn_id`. Two new endpoints make the buffer reconnectable:

- **`GET /v1/chat/resume?turn_id=&after=<seq>`** replays the buffered frames after `<seq>`, then continues live — so a client that dropped at seq 40 reconnects and picks up at 41 with no gap and no overlap. If the buffer has already been evicted (or the turn never existed), it 404s and the client falls back to reloading `/history`.
- **`POST /v1/chat/stop?turn_id=`** cancels the task; `_run_turn` `aclose()`s the SDK generator so `query()`'s subprocess dies promptly, then emits a `Stopped` + `done` so the turn ends cleanly.

The key semantic flip: **a dropped connection no longer cancels the turn.** That's *why* Stop had to become an explicit endpoint — the old "just close the socket" cancel no longer works. To bound abandoned (and still-billing) turns, a watchdog (`_turn_sweeper`) cancels a running turn with **no consumer for 300s** and evicts finished buffers after 120s. The 300s window is deliberately generous: the frontend *parks* an in-flight turn when the user switches projects (detaching without ending it) and resumes it on switch-back via `resume?after=-1`, so a parked turn has to outlive a detour. The buffer/drain/resume/stop algorithm is unit-tested (replay-from-offset with no gap/overlap, concurrent consumers, stop → `Stopped`+`done`+`aclose`); the live wire behaviour is a deploy-time check.

### Reflecting the Environment Back to the Frontend

The agent can spin up a Datafye environment, but the frontend needs to *show* what's running — which datasets, which symbols, backtest vs paper-trading, which broker. Rather than try to track that state by parsing the agent's own tool calls (fragile), we read it from the source of truth: after each chat turn, `_fetch_deployment_state()` makes a best-effort call to the deployed environment's deployment REST API (`GET .../deployment/{descriptor,datasets,symbols}` at `DATAFYE_AGENT_DEPLOYMENT_API_URL`). If no environment is up — connection refused, 404, no descriptor — it returns `None`. The `descriptor` call is load-bearing; `datasets` and `symbols` are enrichment and tolerated to fail.

Early on, a `None` read emitted *nothing* — and that turned out to be a bug: after a user tore down a foundry or switched datasets, the panel kept showing the *old* environment (SIP lingering after a move to Crypto) because no event ever arrived to correct it. So a `None` read now emits a **CLEARED** `env_status` — `status:'idle'`, `env_type:None`, empty dataset/symbol lists — which resets the panel. A still-running environment simply re-asserts itself on the next turn's read. The lesson: "no change to report" and "the thing is gone" are different messages, and a stateful UI panel needs the second one explicitly.

On success the agent emits two SSE events:

- **`descriptor`** — the raw deployment-descriptor YAML text, which the frontend relays verbatim to accounts (accounts keeps the canonical record of what each project deployed).
- **`env_status`** — a parsed, frontend-friendly summary: `{status, env_type, datasets, symbols, broker, mode}`. `mode` is the descriptor's `mode` (`backtest`/`paper`); `env_type` is the friendly label ("Foundry" for backtest, "Trading" for paper).

There's a gotcha buried in that payload that cost us a confusing afternoon, so it's worth writing down: **the environment-type field is keyed `env_type`, not `type`.** Every SSE frame is `{type: <event-name>, ...payload}` — `sse_event()` sets `type` to the event name (`env_status`, `descriptor`, etc.). If the payload *also* carried a `type` key, the spread would clobber the frame discriminator and the frontend's event router would mis-dispatch the message. Renaming the field to `env_type` sidesteps the collision entirely. The general rule: never put a `type` key in an SSE payload that gets spread into a frame whose discriminator is also `type`.

### Naming a Strategy From Its First Message

When a new conversation arrives it only has a provisional name — `deduce_name()` just grabs the first few words of the first message, which reads like a truncated sentence rather than a title. So on the **first turn of a new conversation** the agent fires `generate_title()`: a single cheap, direct Anthropic call (`POST https://api.anthropic.com/v1/messages` over `httpx`, using the haiku model from `DATAFYE_AGENT_TITLE_MODEL`, default `claude-haiku-4-5`) that summarizes the first message into a short Title-Case label. The agent renames the strategy on disk and emits a `title` SSE event (`{conversation_id, name}`) that Yukti adopts in place of the provisional name. This is deliberately the *one* spot where the agent talks to the model directly instead of through the Claude Agent SDK — title generation is a cheap one-shot summary that doesn't need tools, the agent loop, or the expensive reasoning model. It's also fully best-effort: no key, an API error, or an empty reply all return `None`, and the provisional `deduce_name` label simply stays.

### Deliverables You Can Take Away

Sometimes a turn produces something the user wants as a *file* — a CSV of backtest trades, a generated report, an exported chart. The workspace already has an `uploads/` folder, but that's for context flowing *into* the agent; a deliverable is the opposite direction. So each project gets a separate **`outputs/`** folder, and the convention is simple: the prompt tells the agent to write anything the user should be able to download into `outputs/`, and to keep scratch/working files elsewhere so only real deliverables surface.

Two endpoints expose the folder — `GET /v1/conversations/{id}/outputs` lists what's there (`{name, type, size, modified_at}`), and `GET /v1/conversations/{id}/outputs/{filename}` streams one file back as a `FileResponse`. Both are JWT-gated, and the download path is **path-safety-guarded**: the filename arrives off a URL, so `output_file_path()` resolves it and refuses anything that lands outside `outputs/` — the same `..`-can't-escape discipline the folder-delete uses.

The nice touch is that the frontend doesn't have to poll to discover new files. `/v1/chat` snapshots the `outputs/` folder *before* the turn and diffs it *after*, emitting an **`artifact` SSE event** (`{name, type, size}`) for each new or changed file. So the moment the agent finishes writing a deliverable, a download chip can appear. The diff is best-effort and wrapped so it can never break the turn.

### Counting Tokens Honestly (Per-Turn Usage)

Usage — tokens and cost — matters here because it drives billing and the hosted-tier quota meter. The naïve way to read it is the single `usage` field on the final result, and that's what we did first. The problem: a real agent turn isn't one model call. Claude reads a file, calls a tool, reasons again, calls another — a *multi-step* turn — and the flat `usage` number **undercounts** all of that.

So we now attribute usage from `ResultMessage.model_usage`, the CLI's per-model breakdown (`modelUsage`: model id → that model's totals for the turn). We build one delta per `(stage × model)` (`_usage_delta_from_model_entry` + `_accumulate_turn_usage` over `_TURN_USAGE_FIELDS`), idempotency-keyed per model so a re-emitted total doesn't double-count. If the CLI emits no `model_usage` at all, we fall back to the old flat read — degrade, don't crash.

There's a second leak the sidecars caused. The agent makes a few *cheap direct Anthropic calls* outside the SDK loop — `generate_title`, `classify_lifecycle`, and now `analyze_satisfaction` (all on the Haiku `TITLE_MODEL`). Those tokens never show up in `model_usage` because they aren't part of the agent subprocess's turn. So each takes an optional `usage_sink` list, appends its own `{model, usage}`, and `/v1/chat` folds those deltas into the turn total (tokens counted; cost left at 0, since the direct API response doesn't price them). Finally, `conversations.set_last_message_usage` stamps the per-turn usage onto the assistant message, so `/history` can hand the accounts Conversation view a per-message token+cost figure. The lesson is a general one for metering an agent: **the bill is the sum of every model call the turn touched, including the little ones you make off to the side** — read the per-call breakdown, not the headline number.

### Attributing Cost Step by Step

Per-turn usage tells you what a turn cost. It doesn't tell you *what in the turn* cost it, and on a twenty-minute build turn that is the question you actually want answered. So each model round now emits a **`step`** commentary badge into the activity rail carrying two figures. **`new`** is what this request appended to the prompt: the previous step's reply plus its tool results. **`carried`** is the prefix re-read from cache, and it is the interesting one, because it accrues on *every* step whether or not anything new happened, and it is what makes a long turn expensive. `new` is deliberately the sum of uncached input *and* cache creation rather than cache creation alone, because a span under the API's minimum cacheable prefix is silently not cached and bills as ordinary input; keeping the two apart would make identical work look different depending on where a cache boundary happened to fall.

**The trap worth writing down: an `AssistantMessage` is not a model round.** The SDK emits one message *per content block*, and every message of a round repeats that round's *same* usage object. Counting messages therefore produced a badge per block (2-4 identical badges in a row, observed on Sutra where this shipped first) and inflated the step number along with them. A round is now detected by the usage object **changing**, which works because the figures grow monotonically as the prompt grows, so an unchanged object means we are still inside one round. The good part of that story: the same repeat had been quietly double-counting the old live ticker for as long as the ticker had existed, and nobody noticed until duplicate badges made it visible. A *display* bug exposed a *metering* bug that had been wrong all along, which is a decent argument for putting your numbers somewhere a human will actually look at them.

**There is deliberately no per-step output figure.** What reaches us at `message_start` is a placeholder; the API docs show `output_tokens` there as literally 1, 2 or 3. The real count lands at the *end* of the same step's stream, on `message_delta`, which the SDK does not surface to us here. Storing the placeholder under a name that reads like an output count was judged worse than storing nothing at all, because a number that looks authoritative gets trusted and then quietly poisons everything downstream of it. Whole-turn output stays correct via `model_usage`; only the per-step granularity is missing, and it is missing *visibly*.

The live ticker changed meaning as a consequence. It used to sum `input_tokens + output_tokens` per round, which was reasonable before prompt caching and nonsense after: once the prefix is cached the first term is only the uncached remainder (single digits) and the second is that placeholder, so the ticker read in the **tens** on a turn whose context had already reached 64K. It now reports **context size** (`new + carried`, the whole prompt at that step), which is exact and needs no accumulation at all. The SSE field is still named `tokens` so an older client keeps working, but what it carries is different. Context size is also stored as a **latest value** (`conversations.set_context_tokens`), pointedly *not* as one of the additive usage fields: a later turn's prompt already contains the earlier turns, so summing context across turns counts the same context over and over.

**Tool weight now counts the call as well as the result.** The rail used to weigh only what a tool handed back, which is exactly backwards for the tools that cost the most. For `Write` and `Edit` the model generates the whole file into the *call's* input and the result is a one-line acknowledgement, so a result-only figure reported roughly nothing for the single most expensive thing in the step. Tool-label entries now carry `call_tokens` (measured off the call's JSON, since that is the form it takes in the prompt, escaping and all) alongside `result_tokens`. And the result is weighed on the **full** content, before `_DETAIL_OUTPUT_CAP` truncates it to 2000 chars for display, because the whole result is what lands in the prompt and gets re-read on every remaining round of the turn. Both are rough 4-chars-per-token estimates; an exact count would mean a `count_tokens` API call per tool call, which is absurd for a display badge.

One last plumbing detail: the `step` is stamped on **every** commentary entry of a round, not just on the badge. One round can emit several rail lines (narration flushes one line per block), so the grouping has to be recorded at the source rather than inferred from how the rail happens to render. It is an identity, not a dense index, so gaps are fine. A diagnostic came along for the ride: `DATAFYE_AGENT_LOG_USAGE=1` dumps the raw per-round usage object and the usage-bearing stream events, which is how you find out what fields the bundled CLI actually passes through. It is off by default (one line per model round is hundreds per build turn) and logged at **INFO** on purpose: the service runs at INFO, so a debug-level line would be silently swallowed and the diagnostic would look *broken* rather than *disabled*.

### A Subagent Is Not the Agent (DAT-138)

A subagent's messages arrive on the *same* stream as the main thread's, carrying **their own conversation's** usage, and the only thing that tells them apart is `parent_tool_use_id` (`None` means the main thread). Folding them into one sequence interleaved two independent contexts: the step number jumped around, and the context appeared to **shrink** mid-turn, which is impossible for a real prompt and is the tell that you are reading two things at once. So only the main thread is measured. The subagent's tokens are still billed and still counted, since they arrive in `ResultMessage.model_usage`, and its tool calls still count toward the turn's tool metric; nothing about the accounts totals changes.

Both the badge **and the content** are suppressed, and the second half is the part that isn't obvious. Gating only the badge leaves `pending_blocks` (the narration buffer, see [Narration routing](#narration-routing-two-registers-in-one-conversation)) thread-blind: the subagent's prose piles into the same buffer as the main agent's and flushes into the rail as if Yukti had said it, interleaved and out of order with respect to either thread. The rail is the main agent's account of itself, and delegated work is represented there by the one tool line that spawned it.

Which brings us to why delegation is off entirely. `Task` was already absent from `INTERNAL_TOOLS` on the harness-only grounds described [earlier](#the-claude-agent-sdk), and the stronger reason is now recorded next to it: **a subagent does not inherit `prompt.py`.** It runs on the SDK's default agent prompt, so every rule this agent carries about audience, plain language, ASCII punctuation and short action lines is simply absent for delegated work, and whatever it produces lands unfiltered in a user-facing surface. It also spends its own containment benefit: a subagent exists so that its exploration stays *out* of the main context, and then a large report lands in that context anyway. The general lesson is that a tool inherited from a harness comes with the harness's assumptions about who is reading the output, and those assumptions do not travel.

### Thinking, Made Visible and Made Safe (DAT-139)

Reasoning is now requested explicitly: `thinking={"type": "adaptive", "display": "summarized"}`. Adaptive thinking is the model's default; what is *not* the default is getting the text back. On Opus 5 `display` defaults to `omitted`, so thinking blocks were arriving with an empty string, the emit was skipped, and the reasoning was invisible in the rail while still being billed at output rates. `display` controls visibility only and does not change the bill, so the old state of affairs was the worst of both: paying for reasoning and showing none of it.

Two things then had to happen to that text. It is **ASCII-folded** (`_ascii_fold`) at the append site, because `prompt.py`'s plain-ASCII rule governs what *the agent* writes, and this is the API summarizer's prose, which no prompt instruction reaches (observed thinking is full of em dashes), while accounts stores the commentary trail ASCII-encoded. Note *folding*, not stripping: a dropped em dash welds two clauses into one unreadable sentence, and a dropped accent beats a dropped letter, so the map turns an em dash into `" - "` and an ellipsis into `"..."`, then falls back to NFKD decomposition (which leaves the base letter behind) before it removes anything at all. And the text is now **persisted** as a `thinking`-kind commentary entry rather than being live-only, so it replays from `/history`. Reasoning that is billed but leaves no trace is exactly the record you want back when you reopen a strategy a week later and ask why it did that.

### Reading the Room (Inferred Satisfaction)

We'd like to know whether a project is going well for the user without making them fill in a star rating. So after the stream ends, `analyze_satisfaction` — another cheap Haiku sidecar, same shape as `classify_lifecycle` — reads the last few turns of the transcript and infers a **1-5 rank plus a short reason**. `_report_satisfaction_to_accounts` then POSTs *only that derived signal* (never the raw conversation) to `POST /accounts/{u}/projects/{id}/satisfaction` with `source=inferred`, forwarding the caller's JWT — the same self-scoped agent→accounts write the usage reporter and the ConnectTrade user-cred write-back already use, so no new credential is involved. `conversations.set_satisfaction` caches it agent-side, and a `"user"` source is **sticky**: an explicit rating from the human always wins over the model's guess and won't be overwritten by the next inference.

The inferred read isn't the only path. The model can also record an **explicit** rating the user gives directly, through a `submit_satisfaction` tool — and it can log free-form **feedback** mid-conversation through a `submit_feedback` tool, instead of making the user hunt for the app's Send-feedback button. Both live in an in-process SDK-MCP server that `_build_reporting_mcp` stands up per turn (using the SDK's `create_sdk_mcp_server` / `@tool`), and both report to accounts forwarding the *user's own JWT* — the same self-scoped write the usage and inferred-satisfaction reporters already use, so the agent never holds a Slack or JIRA credential (accounts does the routing). `submit_feedback` POSTs to `POST /accounts/{u}/feedback`, and when accounts opens a tracking ticket it hands back a **`ticket`** key (provider-neutral; `jira` is kept as a fallback for older builds) that the tool surfaces to the user as "A tracking ticket was opened (`DAT-NNN`)". `submit_satisfaction` writes `source=user`, which is sticky and outranks the model's inferred guess on both sides.

One guard that got dropped along the way: the usage, satisfaction, and feedback reporters used to only fire for project ids with a `proj-` prefix. That excluded a **reconciled** browser-local project — one whose create failed and was later imported into the registry through accounts' reconcile endpoint. Since accounts is the authority on what's registered, the reporters now accept **any** id (an unregistered one simply 404s and the best-effort call logs it), and the post-stream satisfaction gate keys on a **forwarded identity** (the caller's JWT plus username) instead of the id prefix — so it still cleanly skips a self-hosted run with no accounts behind it.

The one subtlety worth writing down is *when* these tools exist. The server is attached only when the turn can actually route to accounts — a platform user with a forwarded JWT. A self-hosted run with no accounts behind it gets neither tool, and the prompt tells the model to fall back to pointing the user at the in-app option. So the same agent binary behaves correctly whether it's a managed sandbox or someone's own box: it offers the in-conversation shortcut exactly when the shortcut can work, and never dangles a tool that would fail. The prompt carries matching FEEDBACK and SATISFACTION sections — offer to log only after the user agrees, capture a rating only when the user genuinely gives one, and never fish for either.

### Answering Questions About Datafye Itself

Most of the time the user is building *with* Datafye. But sometimes they ask *about* it — "what datasets does Datafye support?", "does it do options?". The system prompt now carries a **REPRESENTING DATAFYE** section that puts the agent in a product-expert Q&A posture for exactly those moments: answer as the authoritative Datafye expert, grounded in the on-disk docs (`{docs_dir}`); guard against confusing the user or inventing capabilities; say so gracefully when it genuinely can't answer; and stay honest rather than over-promising. It's the same "the docs are on disk, use them" philosophy pointed at a different job — being the product's knowledgeable representative, not just its build tool.

A small but load-bearing prompt rule sits alongside it: **use plain ASCII punctuation, everywhere.** No em/en dashes, no curly quotes, no ellipsis character — in chat replies *and* in the activity/progress lines. The reason isn't stylistic: non-ASCII characters break the accounts `resultJson` storage downstream, so a stray em dash in a reply can corrupt what accounts persists. Cheaper to forbid the characters at the source than to sanitize everywhere they might land.

### Knowing Whether the Box Can Actually Do Anything (DAT-198)

"Running" used to mean this Python process answers `/health`. That is a real fact, and it is almost useless: the agent can be perfectly healthy on a box whose foundry is half-built, wedged, or absent. On u1 the gap put a user's request onto a box three minutes into its first provision.

The detail that stings is that the agent *already knew*. Fifteen seconds before the provision started it logged `Datafye API MCP: NOT REACHABLE ... tool calls requiring the deployment will fail`. The information existed and nothing consumed it — which is a more common failure than missing information, and a lot harder to notice, because every component is individually doing its job.

So `/health` now republishes a `foundry` block, and the model is handed the state and the reason in its prompt rather than being left to discover the situation by colliding with it. Mid-operation it is told explicitly not to start, apply or provision, because a second operation on one foundry is precisely what corrupts it. On a failure it is pointed at the failure report, told to quote the real error, and told **not** to rebuild automatically — the broken environment is the only evidence of why it broke.

Two distinctions in the reader are load-bearing, and both are the same shape: refusing to collapse two different answers into one.

**`unknown` is not `absent`.** A missing state file means *we do not know*; `absent` means *we know there is no environment*. Those demand opposite responses, and a fresh box legitimately produces the first — the boot unit is ordered after the agent, so the agent answers `/health` before anything has written a state at all. Absence only becomes meaningful after a grace period, and this process cannot judge that: it does not know when the box booted. Accounts does. So the agent reports what it sees and declines to invent a verdict.

**Reading never raises.** It sits on the `/health` path, which has to answer when everything else on the box is broken. An agent that cannot report its own health is indistinguishable from a dead instance — and telling those apart is the single thing accounts most needs.

⚠️ **For a while the block reported `unknown` on every box, because nothing wrote that state file** — expected at the time, not a defect to chase. Both writers had been removed deliberately (see the postscript below and the DAT-199 story that follows). That gap is now closed, and it did not close by putting the writers back: readiness is *derived* by whoever asks rather than stored by whoever moved last. See [Readiness, Derived](#readiness-derived-dat-198-finished) below for how the three inputs combine and which branches of the truth table are the opposite of what you would guess.

A postscript on ownership, because it cost a design round. The first implementation had the *deployment engine* record intent on every lifecycle command. It reads plausibly — the command is the thing acting — and it is wrong, with a mundane counterexample: someone SSHes in to debug, runs `stop`, and the engine writes down that this environment is meant to be stopped. The box reboots and the boot service faithfully leaves the foundry down, permanently. A debugging action promoted into standing policy. **"An operation is running" is a fact about a process; "this box should have a running foundry" is a decision somebody made** — and the component performing an action is very often not the one that decided it should happen. Desired state belongs to accounts, where the user's request actually arrives; the agent reads it and reports.

### The Foundry That Nobody Built (DAT-170)

Here is a small horror story about a comment.

The hosted AMI is baked by running the installer with `--ami-cleanup`, a flag that
deliberately skips foundry provisioning. That skip is correct: provisioning pulls
images, starts containers and writes instance-specific state under `~/.rumi`, and
snapshotting any of that into a shared AMI would hand every future sandbox one
sandbox's leftovers. So the installer skipped it, and left a comment explaining what
would happen instead:

> Each per-user sandbox provisions its own foundry at first boot.

Nothing implemented that sentence. There was a `first-boot.sh`, which made it look
implemented, but that script is for the standalone/marketplace AMI: it reads EC2 user
data and hard-codes `--mode standalone`, and no systemd unit ran it on a hosted box
at all. Meanwhile `prompt.py` was telling the agent the exact opposite of the truth —
*"your sandbox already has one, so you almost never need `provision`"* — and the
installer's own warning text was confidently asserting a third thing, that the agent
provisions on demand, which it never did either.

So a brand-new sandbox had no environment, an agent that had been told not to make
one, and three files each describing a different system. The gap survived because
every individual statement was plausible and no two of them were ever read together.

The first fix was a hosted first-boot one-shot, `datafye-foundry-firstboot.service`,
running `install/first-boot-foundry.sh`. It provisioned an empty foundry — Platform
plus the API system, no datasets — which is precisely the state the prompt already
described, so the prompt did not have to change; it just became true.

Three decisions inside it were right and have survived every rewrite since.

**It keys on real state, not on a marker.** The obvious way to write a "first boot"
script is to touch a sentinel file when it is done and bail out if the sentinel
exists. That would have been a bug of the same family as the one being fixed: a
marker written before the work succeeded locks the box out of ever retrying, so a
single failed provision produces a sandbox that is permanently empty and *believes it
has already handled that*. Instead it asks `foundry local status` whether a foundry
is actually there. The pleasant side effect is that the unit is safe to leave enabled
forever: an ordinary reboot, or a wake from dormancy, costs a fast no-op, and a boot
after a failure retries by itself.

**It runs the CLI as the `datafye` user, always.** This looks like a detail and is
actually a trap with teeth. The `rumi` user is in `wheel` but not in `docker`, so as
any other user `foundry local status` cannot reach the Docker socket — and it did
not report a permission problem, it reported *"not provisioned"*. That is a confident
false negative indistinguishable from a genuinely empty box, and acting on it means
provisioning on top of a live environment, which collides and fails. A status command
that cannot tell "absent" from "I am not allowed to look" is worse than one that
errors out.

**It rides the companion-refresh loop.** The script lives in the install directory and
is executed from there by a systemd unit, which puts it in exactly the category that
bit us before: files a box only receives when its AMI is baked. Adding it to the loop
that already refreshes `upgrade-check.sh` means a fix to it reaches the fleet through
an ordinary auto-upgrade instead of requiring a re-bake.

Failure is deliberately non-fatal. The agent is genuinely useful with no foundry —
chat, docs, code and memory all work — so the unit logs loudly to the journal and
leaves the agent running rather than taking the whole box down over an environment
the user may not have asked for yet. The one thing it must not do is fail *silently*,
because silence is how the original gap survived: a missing foundry left no trace
anywhere, and the first person to notice was a user whose agent was confidently
acting on a false assumption.

The transferable lesson is about where truth lives. A comment describing a step in
*another* file is not a mechanism, it is a hope. If a promise spans two files, put the
thing that keeps it somewhere a test or a boot will exercise — and when you find one
claim that is wrong, go and read every other place that claims something about the
same subject, because they will not agree either.

### The Second Half of That Fix (DAT-199)

The first-boot unit solved the boot it was named after and nothing else, and the two
things it left behind turned out to matter more than the thing it fixed.

The first is that it was **hosted-only**. A user who provisions their own box stops
and starts it like anyone else, and hits the identical failure — so half the fleet
had a mechanism and half had a comment. The second is subtler and nearly shipped as a
disaster. A separate ticket existed for the *wake* case: restore the environment when
a dormant box comes back. Two tickets, two obvious homes, two scripts. And on
2026-08-09 a sandbox called u1 showed exactly what that costs: the first-boot unit
started a provision, the agent fired a `start` three minutes into it, an `apply`
landed on top of both, and the environment had to be rebuilt from scratch. Twice.
**Two boot-time actors mutating one foundry was already the incident.** Adding a
second on purpose would have moved it from every fresh boot to every wake.

So the two became one: `datafye-foundry-boot.service`, installed in **every** mode,
running on **every** boot. The rename mattered more than it sounds. "first-boot"
described one of the two jobs the file did, and a name that disagrees with behaviour
is precisely the drift that produced DAT-170 in the first place.

**What it does not do is the interesting part.** An earlier version of this work had
every lifecycle command write down what the environment *ought* to be. That shipped,
and was reverted a day later, because of one scenario: an engineer SSHes into a box
to debug something and runs `foundry local stop`. The engine dutifully records
"intended: stopped". The box reboots. The boot service reads the record and leaves
the foundry down — forever. A debugging action had been promoted into standing policy
by a component with no way to tell the two apart.

The line that fell out of that is worth carrying to other systems: **"an operation is
in flight" is a fact, "this box should have a running foundry" is a policy.** The CLI
knows the first and cannot know the second. Intent is formed where the user's request
actually arrives — in the accounts service — and is pushed down to the box as a
replica, never a record. So the boot service *reads* intent, *interrogates* reality,
*reconciles*, and stores nothing at all. Absence of a recorded intent means running,
because a sandbox exists to host a foundry, and that default is what lets the unit be
correct today while the push half is still unbuilt.

Two rules keep it from doing harm, and both are the kind you only write down after
watching something helpful cause damage:

- **Reconcile additively only.** If intent says stopped but the foundry is serving,
  somebody started it by hand. Never tear down live work to satisfy a record.
- **Never act while something else owns the environment**, and exit *successfully*
  when you decline. A boot unit parked behind a seventeen-minute provision is
  indistinguishable from a hung boot, and a red unit in `systemctl status` is a
  signal worth reserving for the boots where something is genuinely broken.

Two implementation details are worth the space because both are cases where the
obvious thing is silently wrong.

**The script cannot take the lock, and must not pretend to.** The engine's mutual
exclusion is a Java `FileChannel.tryLock()`. The shell's equivalent is `flock`. They
look interchangeable and are not: on Linux `tryLock` maps to **fcntl** record locks
while `flock(1)` uses **flock(2)**, and those are *independent lock namespaces*. A
shell `flock` on the same file succeeds against a held Java lock — mutual exclusion
that reports success and provides nothing, which is worse than none, because now
you believe you have it. So the script does not lock. Every operation it performs goes
through the CLI, which takes the real lock for its duration; the script's own check is
only a courtesy that avoids starting a command destined to be refused, and names the
current holder in the journal. The gap between checking and acting is closed by the
lock, not the check.

**It refuses to trust a verdict it could have trusted.** `foundry local status` prints
a tidy `Overall: HEALTHY`, and skipping the reconcile when it says so is free
performance. It is also wrong: that verdict keys on the deployment API answering, and
an environment with one dead service still reports HEALTHY. Taking the shortcut would
make the unit blind to exactly the partial state it exists to repair. So the serving
decision stays in one place — the per-service prober inside `start`, which asks each
service for an *answer* and relaunches only the dead ones — and the only judgement the
shell makes for itself is whether an environment exists at all. A healthy box pays a
few seconds for that; a broken one gets fixed.

The last change is one line of behaviour with a lot behind it: a fresh box is now left
**running** rather than provisioned-and-stopped. Under the readiness model a box must
end up matching intent, and intent is running — so stopping it made every fresh box
permanently unready and the prompt's "your sandbox already has one" false on precisely
the boxes that had just built one. The original reason for stopping (keeping a box out
of the app-less wake state, where `--restart unless-stopped` faithfully restores
containers with no applications inside them) has not gone away; it moved to where it
belongs, which is stopping the apps cleanly *before* the box stops rather than leaving
them stopped for hours in case it does.

### Stopping the Environment Before the Box (DAT-125)

For most of this system's life, "stop the sandbox" meant exactly one thing: accounts
called StopInstance on a live box and the lights went out. That is a perfectly good way
to stop a web server. It is a bad way to stop a box hosting a running foundry, because
it pulls the floor out twice over.

The first way is the one you would predict. The Rumi applications are killed mid-write,
so a transaction log can be left unflushed — the environment's own record of what
happened, cut off mid-sentence.

The second is worse because it is *delayed*. Nothing ever marked the containers as
stopped, so on the next boot Docker's `--restart unless-stopped` did precisely what it
promises and faithfully brought every container back up, with no applications inside
them. The box is alive, the containers are alive, and nothing is running in them. That
is the app-less wedge of DAT-171, arriving hours later, produced by a restart policy
behaving perfectly correctly. u1 demonstrated it live: the idle monitor stopped that
sandbox **sixteen minutes into a provision**.

The fix spans three repos — accounts learns to ask, the agent learns to answer, and the
deployment engine learns to actually finish a stop when one application misbehaves. The
agent's half is `POST /v1/foundry/stop`, called immediately before StopInstance, for
both the idle `Dormant` stop and a deliberate user Stop. And the interesting part is
not that it stops the foundry. It is what it says back.

**The reply is a decision, not a result.** There are four statuses, and only one of them
changes what accounts does:

| status | what happened | what accounts does |
|---|---|---|
| `stopped` | the environment came down cleanly | stop the box |
| `absent` | there is no foundry here | stop the box |
| `failed` | the stop did not complete, but nothing is in flight | stop the box, loudly |
| `busy` | another operation owns the environment | **abort the stop** |

The asymmetry is the whole design, and it is worth sitting with, because the instinct is
to treat `failed` and `busy` as the same kind of bad news. They are not. An environment
mid-provision must not be cut off — that is the u1 incident, and interrupting it costs a
rebuild. But a stop that merely *failed* protects nothing by keeping the box up: the
damage, if any, has already happened, and the alternative is a sandbox that can never
stop cleanly and therefore **bills forever with nobody watching**. So `failed` still
stops the box, at WARNING, because that log line is the only trace that a box went down
without a clean stop. Refusing to act on a failure is not caution; it is just a slower,
more expensive failure.

One implementation detail carries a transferable lesson. **`busy` is decided from the
DAT-183 in-flight marker, never from the lock's refusal text.** The tempting shortcut is
right there: the operation lock refuses with a perfectly clear sentence, and matching on
it is three lines. But that sentence is written for *humans*, and it lives in another
repo. Keying on it would create a cross-repo contract with no compiler behind it — the
kind that survives every test suite and breaks the day somebody improves the wording.
The marker exists precisely to answer this question, so it is what gets asked.

The engine half is the same lesson in a different key: `stop` no longer aborts when one
application refuses to shut down, and falls back to the container inventory when the
deployment API cannot say what is deployed. The API *is* one of the applications — so
the box that most needed stopping was exactly the box where the stop did the least.

### Readiness, Derived (DAT-198 finished)

The DAT-198 reader shipped ahead of its data and spent a while reporting `unknown` on
every box in the fleet, because both writers of the state file it read had been
deliberately removed. That is now closed, and the closing is more interesting than the
original design, because the answer was not "put the writers back". It was to stop
storing readiness at all.

Readiness is now **derived**, from three inputs that no single component owns:

- **Intent** — what accounts decided, pushed down to the box and cached on disk.
- **In flight** — the DAT-183 marker plus a liveness check: is somebody mid-operation
  right now.
- **Observed** — are the applications actually *answering*.

`derive()` combines them and is a pure function, deliberately, because the truth table is
the part that is easy to get subtly and confidently backwards. Three of its branches are
the **opposite** of the obvious reading, and each one is a small lesson in what
"healthy" means.

**Intent `stopped` is READY, not unready.** A foundry the user asked to stop is in good
order. Calling it unready leaves the box permanently unhealthy, curable only by starting
an environment the user explicitly did not want — a health check that can only be
satisfied by overriding the user is not a health check, it is a demand. And it stays
ready even if the observation finds it *serving*, because somebody started it by hand:
that is more than intended, not less.

**In-flight beats every observation.** Mid-provision the deployment honestly reports "not
serving", which is indistinguishable from a genuine mismatch. Anything reconciling on
that reading would set about repairing an environment that is being built — which is the
u1 collision exactly, damage produced by something trying to help.

**Absent intent means running.** A sandbox exists in order to host a foundry, so "no
deviation has ever been recorded" and "this should be running" are the same sentence.
`unknown` would leave every fresh box permanently unready; `stopped` would leave it
permanently empty. The default is not a fallback here, it is the statement.

**The observation is refreshed in the background, not on the request**, and that is a
deliberate inversion of the obvious design. Interrogating an environment costs real time
precisely when something is wrong, and `/health` is not a debugging endpoint — it is
polled by accounts for dormancy decisions, by the upgrade cron every minute, and by the
SPA. An agent that goes quiet while it thinks is indistinguishable from a dead instance,
and the one thing this endpoint must never do is make a healthy box look dead. So a loop
refreshes on a timer and `/health` serves the snapshot along with its age. The in-flight
read is cheap enough to do inline, so the fast-moving input stays current and the slow
one is allowed to be a little stale, with its staleness stated.

One lesson from the build is worth recording because it nearly shipped. The first cut of
the health-ping parse assumed the response was a flat array of services. It is not: it
is an object **keyed by service name**, nested under per-system groups. The code ran, the
loop iterated over something, and the answer was quietly wrong. What caught it was not a
test — it was cross-checking the Java prober that had been reading the same endpoint
correctly for months. It is now guarded properly: all nine payloads captured off a live
foundry are replayed through the Python parse in the test suite. **The agent and the CLI
disagreeing about one box's health would be worse than either of them being wrong
alone** — two components confidently contradicting each other is a debugging session
nobody wins.

### Who Is Allowed to Change Their Mind (DAT-214)

Intent belongs to accounts. But *somebody* has to tell accounts that the user wants
their environment gone, and answering that question turned out to be a nice piece of
reasoning: we listed every actor that can mutate a foundry and asked, of each, whether
it is entitled to change **intent** — the standing policy — as opposed to merely acting.

Almost all of them are not.

- **Accounts' explicit stop and start** are decisions about the *box*. Someone who stops
  their sandbox on Friday afternoon fully expects their environment to be there on
  Monday. Letting the box's power switch rewrite the environment's policy would punish
  the user for being frugal.
- **Dormancy** is not a decision at all. It is a cost optimisation the user never asked
  for and mostly never sees. A box that wakes and declines to restore what nobody chose
  to lose is the clearest possible reductio.
- **The Yukti SPA's Stop** is box-scoped for the same reason as accounts'.
- **An operator's `foundry local stop` while debugging** is explicitly not a policy
  statement, and this codebase has already paid for pretending otherwise — that is the
  reverted design from DAT-199.

Which leaves exactly one actor that may legitimately change intent: **the user, speaking
through the model.** It is the only participant in the conversation, so it is the only
one that can possibly know. And it was, of course, the one with no way to say so.

**The distinguishing factor is not the command.** This is the crux, and it is why every
"just infer it from the lifecycle command" design fails. `foundry local stop` run because
the user said "shut it down, I'm done for the month" and the same command run because the
model is working around a wedged service are **byte-identical**. There is no flag, no
argument, no telemetry that separates them. The only place the difference exists is in
the sentence the user typed, which is why the agent is the sole production caller of the
intent endpoint, forwarding the user's own JWT on the same self-scoped channel the usage
and feedback reporters already use.

**Two mechanisms, because a prompt rule alone will not hold.** Asking the model to
classify a request as policy *and* remember to take a second action after it has already
performed the first is asking for two things it will sometimes not do — and this codebase
has the receipt. The prompt carefully explained how to run long operations, and the agent
backgrounded a provision anyway, which was then orphaned when the session ended (DAT-185).
A rule the system depends on should not live only in prose that the model is free to
weigh against everything else in its context. So:

- an **explicit tool** the model can call when the user actually states a decision, and
- a **post-stream Haiku sidecar** that classifies the turn and **always runs**, so the
  mechanism does not depend on the model choosing to use it.

They cannot fight: a turn where the tool fired is marked, and the sidecar skips it.
Explicit beats inferred, because the model's own statement is better evidence than an
inference drawn from the same conversation.

**The classifier is deliberately biased towards "no decision"**, and the bias is
asymmetric on purpose. A wrong `stopped` costs the user their environment. A missed one
only means their environment keeps running — which is the default anyway, and the thing
they will notice least. When the two error directions have wildly different prices, a
classifier that is "accurate" is the wrong goal; one that is *cheap to be wrong in* is
the right one. An unrecognised value, unparseable output, an API failure and a missing
key all come back as no decision rather than a guess.

### The Warm Signal: Why a Box Refuses to Sleep (DAT-184, DAT-177)

Accounts stops a box that nobody has **chatted with** for thirty minutes. That is a
perfectly reasonable measure of something, and it is the wrong question. On u1 it
stopped a sandbox sixteen minutes into a foundry provision: nobody had typed anything,
so by the only measure available the box was idle. It was not idle. It was doing the
single most expensive thing it ever does.

The fix is almost embarrassing in hindsight. `/health` already published a field called
`active_proxied_apps`, and accounts already treated a non-empty value as busy in both
places that matter. The agent had always sent `[]`. **The plumbing was live and inert
the whole time**, wired end to end on the consuming side and hardcoded to a lie on ours.
Filling an existing field rather than adding one meant no accounts change and no
coordinated deploy — and it is worth noticing how the bug hid: nobody was going to
discover an empty list was *wrong*, because an empty list is also what a genuinely idle
box sends.

The values are self-describing (`env:data-flowing`, `env:provision`) rather than a bare
`true`, because they surface raw in the admin panel and in logs, where an operator
looking at a box that refuses to sleep needs to know *why* it refuses.

The load-bearing decision is what **does not** count. **An idle or empty foundry reports
nothing at all** — and the pleasing part is that this falls out of the definition rather
than needing a carve-out. An empty foundry has no datasets, so no service reports any
activity, so it reports nothing. If merely having containers up kept a box awake, every
provisioned sandbox in the fleet would be permanently warm and **dormancy would stop
saving anything whatsoever**. A cost-saving mechanism that never fires is worse than no
mechanism, because it also carries the code.

Two more rules in the same spirit. **Unreachable is not warm**: "I could not look" must
not pin a box awake indefinitely on a probe that may never recover, which also matches
how accounts treats an agent *it* cannot reach. And the case that looks lost to that
rule — a box mid-provision whose API is not up yet — is covered by the in-flight signal,
which is local and needs nothing to answer.

The window is **a third of the thirty-minute idle threshold**: long enough that no gap
inside genuinely continuous work reads as cold, short enough that it can never dominate
the dormancy decision. It was chosen *against* the threshold rather than picked, which is
the difference between a constant and a number.

And then the off switch, which is a lovely small trap. Setting the window to `0` disables
environment-based warmth entirely — and that has to be handled **on the agent's side**,
because the platform reads `activeWithinSeconds == 0` as "use my default of 300". Forward
the zero and you would get a *shorter* window instead of none: reaching for the off switch
and being handed the opposite. Whenever you pass a user-facing knob through to another
system's API, go and check what that system thinks your sentinel value means. Zero, empty
string and null are the three most commonly overloaded values in software, and every layer
is entitled to its own opinion about them.

### A Reading User Is Still a User (DAT-169)

The warm signal keeps a box alive for active *work*. This one keeps it alive for a
present *person*, and they are genuinely different problems.

Accounts measures idleness from `last_chat_activity_at`, and that field only ever
advanced when a chat turn ran. So a user reading a backtest result, studying a scorecard,
or simply thinking for half an hour looked **exactly** like a user who had closed the tab
— and their box dormed underneath them, mid-thought.

`POST /v1/activity` is the agent's half: it bumps the same timestamp a turn does, without
running one. One assignment, no I/O, because every open tab calls it on an interval and
anything heavier would be a permanent per-user background load whose entire purpose is to
say "still here".

**Deliberately the same field, not a peer.** The temptation is to add
`last_presence_at` alongside it and let accounts take a `max`. But the monitor's actual
question is "when was this box last of use to somebody", and reading answers it every bit
as well as typing does. Two fields would mean two things to keep in sync, two places to
forget, and a `max` computed forever, in exchange for a distinction nobody consuming it
needs.

**⚠️ Visible tabs only — and the agent cannot enforce it.** That half of the contract
lives in the frontend, which gates on `document.hidden`, stops the interval when the tab
goes away, and self-clears on sign-out. It has to, because a hidden tab that kept pinging
would pin every abandoned browser session's box awake forever, and dormancy would stop
saving anything — the same failure mode as a foundry that counts as warm just for
existing, arrived at from the other direction. Worth being honest about this shape when
you meet it: a rule whose enforcement lives in a component you do not control is a
convention, not a guarantee, and the only defence is to write it down where both halves
can see it.

### The Rule We Wrote Down Three Times and Still Broke (DAT-203)

There is a rule this codebase keeps rediscovering: **never offer, or forbid, a capability
the surface does not actually control.** It was learned from `AskUserQuestion` (offered to
the model, no UI to answer it, so questions vanished), then from the `Task` family (offered,
but a subagent does not inherit the prompt), and then from background execution — where the
prompt had *suggested* `&` and `nohup`, the agent took the suggestion, and a provision was
orphaned when the session ended. That third one was fixed by writing an absolute prohibition
into `prompt.py`: never background anything, no exceptions, three reasons given.

The prohibition was correct, well-argued, and could not possibly work.

Because the thing doing the backgrounding was never the model. When a Bash command outlives
its timeout, the harness does not kill it — it **moves it to the background**, returns a
task id and an output file, and lets the turn carry on. No rule addressed to the model can
prevent that; the model isn't the actor. We had written a rule governing the wrong party and
then relied on it.

What that costs is visible in a real incident. On u1 the model ran `start`, the harness
backgrounded it at the 600-second default, and the returned message — a timeout notice — is
very nearly indistinguishable from completion. The model had no way to check: there is no
`BashOutput` here, no `KillShell`, and the `Task` tools are deliberately absent. So it polled
the output file, saw nothing (these commands go long stretches without writing), concluded
the operation was done, and fired `apply` on top of a `start` that was still running. Both
failed. The environment was destroyed twice over.

The fix turned out to be one environment variable. The 600 seconds was never a hard limit —
it is the *default* of `BASH_MAX_TIMEOUT_MS`, and it is configurable. Raising it to thirty
minutes, comfortably past the seventeen-minute cold provision, means the harness has no
reason to background the command in the first place.

That is the shape worth keeping. **When a prohibition fails, check whether you were
prohibiting the right actor** — the durable fix removes the *reason* for the behavior rather
than adding another sentence forbidding it. The prompt rule stays, because the model
genuinely should not detach things; it just no longer has to carry weight it was never able
to bear.

Three details, each a small lesson of its own.

**Only the ceiling moves, not the default.** `BASH_DEFAULT_TIMEOUT_MS` applies to *every*
command, so raising it would mean an ordinary hung command — a curl to a dead host — blocks
the turn for half an hour. The ceiling is opt-in: it only takes effect when the model asks
for a long allowance, which the prompt tells it to do for environment work specifically. A
limit and a default are different knobs, and the temptation to move them together is how you
turn a targeted fix into a general liability.

**The old cap was invisible because it clamped instead of refusing.** Asking for 900 seconds
under a 600-second ceiling got you 600 seconds and no error at all. Silent clamping is the
same failure mode as the empty output file and the hardcoded `active_proxied_apps: []`
elsewhere in this system: a value that is *plausible* is far more expensive than one that is
obviously broken, because nothing prompts anyone to look.

**And it was verified, not reasoned about.** Published reports claim these variables are
inert in some versions, so the behavior was tested against the actual binary in both
directions: with the cap below the command's runtime it backgrounded exactly at the cap, and
with the cap above it the same command ran to completion in the foreground. That check
matters more than usual here, because ⚠️ **the Claude Code CLI is unpinned and the installer
skips it entirely when a binary already exists** — so a box's harness can be any version that
was current the day it was built, and it will never move again. A fix that depends on harness
behavior sits on ground that drifts per box.

### The Turn That Died Because It Looked At Its Own Work (DAT-204)

A user spent thirty-seven minutes on an earnings analysis. At the end of it the model
read back a chart it had drawn, to check the layout — a habit that had already caught two
real collisions in that same session. The turn died on the spot, with an error about JSON
and a buffer size. The next message came back "Could not reach the agent". Only the second
retry recovered, and the work survived only because the analysis had been written to files
along the way.

The mechanism is unglamorous. The CLI writes one JSON message per line; the SDK frames
those lines and refuses any single message larger than a megabyte. The refusal is raised
out of the **read loop** — not out of the tool call. So it does not fail the read and let
the model try something else; it ends the conversation's turn and everything in it.

**The blast radius did not match the mistake**, and that is the part worth generalising. A
tool call is a small, recoverable unit: it can fail, the model reads the failure, adapts,
continues. A turn is a large, unrecoverable one. This bug took an error that belonged to
the first category and reported it in the second, and the cost of the mismatch was
everything the user had done that hour rather than one wasted step.

Three changes, because none of them is enough alone.

**Raise the ceiling.** Sixteen megabytes instead of one — far above any legitimate result.
This makes the reported incident impossible. It is also, on its own, a false comfort: a big
enough payload still blows any finite cap, and the cap has to stay finite or a runaway
result trades a lost turn for a lost process. Verified rather than assumed, by driving the
real transport: two megabytes dies at the default and passes at sixteen, and twenty
megabytes still dies at sixteen. There is always a payload big enough.

**Refuse before you read.** A `PreToolUse` hook checks a file's size and, past half the
buffer, denies the read with a message explaining what to do instead — `head -c` a log,
summarise a data file in Python, check an image's dimensions rather than reading the image.
A denial costs one tool call; the alternative costs the turn. The threshold is *half* the
buffer rather than the whole of it because the framer bounds the encoded message: JSON
escaping and the envelope make it bigger than the file on disk, and base64 for an image
adds a third again. Half absorbs both without arithmetic that goes stale the moment the
envelope changes.

⚠️ **The hook fails open, deliberately.** It runs before every single `Read`. A guard that
breaks reading would be far worse than the bug it prevents, so an unstattable path, an
unfamiliar input shape, a permission error — all of them allow the read and let the CLI
decide, exactly as before the guard existed. When you insert yourself into a hot path, the
question is not "is my check correct" but "what happens when my check is wrong".

**And say something true when it happens anyway.** The raw error names our transport and
reads like data corruption, which sends the reader looking in entirely the wrong place. It
now says that a step produced more output than one message can carry, that anything already
written to files is safe, and to ask for a smaller piece. Everything else keeps its own
wording, because a message that is merely technical beats one that is confidently wrong.

**A postscript that changed what we thought we were fixing.** None of the obvious vectors
reproduce on a current CLI — it already refuses huge text files, truncates bash output and
downsizes images. Chasing why led somewhere more useful: `_find_cli` prefers the CLI
**bundled inside the SDK** over anything on `PATH`. So the harness version is not the one
the installer carefully installs; it is whatever `claude-agent-sdk` pins in
`requirements.txt`. A whole class of "why does the box behave differently from my laptop"
lives in that gap, and we had been reasoning about the wrong binary — including, one commit
earlier, in this very repository's notes about DAT-203.

### A Note on Where All This Stands

The whole arc above — the graceful stop, derived readiness, intent through the model, the
warm signal, the presence heartbeat, and the foreground-command ceiling — is **merged, but
not yet released**. The agent
changes travel in an agent release, so a fleet box only gets them when that release is
published and the auto-upgrade picks it up. Until then the code is true of the repository
and not yet true of any sandbox, which is a distinction this project has learned to state
out loud: a fix that exists only in `main` has the same effect on a user as a fix that
does not exist.

### Bootstrap: How the Agent Learns Who It Is

A freshly-launched agent is a blank slate. It doesn't know its username, it doesn't have the key to decrypt its own credentials, and it has no Anthropic key to talk to Claude. So it doesn't pretend otherwise — it boots into an **awaiting-bootstrap** holding state. In that state exactly two endpoints answer: `GET /health` (so accounts can see it's alive and not yet bootstrapped) and `POST /bootstrap`. Every user-facing endpoint returns HTTP 503, enforced by a single `require_bootstrapped` FastAPI dependency. Nothing runs against a `None` identity.

The accounts service drives it out of that state. Once the instance is reachable, accounts calls `POST /bootstrap` with an **accounts-signed JWT** in the `Authorization: Bearer` header (`purpose=agent-bootstrap`, verified against the accounts JWKS). The token carries two claims:

- `user_id` — the agent's identity from this moment on.
- `creds_key` — the Fernet key for the encrypted credentials store. It is `base64url(HMAC-SHA256(K_master, username))`, where `K_master` is an accounts-side secret the agent never sees. The agent receives the *derived* key, not the master secret.

The handler configures auth, opens the credentials store with `creds_key`, syncs the Anthropic key, exports the rest of the stored credentials into the process environment (`_apply_credentials_env()`, so the CLI's `${VAR}` substitution resolves), and leaves the holding state. It's idempotent for the same user — accounts re-pushes after a restart and the agent just re-binds to the same identity — but a bootstrap for a *different* user is rejected with a 409. An agent is one user's agent for life.

Why a push instead of the agent reading its own EC2 `Name` tag from instance metadata (the old model)? Two reasons. First, it keeps accounts as the single source of truth — the same design principle that runs through the whole sandbox plane: the agent *receives*, it never *asks*. Second, it lets accounts hand the agent its credentials-store key without that key ever being derivable from anything on the instance itself. A leaked EBS snapshot is just an encrypted blob; the key lives only in accounts and in the running process's memory.

### Credentials Management

User credentials (data provider API keys, broker credentials, and the Anthropic key itself) live in an encrypted on-disk store, opened at bootstrap with the delivered `creds_key`. Updates flow through one channel:

- **The accounts push**: The frontend's Settings modal writes to the accounts service; accounts then pushes each changed value to the agent via `POST /v1/credentials/update` (body `{provider, value}`). The store auto-persists on write. The old direct-write `POST /v1/credentials` endpoint is gone — it returns 410 Gone with a pointer to accounts, so any stale caller fails loudly instead of writing values the next push would clobber.
- **Local-dev seed**: For local development, env vars (`DATAFYE_AGENT_*`) seed the store the first time it is created. In production the store starts empty and accounts fills it in.

The agent's system prompt is rebuilt on every chat request, so credential changes are immediately reflected in what the agent tells the user it can do.

**Credentials have to escape the agent's memory to be useful.** A subtle but important detail: storing a credential in the encrypted store isn't enough. The Datafye CLI provisions environments from YAML deployment descriptors that use shell-style substitution — `polygon_api_key: ${POLYGON_API_KEY}`. The CLI is a *subprocess* the agent spawns, and it reads those variables from the process environment. If the values only live in the agent's in-memory store, the CLI sees blank substitutions and provisioning silently produces a dataset with no API key.

So `_apply_credentials_env()` walks the store and exports every data-provider, broker, and GitHub credential into `os.environ` — and it does this on bootstrap *and* after every `/v1/credentials/update` push, so a key the user adds mid-session takes effect on the next provision without a restart. Two wrinkles worth knowing:

- **Historical renames are exported under both names.** Polygon became Massive; Palpha became Precision Alpha. A descriptor in the wild might reference either, so the store key `massive_api_key` is exported as *both* `POLYGON_API_KEY` and `MASSIVE_API_KEY`, and `palpha_api_key` as both `PALPHA_API_KEY` and `PRECISION_ALPHA_API_KEY`. The map lives in `_CREDENTIAL_ENV_MAP`.
- **Unset means unset.** When a credential is absent from the store, `_apply_credentials_env()` *pops* its env vars rather than leaving stale values behind — so revoking a key in Settings actually de-provisions it from the next CLI run.

This generalises the trick the Anthropic key already used (`_apply_anthropic_key()`); the Anthropic key stays on its own path because it additionally *validates* against the Anthropic API, which the others don't.

**ConnectTrade credentials are a special case.** Most credentials are symmetric — one key, one user, done. ConnectTrade has two layers: a *client* identity (who Datafye is, as a ConnectTrade tenant) and a *user* identity (who this particular sandbox's human owner is inside that tenant). They have very different lifetimes:

- **Client creds** (`client_id` / `client_secret`): the same for every sandbox. **Accounts is now the source** — it pushes them into the encrypted store (`connecttrade_client_id`/`connecttrade_client_secret`) right after bootstrap, exactly like the platform Anthropic key (see datafye-accounts `pushPlatformConnecttradeClient`). The `DATAFYE_AGENT_CONNECTTRADE_CLIENT_ID/_SECRET` env vars are now a *local-dev seed only*, so nothing has to be baked into AMIs or shipped in env files.
- **User creds** (`user_id` / `user_secret`): lazy-minted on the first `POST /v1/broker/connections` call by hitting ConnectTrade's `POST /users`, persisted in the encrypted store, **and written back to accounts** so accounts holds the full set. `_write_back_user_creds` does a best-effort `PUT /accounts/{username}/credentials/{provider}` for each, forwarding the caller's JWT (a self-scoped write — the same agent→accounts pattern the usage reporter uses, no new credential). It's non-fatal: the minted creds already work locally, and a failed write self-heals — accounts' drift-reconcile re-pushes on the next sync, and the next mint after a sandbox rebuild rewrites them. This closes the old "orphaned ConnectTrade user after a sandbox rebuild" gap: because accounts now holds the user creds, a rebuilt sandbox gets them back by the ordinary credential push.

The broker module binds the same shared `credentials` dict that the rest of the service uses, so env-provided creds, accounts pushes via `/v1/credentials/update`, and lazy-provisioned user creds all converge in one place.

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

A dropdown can't capture "I want to use daily OHLC and EMA data for US tech stocks, plus some alternative sentiment data." The agent can. It understands intent, maps it to the right datasets and schemas, and applies them to the running foundry — all in one conversation. This is particularly important for users who don't know what datasets exist or what schemas they need.

## The Algo Development Flow

Here's what a typical session looks like from the agent's perspective:

1. **User describes idea**: "I want to build a mean-reversion strategy on AAPL and MSFT"
2. **Agent determines data needs**: SIP dataset, ohlc-1d and sma-1d schemas, symbols AAPL and MSFT
3. **Agent checks credentials**: Massive API key configured? Yes → proceed. No → "Please add your Massive API key in Settings"
4. **Agent builds descriptor**: Creates a YAML deployment descriptor
5. **Agent applies the dataset**: The sandbox boots a **pre-provisioned empty foundry** (API + MCP up, no datasets), so the agent ADDS its dataset to the running foundry (`datafye foundry local dataset add` / `apply`) rather than running `provision` — a fresh `provision` collides with the already-running platform and fails (DAT-93). Only **one dataset at a time**: switch datasets with `dataset remove`/`dataset add`, not deprovision+reprovision
6. **Agent writes algo code**: Creates Python files in the workspace
7. **Agent tests**: Downloads historical data, runs the algo, collects results
8. **Agent presents results**: Returns, win rate, trade count — the agent also renders these inline in the conversation as a **scorecard table** (for both Backtest and Validate/paper results), and the frontend shows them in the scorecard panel and charts
9. **Iteration**: User says "try a shorter lookback period" → agent modifies and re-tests
10. **Simulated trading**: If broker is configured, the agent switches the running environment to a trading (paper) configuration via `apply` (cross-mode `--morph`), not a fresh provision

### Sizing the box before you fetch (the foundry resource guard)

A foundry runs on a fixed-size sandbox, and a historical fetch or tick replay can be enormous — a day of combined trades-and-quotes ticks for a liquid name is gigabytes. Two failure modes hurt us. The gentle one: the fetch is bigger than the box's RAM or disk, so it thrashes or fills the disk. The nasty one: the history service runs with a **fixed 2 GB heap** (`-Xmx2g`), and a combined-ticks one-day buffer over roughly **1.3 GB OOMs that heap and writes zero data** — the job appears to run, then leaves you with nothing, and *resizing the box does not help* because the heap is fixed regardless of instance size.

So the prompt carries a **RESOURCE GUARD** block. Before any sizeable fetch/replay the agent must estimate the worst case (a high-volume trading day, not an average one) for both peak memory and disk, read the box's real limits with `free -m` and `df -h`, and only proceed if it fits with headroom (peak under ~70% of RAM, at least ~5 GB disk free). If it won't fit, the agent **stops and asks the user to resize to a named instance size first** rather than launching a job that will fail. And it knows the hard OOM rule as a special case: for the combined-ticks-over-1.3 GB situation, resizing is *not* the answer — the fix is to fetch trades and quotes separately, or split the symbol set.

The empirical numbers behind all this — per-symbol-per-day byte rates, the sizing formula, an instance-size map, and worked examples — don't belong inline in the prompt (they'd bloat every turn and go stale). Instead they live in a **bundled cheat sheet**, `reference/foundry-resource-cost-cheatsheet.md`, that ships with the app clone. `main.py` computes `CHEATSHEET_PATH` and passes it to `build_system_prompt(cheatsheet_path=...)`; the guard block just tells the agent to read that file on demand when it needs the actual figures. The numbers were measured empirically against a real Yukti project (foundry 2.0.28, 2026-07-17), and the cheat sheet notes to re-measure if the `-Xmx2g` heap or the foundry version changes. The lesson: encode the *policy* ("estimate, check, ask before you overflow") in the always-on prompt, but push the *data* it reasons over into a versioned reference the agent reads only when it's actually sizing a fetch.

### The lifecycle stepper is intent-aware (the agent owns it)

The workspace shows a progress stepper for the project, but there is **no single
global pipeline**. We used to ship one hard-coded six-stage list
(`Idea→Design→Build→Backtest→Validate→Deploy`) and force every project through it —
which is nonsense for a project that's just a dashboard or a quick research chat. A
dashboard has nothing to backtest; a "what does this indicator mean?" chat has no
build at all.

So the lifecycle became **per-project and agent-driven**. After each turn,
`classify_lifecycle()` (a cheap haiku call) reads the conversation and returns
`{intent, track, stage}`: it infers the project's *intent* (algo, signal,
dashboard, app, tool, chat, research…), maps that to a *track* — the ordered list
of stages that intent actually goes through — and reports which *stage* the turn
landed in. The frontend just renders whatever track it's handed.

The built-in tracks (`conversations.py`):
- **algo / signal** → `Explore → Design → Build → Backtest → Validate → Deploy`
  (for a signal, "Deploy" means *publish* rather than go-live trade)
- **dashboard / app / tool** → `Explore → Design → Build → Ship`
- **chat / research** → no stepper at all

The first stage is **Explore** (renamed from "Idea"). The intent vocabulary is
*open*: faced with a novel build intent the agent can compose its own track around
the common `Explore → Design → Build` spine plus an artifact-appropriate tail. The
project record now persists `intent` + `track` next to `stage`/`maxStage`, and both
the `stage` SSE event and `/history` replay carry them so the frontend always knows
which steps to draw. The old `STAGES` constant survives only as a back-compat alias
for the algo track. The lesson: a "lifecycle" that's right for one artifact type is
wrong for the next — let the agent that understands the project decide its shape,
and keep the UI a dumb renderer of whatever it's told.

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
- **Hosted**: Pre-baked AMI in a Rumi private cloud. Each user gets a sandbox instance at `{username}.app.datafye.io`, proxied through a jump server with wildcard SSL. Managed by the datafye-accounts service (elastic stop/start based on activity). The AMI carries no user-specific data — identity, credentials, and the Anthropic key are all delivered at runtime by accounts over HTTP (`POST /bootstrap` and `POST /v1/credentials/update`).
- **Standalone (Marketplace)**: Minimal AMI with a first-boot script. User provides DNS via EC2 user data; everything downloads and installs on first boot.

The installer no longer takes an `--anthropic-key` flag, and `first-boot.sh` no longer reads an Anthropic key out of EC2 user data. The Anthropic key now arrives the same way every other credential does — over the accounts credentials channel — for *both* hosted and standalone. That collapses what used to be two key-delivery paths into one and lets the installer do something simpler: it just **always starts the agent**, which boots into the awaiting-bootstrap holding state and waits for accounts to push it identity and credentials. There's no longer any "do we have a key to start with?" branch in the install flow. (`pyyaml` was added to `requirements.txt` to parse the deployment descriptor for `env_status`.)

The agent source is open source — the value is in the Datafye platform, not the glue code. Power users can fork and customize the prompt, add tools, tweak behavior.

## Receive-Only Integration with `datafye-accounts`

The agent is a **receive-only worker** in the Datafye sandbox plane. The shape of its role: it **receives** credentials and JWTs, it **does** work, it never **asks** accounts for anything. Accounts is the only writer in the relationship. This section captures how that integration is wired.

### Identity bootstrap — by push, not by metadata

An earlier design had the agent read its own EC2 `Name` tag from the Instance Metadata Service at startup and derive its credentials-store key from the EC2 instance ID. That was scrapped. The agent no longer touches AWS metadata at all — there is no `identity.py`.

Instead, identity arrives by **push** (see [Bootstrap: How the Agent Learns Who It Is](#bootstrap-how-the-agent-learns-who-it-is) above). Accounts calls `POST /bootstrap` with a signed JWT carrying `user_id` and `creds_key`. The agent has zero identity until that call lands.

The push model is strictly better than reading IMDS:

- **The credentials-store key never lives on the instance.** With the old instance-ID-derived key, anyone with the instance ID could reconstruct the key. The new key is `HMAC-SHA256(K_master, username)` — `K_master` is an accounts-side secret, so the key is *only* derivable inside accounts. The agent gets the finished key over an authenticated channel and holds it in memory.
- **No "refuse to start" failure mode.** The old agent crashed if the `Name` tag was missing. The new agent always starts; if no bootstrap has arrived it simply sits in the holding state answering `/health` — which is exactly what accounts needs in order to know it should push.
- **One trust anchor.** Identity, the credentials-store key, and every later credential all flow through the same accounts-signed channel. There's no second mechanism (IMDS) to reason about or secure.

### Encrypted on-disk credentials store

The agent doesn't have a Rumi store (it's Python/FastAPI, not a Rumi application). Credentials live in a single binary file:

- Path: `~/.datafye/agent/credentials.bin` (mode 0600)
- Format: msgpack
- Encryption: `cryptography.fernet`. The Fernet key is the `creds_key` delivered in the bootstrap push — `base64url(HMAC-SHA256(K_master, username))`. It is held in memory only and never persisted to disk.
- Threat model: defends against casual filesystem inspection and against leaked EBS snapshots (the snapshot is an opaque encrypted blob; the key isn't on it and can't be derived from it). Does not defend against an attacker with shell on the running instance — at that point they can read process memory anyway. That's acceptable; encryption-at-rest is one layer of many.
- Replaces the old `~/.datafye/agent/broker_user.json` plain-JSON file. On first load the store migrates any existing ConnectTrade user creds out of that file and deletes it.
- A `generation` value (a short deterministic hash of the contents) is computed on load and on every write, and exposed in `/health`.

### Endpoint shape

- **`POST /bootstrap`** — accounts-only. Establishes identity + credentials-store key from a signed JWT. Idempotent for the same user, 409 on rebind.
- **`POST /v1/credentials/update`** — accounts-only push, body `{provider, value}`. Updates the in-memory store and persists to `credentials.bin` atomically. Returns 204. A push to `anthropic_api_key` also re-syncs and re-validates the Anthropic key.
- **`GET /health`** — reports `bootstrapped`, `anthropic_key_status`, `credentials_generation`, `last_chat_activity_at`, `running_jobs`, `active_proxied_apps`. `username` and `credentials_generation` are `null` until bootstrapped. Accounts polls this for idle detection and cache-loss recovery (if `credentials_generation` drifts from what accounts last pushed, accounts re-pushes everything — see the [datafye-accounts PROJECT.md](../datafye-accounts/PROJECT.md) idle-monitor section). `last_chat_activity_at` is **seeded to boot time, not 0**: the accounts idle-monitor treats `0` as "never active" and skips it, so a provisioned-but-never-chatted agent would otherwise live forever; seeding to boot makes its idle clock start ticking immediately.
- **`POST /v1/credentials`** — removed; returns 410 Gone. The frontend no longer writes credentials directly to the agent; all writes go through accounts.
- **`/v1/conversations*`** — the agent's chat-layer conversation store (history replay via `GET /v1/conversations/{id}/history`). The id-minting `POST`/list `GET` are legacy/unused now that accounts is the authoritative project registry; the agent materialises a local record for an accounts-minted id via `conversations.ensure()` on the first chat turn. `DELETE /v1/conversations/{id}` permanently removes the strategy's agent-side folder via `conversations.delete()` (204, or 404 if the agent never materialised it) — guarded by a path-safety check that refuses to `rmtree` anything resolving outside the strategies base, so a malformed or hostile id like `..` can't escape. Accounts deletes its own project-registry record separately.
- **JWT validation** on `/v1/chat`, `/v1/credentials/status`, and `/v1/broker/*`: verify the accounts-signed JWT against accounts' JWKS and check `sub == the agent's bootstrapped username`. Reject otherwise.

**A startup route guard keeps a broken agent from looking healthy.** A module-level check at the bottom of `main.py` walks `app.routes` and asserts that the load-bearing trio — `GET /health`, `POST /bootstrap`, `POST /v1/chat` — are all registered, raising `RuntimeError` at import if any is missing. The failure mode it defends against is subtle: if a mis-applied edit clobbered a route's decorator, the agent would still boot and serve `/health` 200 while silently 404'ing `/bootstrap`, so accounts' idle-monitor would see "Running" and never realise the agent can't actually be bootstrapped. Crashing loudly at boot turns that invisible degradation into an obvious failure.

**Clock skew bit us at bootstrap.** The very first JWT an agent ever sees is the bootstrap token, minted by accounts moments before. If the accounts host's clock runs even a few seconds ahead of the agent's, that token's `iat` (issued-at) lands in the agent's *future*, and PyJWT rejects it with "token is not yet valid (iat)" — a 401 that makes a perfectly correct bootstrap fail intermittently and undebuggably. The fix is to pass `leeway=_CLOCK_SKEW_LEEWAY_SECONDS` (default 60s, env `DATAFYE_AGENT_JWT_LEEWAY_SECONDS`) to *both* `jwt.decode` calls in `auth.py` (`verify_bootstrap_token` and `require_self_jwt`), so small clock differences on any time-based claim (iat/nbf/exp) are tolerated. Lesson: any time you verify a freshly-minted token on a *different* host from the one that signed it, budget for clock skew — distributed clocks are never exactly equal.

### Why this shape

- **No agent → accounts calls** means no shared secret to bootstrap a request, no IAM-signing layer, no fallback mode when JWTs aren't available. Background tasks use the cached credentials, kept fresh by accounts' push plus the generation-counter recovery mechanism.
- **The agent never sees its own credentials before they're pushed.** Even on first boot the store is empty until accounts (driven by the user's Settings activity) pushes values in. That's by design — accounts is the source of truth.
- **No "stale cache + no user connected" race.** Credentials only change when the user takes action; the user is by definition online during that action; the push lands on the live agent before the user disconnects.

### Smaller follow-ups already on the list

- SDK-based algos (Java / Datafye SDK path alongside Python)
- Live trading (currently capped at simulated/paper)
