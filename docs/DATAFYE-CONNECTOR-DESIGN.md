# Datafye as a Claude connector — design

Status: **designed, not built** (2026-10-23). Tracker: **DAT-418** (team Datafye, GTM
project). Post-Release-1; the build phases in §8 become their own tickets once committed
to.

Companion to **`nvx-sutra/docs/SUTRA-CONNECTOR-DESIGN.md`** (SUT-77), which was written
first. That document is the authority on the parts that are **architecture** and therefore
identical here; this one is deliberately not a transposition of it, because the reasoning
that produced Sutra's central decision does **not** reach the same place on this box.

Read §2 first if you already know the Sutra design. Onboarding is out of scope: everything
below assumes the caller has a Datafye account and a provisioned agent box.

---

## 1. Why

Datafye reaches people who came to Yukti. A connector reaches people already working in
Claude Code, Cowork or claude.ai who have a market-data or strategy question, and puts
Datafye in the consideration set at the moment the problem calls for it.

The same honest framing as Sutra's applies: a connector is discovered by people who
install it, not by everyone, and the positional consequence is the same trade. **Claude
becomes the surface and Datafye becomes infrastructure** — datasets, a provisioned
environment, a backtest harness, and a broker path. That is the right way round, because
the agent is the replaceable half and the data and the environment are not.

The audience argument is, if anything, stronger here than for Sutra. A Cowork user cannot
provision a market-data environment or run a backtest over real history by any other
route, and unlike app-building there is no plausible local substitute they could improvise.

---

## 2. What is different from Sutra, and it is not cosmetic

The boxes are near-identical. Verified: the same file layout, the same `INTERNAL_TOOLS`
(`Read/Write/Edit/MultiEdit/Glob/Grep/LS/Bash`, no `Task`, `bypassPermissions`), the same
bootstrap/credentials/JWKS model, the same three memory scopes and three skill tiers, the
same Claude Code CLI on the box, the same `/health` shape down to `running_jobs` and
`active_proxied_apps`, the same 12/14-step installer with auto-upgrade.

So everything in the Sutra design that is *architecture* ports unchanged, and §4 below
says so rather than repeating it.

**But the reason Sutra keeps work on the box does not hold here.**

Sutra's central argument is that the iteration loop is **toolchain** judgment: build, read
the compile error, fix, rebuild. That has to be local, because the errors are local, the
`~/.m2` is local, and shipping a Maven log across the wire to be reasoned about is the
worst of every option.

Datafye's loop is **strategy → backtest → scorecard → adjust**. The `backtest-strategy`
skill ends, in its own words, by inviting iteration: "try a shorter lookback", "test on a
different period", "add a stop-loss". That is **domain** judgment, not toolchain judgment
— is this strategy any good? — and under the Sutra design's own rule, domain judgment
belongs to the client model, which has the user's context and the conversation.

**Consequence: Datafye wants a thinner connector than Sutra.** Provisioning and
backtesting are jobs, because they are long and measurable. The strategy design, and the
read-the-results-and-adjust loop, sit client-side. Anyone porting the Sutra document
mechanically would move the iteration loop onto this box for a reason that does not apply
to it.

Two smaller inversions worth stating:

- **The credential-risk argument is stronger here, not weaker.** For Sutra the metering
  gateway softened it: the box will hold a revocable per-user token rather than the
  Anthropic key. This box holds **broker credentials** (`connecttrade` client and user
  secrets) plus market-data provider keys (Massive, HWAI) plus GitHub. A broker credential
  is money. So the rule against exposing `Bash` holds more firmly, and §7 adds a mode
  boundary Sutra has no equivalent of.
- **The payoff is data, not a URL.** Sutra's connector hands back a preview link. Here the
  deliverable is the scorecard, returned through the tool as structured data the client
  model can reason about and the user can see rendered in their own client. Simpler, and
  it removes the handoff question that §7 of the Sutra doc spends a section on.

---

## 3. What is on the box

Verified against the installer, `/health`, `prompt.py` and the system skills on
`main` (2026-10-23).

**Environment and toolchain**
- Docker, for the Datafye **foundry**: a per-box platform environment, provisioned from a
  YAML deployment descriptor of datasets, schemas, symbols and a mode
- **Datafye CLI**, which provisions, upgrades, tears down and reports foundry status
- Claude Code CLI (installed as the `datafye` user), Python with a pre-installed package
  set for project code
- Datafye docs at `/opt/datafye/docs`, plus samples. ✅ The same content is also served by
  GitBook's hosted MCP at `docs.datafye.io/~gitbook/mcp` (§5), which is what the connector
  uses; the on-disk copy is for the agent and for self-host
- User workspace at `/home/datafye/workspace`; a project is a **folder** under
  `<state>/projects/<id>/` carrying `meta.json`, `CLAUDE.md`, `PROJECT.md`, `memory/` and
  `.claude/skills/`, and that folder is the turn's cwd

**The platform interface**
- **`datafye-api` MCP server over HTTP** (`DATAFYE_API_MCP_URL`), described in `main.py` as
  the primary interface to the running deployment. This is the structural analogue of
  Sutra's Dev MCP and behaves nothing like it: Sutra's **edits an application model**,
  this one **talks to a live platform**. There is no `model.*` equivalent here.
- `/health` reports `api_mcp_available` and a `foundry` state block

**Datasets and credentials**
- SIP (US equities) and Crypto via a **Massive** key; **HWAI** via its own key;
  **Synthetic**; and **Palpha** (Precision Alpha), which takes no user key at all — it is
  an account-level AWS role configured by Datafye Ops, so it is never "blocked on a
  credential" and must not be reported as such
- Broker: **`connecttrade`** (client id/secret, user id/secret), plus GitHub

**Knowledge**
- Three system skills: `provision-environment`, `backtest-strategy`, `author-skill`
- Fleet memory: `diagnosing-the-environment.md`, `platform-gotchas.md`,
  `sizing-and-resource-cost.md`, plus `reference/foundry-resource-cost-cheatsheet.md`

**App preview**
- Apps the model builds bind the band **10010-10019** and are served at
  `https://<username>.app.datafye.io:<port>`, the jump server routing that band straight
  through. ⚠️ The band is deliberately **not** Sutra's 8080-8089: the Datafye platform
  occupies the 80xx space (`rumi-solace` publishes 8080 and `rumi-influxdb` 8086 on every
  box, empty foundry or not), so the first port of an 80xx band could never bind (DAT-220).

Two properties of how this box is kept honest are worth copying rather than re-deriving:
`prompt.py` reads the pre-installed package list **from the same file the installer
installs from** (DAT-210) so the prompt cannot drift from the box; and fleet memory carries
a resource-cost cheatsheet because sizing here has a real bill attached.

---

## 4. Architecture

Identical to `SUTRA-CONNECTOR-DESIGN.md` §4 in every structural respect, and that document
is the authority. In summary, and without re-arguing it:

- **One stable gateway URL on 443**, OAuth against Datafye accounts, resolving caller →
  sandbox record → hostname, waking a dormant box before proxying.
- **443 is a reachability requirement, not a convention.** Claude Code dials from the
  user's machine; a Desktop or claude.ai **connector dials from Anthropic's
  infrastructure, which reaches 443 only**. A gateway on any other port serves Claude Code
  and nothing else, and no per-user IP allowlisting is possible for the connector case.
- **Build the gateway against `nvx-sutra-agent/fleet_memory/mcp-server-on-rumi.md`**, which
  is a spec-checked recipe (MCP 2025-06-18 Streamable HTTP) proven by a real Claude Desktop
  handshake. It is protocol knowledge, not Rumi knowledge, so it applies here verbatim:
  `202` for notification-only POSTs, `@Produces(WILDCARD)` to avoid a pre-method 406,
  `-32602` for an unknown tool, checking `MCP-Protocol-Version` only after `initialize`,
  CORS exposing `Mcp-Session-Id`, and installing a request log before it is needed.
- The proxy must pass `X-Forwarded-Proto` / `-Host` / `-Port` so OAuth protected-resource
  metadata built from the request advertises the 443 URL.

### ⚠️ The gap this review found: Datafye previews are unreachable from a connector

`https://<username>.app.datafye.io:<port>` is **port-bearing**, which is precisely the form
SUT-146 established a Claude connector cannot reach. Two consequences, and the first is
independent of this whole design:

1. **A Datafye user who builds an MCP server or a dashboard today cannot add it to Claude
   as a connector**, and the failure looks like the server being down rather than
   unreachable. Sutra fixed this in SUT-146 by adding a 443 hostname
   (`<user>-<port>.sutra.n5corp.ai`) alongside the port form; the equivalent here would be
   `<user>-<port>.app.datafye.io` with the regex matching **10010-10019**, not Sutra's
   `808[0-9]`. Worth its own ticket regardless of whether the Datafye platform connector is
   built, and it is the cheaper and more immediately useful of the two.
2. There is **no MCP-server recipe in this repo's fleet memory**. Sutra's
   `mcp-server-on-rumi.md` was earned over several round trips; a Datafye box asked to
   build an MCP server would pay for that learning again. The protocol half ports as-is.

---

## 5. Tool surface

Workspace altitude. Every result compacted server-side; no raw harness output or full
tick dumps across the wire.

**Knowledge and orientation**
- `docs.ask(question)` / `docs.search(query)` / `docs.read(url, full?)` — ✅ **a thin
  wrapper over GitBook's own hosted MCP**, not a search engine of ours. See below.
- `bom()` — pinned versions, capability flags, agent version; also the skew handshake
- `env.status()` — the `foundry` block from `/health`: what is provisioned, what is
  answering, what is flowing

**Projects**
- `projects.list()` / `projects.create(name)` / `projects.get(id)`
- Backed by the accounts registry, so a project created here appears in Yukti

**Data and platform** — thin proxies over `datafye-api`
- `data.*` — the historical bars/ticks a strategy consumes, dataset and schema discovery,
  symbol lookup
- ⚠️ As with Sutra's Dev MCP, this travels **gateway → the box → the platform's MCP
  endpoint**. Do not expose the platform's own ports publicly to shorten the hop.

**Files** — project-folder scoped
- `files.read`, `files.glob`, `files.grep`, `files.write`, `files.edit`

**Jobs** — the delegated path
- `jobs.start(project_id, kind, spec)` → `{job_id}`; `jobs.poll(job_id, after_seq)`;
  `jobs.stop(job_id)`

| Kind | Agentic? | Notes |
|---|---|---|
| `provision_environment` | Partly | Descriptor-building is judgment; the provision itself is the CLI. Long enough to need the job interface. |
| `backtest` | **No, and that is the point** | Fetch history, run the algo, compute the scorecard. Deterministic given a spec. The *judgment* about the result belongs to the client. |
| `teardown_environment` | No | CLI. |

**No `deploy` kind yet.** Paper and live trading are a separate decision; see §7.

### ✅ Docs: wrap GitBook's MCP, and Datafye's has a tool Rumi's does not

**Probed 2026-10-23.** GitBook hosts an MCP server per docs site, and
`https://docs.datafye.io/~gitbook/mcp` is **live, public, unauthenticated, stateless and
spec-correct** ("Datafye Docs MCP Server" v0.27.2, protocol 2025-06-18). So the docs tools
are a capping wrapper over someone else's index rather than construction of ours.

⚠️ **The two sites are not configured the same, and Datafye has the better end of it.**

| | Rumi (`docs.rumi.systems`) | Datafye (`docs.datafye.io`) |
|---|---|---|
| `searchDocumentation` | yes | yes |
| `getPage` | yes | yes |
| `sendFeedback` | yes | yes |
| **`askQuestion`** | **no** | **yes** |

`askQuestion(question, goal)` returns a **synthesized answer with links to its source
pages**. Measured against the real thing: *"What datasets and schemas can a Datafye
environment be provisioned with?"* came back in **672 tokens**, correct and complete —
datasets, the per-dataset schema matrix, the Palpha same-deployment requirement, and which
providers are marked work-in-progress. The equivalent `searchDocumentation` call cost
**2,678 tokens** of ten page excerpts the client model then has to collate itself.

So on this product **`docs.ask` is the primary docs tool**, roughly four times cheaper than
search for anything question-shaped and pre-collated, with `docs.search` / `docs.read` as
the browse path behind it. That is a better position than the Sutra connector has.

Two cautions, because a synthesized answer is not the same kind of thing as a page:

- **It can be confidently wrong in a way an excerpt cannot.** It is a model call on
  GitBook's side. Good for orientation; verify specifics against `docs.read` whenever the
  answer feeds something the platform will validate.
- **It has its own latency and its own failure modes**, and it is a third-party dependency
  on the critical path of every docs question.

**Sizing, for the record.** Datafye's `searchDocumentation` responses measured 10,715 and
12,535 chars (~2.7k and ~3.1k tokens) with the largest single result at 39-46% — more even
than Rumi's, which has a 94 KB configuration reference that dominated one response at 84%.
Cap per-result content **by default, never as a gate**: `full: true` must be available,
because the client's tokens ride the user's own subscription rather than our bill, and the
real constraints are token-denominated rate limits and compaction timing.

**Worth raising with whoever owns the Rumi space:** `askQuestion` appears to be a per-site
GitBook setting, since both servers run v0.27.2. Enabling it on `docs.rumi.systems` would
make the Sutra connector's docs path four times cheaper and pre-synthesized, for free.

**The box keeps its on-disk docs regardless.** The agent's prompt grounds in
`/opt/datafye/docs`, and a self-hosted box cannot depend on GitBook being up.

### Why `backtest` is a job but not an agentic one

This is the sharpest departure from Sutra. A backtest is long, measurable, and
deterministic once the window, universe and strategy are fixed. It needs to be a job
because an MCP call cannot be held open for it, **not** because it needs a model on the
box. So its spec should be explicit — date range, symbols, strategy entry point — and its
result should be the scorecard as structured data, with the trade log available but not
inlined.

That makes the job envelope here **narrower and more valuable** than Sutra's
`build_and_validate`: there is a real schema to agree, rather than "iterate until it
compiles". Spend the design effort on that schema.

### The narrow prompt

The same conclusion as Sutra's §2 reaches here by a shorter path. If no job kind is
agentic beyond descriptor-building, then `prompt.py` — audience and voice, the narration
contract, the stage machinery — is **not on the connector path at all**. Connector jobs
should run with a minimal prompt scoped to the toolchain and the known failure modes, or
in the `backtest` case arguably with no model in the loop whatsoever.

⚠️ **Decide this before building the job runner**, because "reuse the agent's turn path"
is the default that will happen by itself, and it is the wrong one here.

---

## 6. Walkthrough: a first backtest

> *"Test a 20/50 moving-average crossover on AAPL over the last year and show me how it
> would have done."*

The user is in Claude Code with the plugin installed and the connector authenticated.

**0 — Skill activation.** The `datafye-strategy` skill fires on description match: the
prompt names historical testing, which is the `backtest-strategy` trigger vocabulary.
Loads the strategy conventions, the dataset and schema vocabulary, and the honesty rules.

**1 — Project.** `projects.create(name: "ma-crossover-aapl")` → `{project_id, folder}`,
registered in accounts so it shows up in Yukti.

**2 — What does this need?** The client model decides: SIP daily bars, `ohlc-1d`, symbol
`AAPL`, a one-year window, `backtest` mode (foundry, no broker).

**3 — Check the environment before provisioning one.** `env.status()`. If a foundry with
SIP `ohlc-1d` and AAPL already exists, skip to step 5. ⚠️ This check is the whole reason
`env.status()` is a tool and not part of a job: provisioning when you did not need to costs
the user real money, and the cost cheatsheet on the box exists because this matters.

**4 — Provision, as a job.**

```
jobs.start(project_id, kind: "provision_environment",
           spec: {datasets: ["sip"], schemas: ["ohlc-1d"], symbols: ["AAPL"],
                  mode: "backtest"})  → {job_id}
jobs.poll(job_id, after_seq: 0) → events…
```

⚠️ Credentials are **never** part of the spec. The descriptor references `${MASSIVE_API_KEY}`
and the box exports what the user configured. A connector that accepted a key in a tool
argument would put it in the client's transcript, which is exactly wrong.

If the Massive key is not configured, the job fails with a **user-actionable** result:
add it in Yukti's Settings. Not "paste it here".

**5 — Write the strategy.** `files.write(...)` into the project folder: a short Python
algo with the two lookbacks and a crossover rule. The client model does this, with the
user's own conventions if they have any in the repo they are sitting in.

**6 — Backtest, as a job.**

```
jobs.start(project_id, kind: "backtest",
           spec: {entry: "strategy.py", symbols: ["AAPL"],
                  from: "2025-10-01", to: "2026-10-01"})  → {job_id}
```

The box fetches the history through `datafye-api`, runs the algo, captures every simulated
trade, and computes the scorecard: total return, win rate, trade count, largest win and
loss, max drawdown. The result is that structure; the trade log is addressable but not
inlined.

**7 — The client reasons about the result, and this is the division working.** The scorecard
comes back as data. The client model — which has the conversation, the user's risk
appetite, and whatever else is in their repo — says what it means and proposes the next
variant. Then step 6 again with a different spec. **No model on the box took part in that
loop**, and that is the design, not an omission.

**8 — Honesty is a skill obligation, not a tool guarantee.** The skill's rules travel to
the client: report what the data supports, flag small samples, never overstate, and note
that historical results do not guarantee future performance. ⚠️ This is the one place the
weaker enforcement of a skill versus a system prompt genuinely bites, because the failure
mode is a confident number rather than a broken build. Call it out in §7.

Roughly 8-12 MCP calls plus polling, most of the cost in two jobs.

### What could go wrong in exactly this run

- **The skill does not fire**, and nothing else matters if so.
- **The model provisions without checking**, burning money on an environment that already
  existed.
- **It treats Palpha as credential-blocked** if the user asks for it. There is no Palpha
  key; the skill content has to carry that or the client model will invent a missing one.
- **It overstates a result** from a one-year, one-symbol sample. See above.
- **It tries to run the harness locally.** The skill must state that the box is the
  execution environment.

---

## 7. Risks and open questions

**⚠️ Paper and live trading are a hard boundary, and this is the one Sutra has no
equivalent of.** `provision-environment` supports `backtest` (foundry, no broker) and
`paper` (broker-connected). A connector that can stand up a broker-connected environment
is a different risk class from one that cannot, and the client model driving it is reading
the user's repo, which is an injection surface. **Recommendation: the connector provisions
`backtest` only in its first form**, and `paper` stays in Yukti where the user is present
and the consent is explicit. Live trading is not a connector capability at any phase.

**Honesty under weaker enforcement.** On the box, the reporting discipline is in the system
prompt. On the connector path it is skill content competing with the user's own
instructions. The failure mode is a plausible overstated number, which is worse than a
broken build because nobody notices. Mitigation: have the `backtest` job return the
scorecard **with its own caveats attached as data** (sample size, window, symbol count),
so the honesty does not depend solely on the client model honouring a skill.

**Cost and admission control.** Each provision has a bill. A client model that can start
jobs needs a per-user ceiling enforced server-side, not a polite instruction. Sutra's
build loop never needed this.

**Open: the preview 443 gap** (§4). Independent of this design and worth doing first.

**Open: where the plugin and skills live.** Sutra's monorepo gives them an obvious home
next to the agent. `datafye-agent` is its own repo and Yukti is another, so this needs a
decision: inside `datafye-agent`, or a separate published package.

**Open: OAuth mechanics and the directory submission path**, unchanged from SUT-77 and
unanswerable from inside the repo. The proxy-side half has a precedent in SUT-146.

**Open: whether `backtest` needs a model at all.** If the spec is explicit enough, the job
is a harness invocation. If it is not, the gap is the spec, not the need for an agent.
Settle this with a real run before building the job runner.

---

## 8. Phasing

1. **The preview 443 hostname** (`<user>-<port>.app.datafye.io`, band 10010-10019). Not
   part of the connector, useful on its own, and it unblocks a user adding their own built
   server to Claude.
2. **Skills only.** The strategy and environment vocabulary, the dataset and credential
   rules including the Palpha exception, and the reporting honesty rules. No server work,
   reaches Cowork immediately, and tests whether the model reaches for Datafye at all.
3. **Read-only connector.** `docs.*` (a wrapper, not a build — §5), `bom`, `env.status`,
   `projects.list`. Proves
   the gateway, OAuth and resolution with nothing destructive behind it.
4. **Data and files.** `data.*` over `datafye-api`, project-scoped file tools.
5. **Jobs.** `backtest` first, which is the valuable one and the one with a real schema to
   agree; `provision_environment` second, with the cost ceiling in place before it ships.

Phases 1 and 2 are each worth doing whether or not the rest proceeds.
