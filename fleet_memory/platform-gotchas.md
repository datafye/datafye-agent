# Platform gotchas and workarounds

Places where the platform does not yet behave the way you would reasonably
expect. Read this before working with a dataset or planning a fetch — each of
these has cost somebody a wasted run.

Every entry was re-verified against the source on 2026-08-10. Where a ticket has
since changed the behaviour, the entry says so rather than repeating folklore.

## Crypto

**Use bare tickers in a deployment descriptor: `BTCUSD`, never `X:BTCUSD`.**
The history services build their upstream call as `"X:" + symbol` with no strip
first, so a symbol that already carries the prefix becomes `X:X:BTCUSD` and
returns **zero data, with no error**.

The REST API is safer than it used to be: every crypto data endpoint
(history, backtest history, live aggregates, live ticks) now normalises inbound
symbols — strips a leading `X:`, drops `-` and `/`, uppercases — so
`X:BTCUSD`, `BTC-USD` and `btc/usd` all work there. That normalisation does
**not** reach the descriptor. Writing bare everywhere is the habit that cannot
go wrong.

**Crypto has no quotes at all.** The data provider supplies trades and
aggregates only. This is a source limitation, not a bug and not a
configuration mistake, so do not go looking for the setting that turns them on.
Asking for crypto quotes now returns a clear error saying so; it used to return
an empty result, which is why older notes describe it as "quotes come back
empty". Fetch trades, or combined ticks, instead.

**A crypto day is 24 hours**, not a market session. Windows and day counts mean
something different from stocks.

## Deploy one dataset at a time

Keep a **single** dataset (SIP, or Crypto, or Synthetic) deployed at once.
Multi-dataset environments are unreliable: they fail partway, often at the
crypto launch step, and can leave a broken environment behind.

To switch, remove the current dataset and add the new one:

```
datafye foundry local dataset remove <old>
datafye foundry local dataset add <new>
```

Do **not** deprovision and reprovision to change dataset, and do not list
several datasets in one descriptor. The platform fix for this is deferred, so it
is still true.

## A tick fetch can OOM and write nothing

The history service runs on a **fixed 2 GB heap on every instance size**. A
combined-ticks fetch whose one-day buffer exceeds roughly **1.3 GB** exhausts it
and writes **zero data**.

⚠️ **Resizing the box does not help** — the heap does not scale with the
instance. The only fixes are to make the fetch smaller:

- narrow the intraday window (`startTime` / `endTime`)
- fetch trades and quotes **separately** rather than combined
- split the symbol list
- use OHLC instead of ticks where the algo allows it

Estimate against a high-volume day, not an average one, and check `free -m` and
`df -h $(docker info --format '{{.DockerRootDir}}')` before starting -- fetched
history lands in a Docker volume, so the filesystem that fills is the one holding
Docker's data-root, which on a current sandbox is a separate data volume and not
`/`. The bundled foundry resource-cost cheat sheet has the per-symbol-day rates
and worked examples.

## Fetch semantics that surprise people

**`numDays` counts BACKWARDS from `date`.** `date` is the most recent day of the
window, so 2 days from `2025-01-02` gets you `01-02` and `01-01`.

**A fetch REPLACES the stored history** for that frequency rather than adding to
it. Fetch a whole window in one call; looping day by day leaves you with only
the last day.

**OHLC is UNADJUSTED by default (`adjusted=false`), and a split then reads as a
crash.** Netflix shows an apparent -90% move on 2025-11-17: a ten-for-one split, not a
price move. It would have wrecked every average in that analysis, and it was caught only
because somebody was hunting for outsized gaps.

Pass `adjusted: true` for anything computing a return across more than one day. Leave it
false only when you want as-traded prices deliberately -- reconciling a broker fill, or
replaying the tape as it happened. Nothing in the returned series marks where a corporate
action occurred, so treat any single-day move beyond roughly 50% as a suspected split
until you have checked it, and say so to the user rather than reporting the number.

## The Day bar is a SESSION bar, not a calendar-day accumulator

This is the single easiest thing to get backwards from the outside, because the
API returns a plausible bar either way and nothing in the response says which
one you are looking at.

**The day bar opens on the official opening print** -- the trade carrying the
`MarketCenterOfficialOpen` sale condition -- and its `open` is that print's
price. It does not open on the first trade of the calendar day, and it does not
open on the first trade you happen to replay.

**An aggregation service that never saw the opening print produces NO day bar
at all.** That is deliberate: a half-session bar would look authoritative and
would not be. So if you replay a window starting after 09:30, expect no day bar
rather than a partial one.

Two consequences for replay work:

- **Replay from before the open** if you want a day bar. Start at 04:00 (or any
  point before 09:30) so the opening print is inside your window.
- ⚠️ **A day bar whose `open` is NOT the official opening print is a BUG, not a
  semantic to work around.** If you replay 04:00->09:40 and get an open equal to
  the 04:00 pre-market print, the session-open signal did not fire and a bar was
  built from residual state. Report it; do not rewrite your analysis around it,
  and do not conclude the platform aggregates calendar days.

Once the official close (`MarketCenterOfficialClose`) is seen, later
extended-hours trades may still extend `high`, `low` and `volume`, but they must
NOT move `close`. Only a corrected consolidated close does. So a day bar that
keeps moving its close after 16:00 is also a bug.

The day bar can additionally be **sealed at configured intraday cutoffs** and
published as its own series, so a "day" bar carrying a mid-session timestamp is
expected rather than truncated data.

## Never drive Datafye services with Docker directly

To start or stop a Datafye **service** (the application inside a container), use the
Rumi admin scripts. To start or stop the **container**, use the Rumi
`LocalProvisioner`. Reaching for `docker` yourself is the last resort, not the
first, and usually the wrong tool entirely.

⚠️ **`docker restart rumi-<svc>` brings the container back with NO application
inside it.** The container runs a wrapper that does not relaunch the Rumi XVM on
container start, so `docker ps` shows the service "Up" while the application log
simply stops at the last line before the restart and nothing is serving. That is
the same wedged state a half-failed provision leaves -- containers healthy, API
answering nothing -- and it is easy to create by accident and hard to recognise
afterwards.

The repair is `datafye <mode> local start`, which converges only the services that
are not answering and relaunches the application properly.

So:

| To do this | Use |
|---|---|
| start/stop a Datafye service | the Rumi admin scripts (via the CLI) |
| start/stop/terminate a container | Rumi `LocalProvisioner` |
| inspect state, read a file in a volume, list containers | `docker` is fine -- it is read-only |

Reading with `docker` (`ps`, `logs`, `exec ... cat`) is fine and often the fastest
way to see what is true. It is *mutating* lifecycle with Docker that breaks things.
