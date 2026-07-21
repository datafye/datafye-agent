# Datafye Foundry Resource-Cost Cheat Sheet

Purpose: before running a foundry data operation (a historical **fetch**, or a
**replay** of fetched data), estimate the memory and disk it needs, compare against
the instance you are on, and if it will not fit, ask the user to resize to a named
instance size *before* running it. Memory is the scarce resource; disk is second.

All estimates are biased to a **high-volume trading day** (worst case). A quiet day
uses less. Under-estimating is the failure mode we avoid, so round up and apply the
headroom rule.

Units: **disk in MB = 10^6 bytes**; **memory / RAM in MiB/GiB = 2^20 / 2^30 bytes**.
Measured empirically on an **xlarge** (8 vCPU, 31 GiB RAM, 32 GB disk), Datafye
foundry `2.0.28`, SIP and Crypto via Polygon plus Synthetic, high-volume session
2026-07-17 (47 isolated runs). See "Methodology" and the raw CSV for provenance.

---

## 1. The estimation formula (compute this inline)

### Step A - estimated DISK

```
# SIP (US equities), binary on-disk footprint (the .log the fetch writes)
estimated_disk_MB = numDays * SUM_over_symbols( rate_ticks[liquidity] )      # combined ticks
                  = numDays * SUM_over_symbols( rate_trades + rate_quotes )  # if fetched separately
   + for OHLC:      numDays * n_symbols * rate_ohlc[frequency]

# Conservative default: treat EVERY symbol as HIGH liquidity and EVERY day as high-volume.
#   -> estimated_disk_MB = numDays * n_symbols * 780   (combined ticks, MB)

# Synthetic (generated, NOT market-driven): fixed 1024 MB per day per data type,
#   INDEPENDENT of symbol count.
estimated_disk_MB(Synthetic) = 1024 * numDays * n_data_types      # ticks counts as 1 type

# Crypto (US, Polygon): TRADES only (quotes are empty on this key -> ticks == trades).
#   A crypto "day" is 24h (not 6.5h), so a day holds more than an equity session.
estimated_disk_MB(Crypto) = numDays * SUM_over_symbols( rate_crypto_ticks[coin] )
#   Conservative default: 150 MB/coin-day (major pair). Add OHLC per section 2c.
```

### Step B - estimated PEAK MEMORY (the OOM-critical number)

The fetch/replay runs in the `rumi-<dataset>-history` container, JVM heap capped at
**`-Xmx2g`**. That 2 GiB heap is the hard ceiling where a single fetch OOMs.

```
history_service_baseline = 1200 MiB          # idle resident of the SIP history JVM

# TRADES-only or QUOTES-only fetch: streams straight to disk, does NOT buffer.
estimated_peak_mem_MiB = 1300                 # ~flat, any #symbols/#days

# COMBINED-TICKS fetch: buffers ONE day of all symbols in heap (merge-sort by time),
#   flushed per day. Memory scales with symbols-PER-DAY, not with day count.
busiest_day_ticks_MB = n_symbols * 780        # conservative high rate, one day
estimated_peak_mem_MiB = ceil(1200 + 0.9 * busiest_day_ticks_MB)

# REPLAY of a fetched day (drives history+feed+agg pipeline on the host):
estimated_peak_mem_MiB = ceil(1800 + 0.8 * day_ticks_MB)     # day_ticks_MB = one replayed day, all symbols
```

### Step C - the OOM guard (do this BEFORE anything else)

```
IF (combined-ticks fetch) AND (busiest_day_ticks_MB > 1300):
    -> WILL OOM the 2 GiB heap and write ZERO data. Do NOT run as-is.
    Fix: (a) fetch trades and quotes SEPARATELY (they stream, no buffer), or
         (b) split symbols across multiple fetches (<= ~1300 MB of one-day ticks each), or
         (c) fetch fewer symbols / use OHLC instead.
```

### Step D - the fit check (host RAM + disk)

```
foundry_baseline_host = 7000 MiB    # a running single-dataset Data-Cloud-Only foundry, before any op
                                    # add ~2000 MiB per ADDITIONAL deployed dataset

peak_host_MiB = foundry_baseline_host + (estimated_peak_mem_MiB - history_service_baseline)

FITS (memory) IF:  peak_host_MiB <= 0.70 * instance_RAM_MiB          # keep 30% headroom
FITS (disk)   IF:  estimated_disk_MB <= (free_disk_MB - 5000)        # leave >= 5 GB free
                   AND, if a replay will follow, data stays on disk during replay (already counted)

IF not FITS -> tell the user the estimate, the current instance, and the smallest
instance that fits (section 5), and ask them to resize before proceeding.
```

---

## 2. Per-unit cost table (SIP US equities)

Per **symbol per day**, high-volume session. **Headline column = HIGH liquidity on a
high-volume day with the market-open peak already included** - this is the default you
should use. "Typical (midday/mid-liquidity)" is a secondary reference only.

| Data type | Headline HIGH (default) | Typical (mid-liq / midday) | Low-liquidity | Notes |
|---|---|---|---|---|
| Combined ticks (trades+quotes) | **780 MB** | 150 MB | 1-4 MB | ticks = trades + quotes exactly |
| Trades only | **300 MB** | 55 MB | ~1 MB | streams (no heap buffering) |
| Quotes only | **480 MB** | 95 MB | ~2 MB | streams; usually > trades |
| OHLC 1-second | **4.5 MB** | 2 MB | <0.5 MB | per symbol-day |
| OHLC 1-minute | **0.16 MB** | 0.12 MB | tiny | ~390 bars/day |
| OHLC 1-hour | **0.005 MB** | 0.005 MB | tiny | ~7 bars/day |
| OHLC 1-day | ~0.0003 MB | same | same | 1 bar/day |

Measured anchors (combined ticks, 1 day, 2026-07-17): INTC 727 MB, AAPL 603 MB (ultra-high);
KO 213 MB, T 91 MB, F 52 MB (mid); MKL 3.6, UVV 3.1, CABO 1.8, NPK 1.6, SEB 0.7 MB (low).
Headline 780 MB rounds up above the measured max. Multi-day scales linearly
(AAPL 2-day = 1179 MB ~= 2x). Multi-symbol disk is additive (validated to <0.1% error).

**Market-open / peak multiplier.** Intraday volume is front/back-loaded: the peak hour
carries ~2.1x the session-average hour and ~8x the quietest hour (top 2 hours = 82% of
volume, measured on AAPL minute bars). Fetches always pull a whole day, so this does not
change fetch disk. It matters when estimating a **partial-window replay**: multiply the
day rate by the window fraction, and by ~2x if the window is the open or close hour.

### Memory behaviour (SIP), measured

| Operation | Peak memory | Scales with |
|---|---|---|
| Trades-only / quotes-only fetch | ~1.2-1.3 GiB (flat) | nothing - streams to disk |
| Combined-ticks fetch | 1200 + 0.9 x (one-day ticks MB) MiB | symbols PER DAY (buffered) |
| Replay of a fetched day | 1800 + 0.8 x (day ticks MB) MiB | data in the replayed day |
| **OOM point (2 GiB heap)** | **one-day combined-ticks buffer > ~1300 MB** | writes 0 data, ~4 min of GC thrash |

Confirmed OOM: 10 and 25 high-volume symbols, combined ticks, 1 day -> `OutOfMemoryError`,
zero data written. 5 mixed symbols (1.74 GB one-day ticks) was the successful edge case.

## 2b. Synthetic dataset

Synthetic data is **generated, not market-driven**, and has a deterministic footprint:

| Metric | Value |
|---|---|
| Disk per day per data type (trades / quotes / ticks) | **exactly 1024 MB (1 GiB)** |
| Dependence on symbol count | **none** - 1 symbol and 5 symbols both = 1 GiB |
| Multi-day | numDays x 1024 MB (3-day = 3072 MB) |
| OHLC via fetch | not generated (0 bytes) |
| Fetch peak memory | ~0.5 GiB (streams) |
| Replay peak memory | ~1.2 GiB |

Use Synthetic for pipeline/algo testing without provider data. Budget 1 GiB of disk per
day per data type regardless of how many symbols you name.

## 2c. Crypto dataset (measured)

Crypto trades **24/7**, so a "day" is a full 24h. On this Polygon key crypto provides
**trades only - quotes come back empty** - so combined "ticks" == trades. Per coin per day:

| Data type | High (BTC/ETH) | Low (DOGE-class) | Notes |
|---|---|---|---|
| Ticks / trades | **140 MB** (BTC 138, ETH 86) | 4 MB (DOGE 3.8) | 24h day; quotes N/A |
| Quotes | n/a | n/a | empty on this key |
| OHLC 1-second | **15 MB** | (scales w/ activity) | ~3.5x the equity figure (24h vs 6.5h) |
| OHLC 1-minute | **0.25 MB** | | 1440 bars/day |
| OHLC 1-hour | **0.006 MB** | | 24 bars/day |

Memory: streams like SIP trades/quotes - fetch peak ~1.0-1.3 GiB (flat), replay ~1.7 GiB.
Volumes per coin are well below the SIP OOM zone, but the same 1.3 GB one-day combined
buffer ceiling applies if you ever fetch many coins at once. Multi-day is linear
(BTC 3-day = 387 MB). Use headline **150 MB/coin-day** for a major pair (conservative).

---

## 3. On-disk layout & what gets written (ground truth)

- Fetches are performed and stored by the **`rumi-<dataset>-history`** container
  (`-Xmx2g`). Feed/aggregation/reference services are `-Xmx256m` each.
- Data lands in the `rumi-<dataset>-history-shared` docker volume, mounted at
  `/home/rumi/datafye/history` inside the container:
  - Ticks/trades/quotes: `.../<DATASET>/tick/<year>/<date>.log` (+ `.metadata`, `.factories`, `.properties`)
  - OHLC: `.../<DATASET>/ohlc/<Frequency>/<year>/<date>.log` (+ `-idx.*`)
- The `.log` is the binary footprint; it is **written at completion** (buffered first),
  so `du` before/after brackets it cleanly.
- **CSV footprint:** there is no first-class CSV export for ticks/trades/quotes; only
  OHLC history is served (as JSON) via `GET /stocks/history/ohlcs`. A CSV is produced by
  a consumer serializing records. Measured: OHLC-1m CSV ~= **50 bytes/bar** (338 bars =
  16.8 KB), which is far SMALLER than its 154 KB binary store. For ticks, estimate CSV at
  **~50-60 bytes/record** (a high-volume name is ~1.1M trades/day -> ~60 MB CSV vs ~230 MB
  binary), i.e. roughly **0.25-0.3x the binary footprint**. Treat tick CSV as an estimate.

---

## 4. Instance-size map

| Size | vCPU | RAM | 70% RAM (mem budget) | Disk | Notes |
|---|---|---|---|---|---|
| medium | 2 | 8 GiB | ~5.7 GiB | (confirm) | barely covers the ~7 GB foundry baseline - tight |
| large | 4 | 16 GiB | ~11.5 GiB | (confirm) | comfortable for single-dataset fetch + replay |
| **xlarge** | **8** | **31 GiB (MEASURED)** | ~22 GiB | **32 GB (MEASURED)** | headroom for large multi-symbol work |
| 2xlarge | 16 | 62 GiB | ~44 GiB | (confirm) | multi-dataset / heavy replay |

Only **xlarge is measured** here (8 vCPU, 30.97 GiB RAM, 32 GB disk). medium/large/2xlarge
RAM/vCPU follow the standard doubling ladder and **should be confirmed** against the real
platform config; **disk per size is not confirmed** (xlarge = 32 GB observed). The docs
state a hard minimum of **8 GB RAM / 20 GB disk** for any local foundry.

---

## 5. Headroom rule

- **Memory:** keep `peak_host_MiB <= 70% of instance RAM`. The check is
  `worst_case_estimate + headroom <= instance RAM` (worst case = high-volume day, rounded up).
- **Disk:** leave `>= 5 GB` free. Never let `estimated_disk_MB` come within 5 GB of free space.
- **Heap OOM (separate from host RAM):** the history heap is 2 GiB **on every instance
  size** - a bigger box does NOT raise it. So a combined-ticks fetch whose one-day buffer
  exceeds ~1300 MB OOMs on a 2xlarge just as on a medium. The fix is always to split the
  fetch (trades/quotes separately, or fewer symbols), never to resize.
- When borderline, **resize up** rather than assume it fits.

---

## 6. Worked decision examples

Assume current instance and a single-dataset SIP foundry (baseline ~7 GB host). Estimates
assume a high-volume trading day; a quiet day uses less.

**Example 1 - "Fetch AAPL, 1 day of trades."**
- Disk = 1 x 1 x 300 MB = **300 MB**. Memory: trades stream -> peak ~1.3 GiB;
  peak_host ~= 7.0 + 0.1 = **7.1 GB**.
- medium (8 GiB): 7.1 GB > 5.7 GB budget -> **resize to large**. large (16 GiB): 7.1 < 11.5 -> **fits**.
- Verdict: **fits on large+; on medium, resize to large.**

**Example 2 - "Fetch combined ticks for 10 liquid names, 1 day."**
- One-day ticks buffer = 10 x 780 = **7800 MB >> 1300 MB -> WILL OOM, writes 0 data.**
- Verdict: **do not run as-is.** Fetch trades and quotes **separately** (they stream), or
  split into >=6 fetches of <=1 high-vol symbol each. Disk if completed = ~7.8 GB (needs
  >=13 GB free; fine on xlarge, check medium/large disk).

**Example 3 - "Backfill 30 symbols x 5 days of combined ticks."**
- Disk = 5 x 30 x 780 = **117,000 MB (117 GB)** -> exceeds every instance's disk.
- Per-day buffer = 30 x 780 = 23,400 MB -> **OOMs** too.
- Verdict: **infeasible as one fetch.** Reduce scope (fewer symbols / OHLC instead of ticks),
  and/or fetch per-symbol trades+quotes separately across days. If genuinely needed, this
  is a disk-bound job requiring a much larger attached volume - escalate to resize disk.

**Example 4 - "Replay AAPL, 1 day (after fetching its ticks)."**
- day_ticks_MB = 603 -> replay peak = 1800 + 0.8x603 = **~2280 MiB**;
  peak_host ~= 7.0 + (2.3 - 1.2) = **~8.1 GB**.
- medium (8 GiB): 8.1 GB > 5.7 GB budget and ~= total RAM -> **OOM risk, resize to large**.
- large (16 GiB): 8.1 < 11.5 -> **fits**.
- Verdict: **resize to large for replay on medium; fits on large+.**

**Example 5 - "Fetch 30 symbols x 90 days of 1-minute OHLC."**
- Disk = 90 x 30 x 0.16 = **~432 MB**. Memory: OHLC streams -> ~1.3 GiB.
- Verdict: **fits on any size** (medium included).

---

## 7. Methodology

- Provisioned local Data-Cloud-Only foundries per dataset via
  `datafye foundry local provision -x <descriptor>`; deprovisioned + wiped volumes
  between datasets so measurements are isolated.
- Each operation driven by REST against `http://local-foundry-dev-api.datafye.local:7776`:
  fetch = `POST /<asset>/backtest/history/{ticks|trades|quotes|ohlcs}` with body
  `{"date","dataset","symbols","numDays"[,"frequency"]}`; status = `GET` same path;
  replay = `POST /<asset>/backtest/replay/ticks`; reset = `DELETE /deployment/state`.
- Memory sampled ~4x/sec from cgroup v2 (`memory.current`, `memory.stat`) for the
  relevant container(s) plus host `free -m`. Reported `anon` = heap+native (the OOM-
  relevant figure); working-set (docker-stats equivalent) and host peak also captured.
  Replay samples history+feed+agg summed (whole pipeline on the host).
- Disk = `du -sb` of the history dir before/after each op (delta), plus the `.log` size.
- Isolation: `DELETE /deployment/state` + wipe of the dataset's history dir before each
  fetch; replay resets state but keeps the fetched data.
- Model validated on two unseen fetches: AAPL+F ticks 1d (predicted 655 MB, actual 655.3
  MB) and AAPL+KO+F ticks 1d (predicted 867 MB, actual 867.8 MB) - disk error <0.1%.
  Memory buffering factor observed 0.53-0.82; refined UP to 0.9 for conservatism.

---

## 8. Assumptions, caveats and gaps (read before quoting a number)

- **Conservative bias:** headline rates are HIGH-liquidity, high-volume-day, market-open
  figures, rounded up. Real quiet-day usage is lower. This is deliberate - the sheet
  exists to stop us exhausting the box.
- **Crypto is measured** (section 2c), with two caveats: (a) **quotes are unavailable** on
  this key (empty), so crypto ticks == trades; (b) crypto trades 24/7, so a "day" is 24h.
- **Crypto symbol format:** pass the **bare** symbol (`BTCUSD`, `ETHUSD`) - the dataset
  prepends the `X:` prefix itself. Passing `X:BTCUSD` becomes `X:X:BTCUSD` at the provider
  and silently returns **zero data** (this bit us initially). Also, for the **crypto**
  asset class `dataset` must be in the **query string, not the body** (the body rejects an
  unknown `dataset` field with HTTP 500) - the opposite of Synthetic, which requires
  `dataset` in the body. SIP accepts it in either place.
- **Symbols must be declared** in the deployment descriptor to be fetchable; an undeclared
  symbol is rejected ("not in the deployed symbol set"). A declared symbol the provider
  lacks returns 0 bytes.
- **Synthetic** supports a fixed symbol universe (AAPL, MSFT, NVDA, TSLA, AMD, AMZN, GOOGL,
  META, NFLX, DIS, JPM, V, PYPL, INTC, WMT) and does not generate OHLC via fetch.
- **`numDays` belongs in the request BODY**, not the query string (query-string `numDays`
  is silently ignored and you get 1 day). `symbols` in the body must be a comma STRING; a
  JSON array returns HTTP 500. `dataset` placement is asset-class-specific: SIP accepts it
  in query or body; **Synthetic requires it in the body**; **Crypto requires it in the
  query** (body rejects it). Safest: always put `dataset` in the query, and add it to the
  body only for Synthetic.
- **Instance sizes:** only xlarge is measured. Confirm medium/large/2xlarge RAM and all
  per-size disk figures against the platform before relying on section 4.
- **Multi-dataset foundries** raise the host baseline (~+2 GB per extra dataset) and were
  less stable to provision here; per-dataset foundries are cleaner.
- **CSV tick footprint** is an estimate (no export endpoint); OHLC CSV is measured.
- Figures are for foundry `2.0.28` on Linux/Docker (cgroup v2). Re-measure if the history
  service heap (`-Xmx2g`) or version changes.

---

*Raw measurements: `foundry-cost-raw-measurements.csv` (this folder). Sample OHLC CSV
export: `sample-ohlc-1m-AAPL-2026-07-17.csv`. Generated 2026-07-21.*
