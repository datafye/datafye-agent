# Sizing an environment, and what memory a flow actually needs

**Read this before any fetch, replay, or backtest — not after one fails.** Estimate what
the flow needs, compare it against the box, set the heaps in the descriptor, apply, and
only then run. A run that dies at 80% has cost more than the estimate would have.

## There are two memory ceilings and they are not the same

- **Host RAM** — everything on the box together. Check with `free -m`.
- **A service's JVM heap** — one service, and **a bigger instance does not raise it**.

A service can die of `OutOfMemoryError` while the host has tens of GiB free. If a container
is sitting near its own heap ceiling while `free -m` looks healthy, it is the second kind
of problem and resizing the box will not fix it.

Do not read container RSS as heap. RSS includes everything outside the heap, so a container
showing ~600 MiB may be a JVM whose heap ceiling is 256 MiB and is about to die.

## Default heaps, and how to change them

| Service | Default | Driven by |
|---|---|---|
| `<dataset>-history` | **2 GiB** | buffers a whole day during a combined-ticks fetch |
| `<dataset>-agg` | **256 MiB** | retained bars: symbols x frequencies x days since last clear |
| `<dataset>-feed` | 256 MiB | streams; does not accumulate |
| `<dataset>-reference` | 256 MiB | the symbol universe |

Set them per dataset in the descriptor and apply before the run:

```yaml
datasets:
  - dataset: SIP
    services:
      history: { heap: 2g }
      agg:     { heap: 1g }
      reference: { enabled: false }
```

`heap` is a plain size (`512m`, `2g`), never a JVM flag. `enabled: false` switches a service
off entirely — worth doing for anything the flow never reads.

## The aggregation service is the one that surprises you

Its memory is driven by what it **retains**, not by throughput.

```
agg heap  ~  symbols x bars per day x frequencies enabled x days replayed since the last state clear
```

Bars per symbol per day: Second ~57,600 · Minute ~960 (04:00-20:00) · Hour ~16 · Day 1.
SMA and EMA are **separate queues again**, one per frequency, each the size of the OHLC
queue.

**Only the Day queue is bounded** (`retentionDays`, default 30). Second, Minute and Hour
grow until the store is cleared and at no other time.

Three levers, in this order:

1. **Clear state between replays** — `DELETE /datafye-api/v1/deployment/state`. This is also
   the documented backtest order: fetch, clear, replay. Skip it and replaying four dates
   leaves four days of bars per symbol in the heap, none of which the current replay needs.
2. **Do not build frequencies you will not read.** Second costs ~60x Minute.
3. **Then raise `agg.heap`.**

Prior days do not need to be in the agg: `/stocks/live/agg/ohlcs?history=N` reads earlier
days from the history service.

## Fetch: estimate, then guard

Disk per symbol-day, high-volume (conservative, use these): combined ticks **780 MB** ·
trades only **300 MB** · quotes only **480 MB** · OHLC 1s **4.5 MB** · 1m **0.16 MB** ·
1h **0.005 MB**.

```
estimated_disk_MB = numDays x n_symbols x 780      # combined ticks
```

Peak memory: trades-only or quotes-only fetch is flat ~1.2-1.3 GiB (streams to disk); a
combined-ticks fetch is `1200 + 0.9 x one-day ticks MB`; a replay is
`1800 + 0.8 x day ticks MB`.

**The guard, before anything else:**

```
IF combined-ticks fetch AND busiest_day_ticks_MB > 1300:
    it exhausts the history heap and writes ZERO data
```

It fails quietly and surfaces later as a missing log, far from the cause. Fix by fetching
trades and quotes **separately** (both stream), or splitting the symbol list, or raising
`history.heap`. A bigger instance does not help.

Synthetic: fixed 1024 MB per day per data type, independent of symbol count. Crypto: a day
is 24h, ~150 MB per coin-day for a major pair, trades only.

## Instance sizes

| Size | vCPU | RAM | Budget (70%) |
|---|---|---|---|
| medium | 2 | 8 GiB | ~5.7 GiB |
| large | 4 | 16 GiB | ~11.5 GiB |
| **xlarge** | **8** | **31 GiB** | ~22 GiB |
| 2xlarge | 16 | 62 GiB | ~44 GiB |

Only xlarge is measured. Keep peak host usage at or below **70%** of RAM and leave **5 GB**
disk free. When borderline, ask the user to resize up rather than assuming it fits.

## Provenance

Fetch and replay figures: measured on an xlarge across 47 isolated runs, high-volume
session, state cleared and history wiped between runs. Biased high on purpose.

The agg model is derived from what the service retains, not measured. Treat it as the right
shape with an uncertain constant, and prefer clearing state over trusting the arithmetic.

Full per-unit tables, worked examples and methodology: the bundled foundry resource-cost
cheat sheet.
