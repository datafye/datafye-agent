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
`df -h` before starting. The bundled foundry resource-cost cheat sheet has the
per-symbol-day rates and worked examples.

## Fetch semantics that surprise people

**`numDays` counts BACKWARDS from `date`.** `date` is the most recent day of the
window, so 2 days from `2025-01-02` gets you `01-02` and `01-01`.

**A fetch REPLACES the stored history** for that frequency rather than adding to
it. Fetch a whole window in one call; looping day by day leaves you with only
the last day.

**Daily OHLC is unadjusted** by default (`adjusted=false`), so a stock split
reads as a crash.
