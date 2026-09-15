# HANDOFF — read this first

Context for whoever picks this up next (human or agent). Written 2026-09-15.

---

## TL;DR

A complete, verified backtesting and live-execution system for a BTCUSDT
perpetual futures strategy. **The engineering works. The strategy does not.**

Over **5 years / 394 trades** the strategy returns **−22.9%**. More importantly,
its entry logic performs **no better than a random entry with the same stop and
target** — at every reward:risk ratio tested, the win rate sits within ~2 points
of the coin-flip benchmark and none of the differences are statistically
significant.

**Do not deploy this with real money.** The infrastructure is reusable; the
signal is not validated.

---

## What the system is

The owner specified a two-part strategy. Both parts are implemented literally,
with every deviation documented.

**Part 1 — build a daily price grid** (`aar/core/levels.py`)
1. Anchor = close of the 05:30–05:45 IST candle
2. Over 05:30–12:30 IST find two "doji" candles (open == close) — one in the top
   half of the session range, one in the bottom
3. `D` = absolute difference between their closes
4. Grid = `anchor ± k·D` for k in ±1..N
5. Grid freezes at 12:30 IST, expires 18:30 IST

**Part 2 — trade the grid** (`aar/strategies/break_fade.py`)
1. A "break candle" opens one side of a level and closes the other side
2. It must be red or green (colour only — no body/wick ratio)
3. It must interact with the EMA
4. The **next** candle, opposite colour, is the entry — **trade in the direction
   of that opposite candle** (so a bullish break → bearish next → SHORT). It is
   a break *fade*.
5. Stop 300 USDT, target `300 × R:R` (both as BTC price distances)
6. Size = `(balance × risk%) ÷ stop` → `(1000 × 1.5%) ÷ 300` = 0.05 BTC
7. Leverage = `(size × price) ÷ margin` — derived, ~4–9x in practice

---

## The result, and why it is what it is

```
5 years (2021-08 → 2026-08), 394 trades, 1,000 USDT account

  won  on  52 winners     +4,674
  lost on 342 losers      -4,376
                        ---------
  gross edge                +299      ← real but tiny
  fees                      -536      ← 180% of the edge
  funding                     +9
                        ---------
  NET                       -229      (-22.9%)

  win rate 13.2%   breakeven needed 13.8%   short by 0.6 points
  realised R:R 6.27:1   (vs 7.56 stated — fees take the difference)
```

### The finding that matters most

A random entry with a stop and a target hits the target with probability
`stop ÷ (stop + target)` = `1/(1+R:R)`. Compare:

| R:R | actual win% | random win% | edge | z |
|---|---|---|---|---|
| 1:2 | 32.1 | 33.3 | −1.2 | −0.54 |
| 1:5 | 18.7 | 16.7 | +2.0 | 1.10 |
| 1:6 | 17.1 | 14.3 | +2.8 | 1.64 |
| 1:7.56 | 13.2 | 11.7 | +1.5 | 0.94 |
| 1:12 | 10.0 | 7.7 | +2.3 | 1.63 |

**Every setting is within ~2 points of random and none reaches significance
(all z < 2).** The level break, the EMA filter and the fade direction are
selecting *fewer* trades, not *better* ones.

That is why no R:R "works": changing it slides you along a random-entry curve
that sits slightly below zero after fees. 1:13 looked best (+1,642) but it is a
spike not a plateau — neighbours at 1:12 and 1:14 give +517 and +555, its top 3
trades are $1,286 of the total, and all three are in 2026.

### Year by year

| year | n | win% | net |
|---|---|---|---|
| 2021 | 50 | 14.0 | +34 |
| 2022 | 79 | 12.7 | −44 |
| 2023 | 68 | 10.3 | −199 |
| 2024 | 80 | 15.0 | +89 |
| 2025 | 67 | 11.9 | −193 |
| 2026 | 50 | 16.0 | +84 |

---

## Things already tried — do not redo these

| tried | outcome |
|---|---|
| doji rule taken literally (`open == close`) | valid grid on only **5.2%** of days; needs a tolerance. `adaptive` mode gets 89–92% |
| 100%-body / 0-wick marubozu candles | at 15m this produces **zero trades in 12 months** |
| market entries instead of post-only | gross improves, fees eat more than the gain — net worse |
| widening the stop 100 → 300 | helps cost/risk ratio (0.538R → 0.181R) but no edge to protect |
| tuning R:R across 1:1 … 1:20 | all within noise of random; best settings are single-period artifacts |
| EMA period sweep 2 … 50 | shorter is better, 3–6 best, but only 5 and 6 survive an out-of-sample split |
| risk % sweep 0.5 … 10% | **cannot** change fees as a share of risk (constant 16.3% — both scale with size). Only affects volatility drag; >5% reduces return, 10% goes negative |
| filters to reach 36–40% win rate | every combination that hits 33–35% in-sample collapses to **0–7%** out-of-sample. Classic curve-fit |
| anchor-only levels (`level_kinds = "anchor"`) | the **one filter that survived** an honest split: 25.0% first half / 21.1% second. Kept |

---

## Current config (`config.toml`)

```toml
interval        = "15m"   # execution
level_interval  = "3m"    # the doji rule needs small candles; at 15m only 11.5% of days are valid
anchor_interval = "15m"
anchor_position = "first_session"   # the candle OPENING at 05:30, its close

doji_mode = "adaptive"
level_kinds = "anchor"       # only survivor of out-of-sample testing
include_midpoints = false
steps_per_side = 12

ema_period = 3               # owner's choice; 5–6 tested better out-of-sample
ema_mode = "cross"
skip_first_candles = 2       # 12:30–13:00 was worst hour in both halves
level_cross_tol_ticks = 3    # OPEN-side slack only; close must genuinely clear

stop_usdt = 300
target_usdt = 2268           # 1:7.56
risk_pct = 1.5
compound = true              # "Account Balance" = current balance
max_trades_per_day = 1
flatten_at_london_close = false   # hold to stop or target, no 18:30 cut

post_only_entry = true
limit_target = true
fee_discount = 0.9           # BNB
leverage = 40                # cap; ~4–9x actually used
```

---

## Architecture

```
aar/core/levels.py     THE LEVEL ENGINE — pure function, candles in, LevelSet out.
                       No network, no clock. Backtest and live both call it, which
                       is what stops them diverging.
aar/core/sessions.py   IST window maths. IST is UTC+5:30 with no DST, so every
                       boundary is an exact UTC hour.
aar/core/rules.py      Strategy protocol + Context + registry.
aar/strategies/        break_fade.py — the 7-step execution plan.
aar/backtest/engine.py Two engines: run() day-by-day (flatten at close), and
                       run_held() a continuous walk that carries positions across
                       days until stop/target. The latter is active.
aar/live/              broker.py (orders, filters, kill switch) + runner.py (scheduler)
aar/data/              Binance USD-M REST (stdlib HMAC) + bulk archive loader
tools/sweep.py         parameter sensitivity
tools/audit_trades.py  ** re-derives every trade from raw candles, independent of
                       the strategy code. If they disagree, one is wrong. **
```

**Zero third-party dependencies.** Python 3.11+ stdlib only. Nothing to install.

### The auditor is the most valuable thing here

`tools/audit_trades.py` rebuilds every trade from raw candles without touching
the strategy class, and checks it against all 10 spec rules. It has caught
**four real bugs** the 93-unit-test suite missed:

- dojis ranked by wick while the level came from the close
- the live runner silently dropping the take-profit order
- liquidation checked before the stop (12x overstated losses)
- the EMA going stale while a position was open

**Run it after any change.** It must report 10/10 on every trade.

---

## Running it

```bash
python3 -m unittest discover -s tests -t .              # 93 tests
python3 -m aar verify                                   # check engine vs live exchange
python3 -m aar backtest --start 2021-08-01 --end 2026-08-01
python3 tools/audit_trades.py                           # MUST be 10/10
python3 -m aar levels --date 2026-08-27
python3 -m aar live --once                              # dry run, places nothing
```

First backtest downloads ~110 monthly archives (~5 min). Cached after that (~28 s).

**Live trading needs all three locks:** `live.testnet = false`,
`live.dry_run = false`, and env `AAR_ALLOW_LIVE=1`. Defaults are safe.

---

## If you want to take this further

The exits, costs and plumbing are done and verified. **The entry is the problem.**

1. **Test each entry condition against a random-entry control, individually.**
   Does the level break beat random? Does the EMA filter? Does the fade
   direction? If one carries signal, build on it. If none do, the premise needs
   rethinking rather than retuning. *This is the single highest-value next step
   and it has not been done.*
2. **Re-tune on 2021–2024, test on 2025–2026,** never letting test years inform
   choices. Expect near-breakeven, but it would be an honest number.
3. **Forward-test on testnet.** Data no parameter has ever seen.

### Methodology notes, learned the hard way

- **Always split in/out-of-sample.** Filters reaching 35% in-sample delivered
  0–7% out. Every promising result in this project died on an honest split.
- **Watch for spikes vs plateaus.** A real parameter has neighbours that behave
  similarly. 1:13 was a spike.
- **Check whether one trade carries the result.** It usually did.
- **Compare against the random-entry benchmark**, not against zero. A stop/target
  system has a *mechanical* win rate; beating zero is not beating chance.

---

## Known gaps

- `runner.py` / `broker.py` have almost no test coverage
- Never run against a funded testnet account — auth, rounding and fills unexercised
- No crash recovery: a restart mid-position does not reconcile with the exchange
- No partial-fill handling; REST polling, no WebSocket
- Slippage is a flat 1 tick; real stops slip worst in fast moves
- Liquidation uses tier-1 maintenance margin; positions may sit in a higher tier
- `tools/audit_trades.py` prints "7 EMA" regardless of the configured period
  (cosmetic — it reads the real value from config)

---

## The honest summary

Roughly 20 substantive changes were made across this project, each measured.
The infrastructure is solid: 93 tests, an independent auditor, an adversarial
audit that found and fixed 9 confirmed defects, and every reported number
reproducible from raw candles.

But five years of data say the entry logic is indistinguishable from chance, and
the fees are 180% of the gross edge. Every configuration that looked profitable
on one year failed on the other four.

Treat the code as a well-built research platform. Treat the strategy as
falsified until an entry condition is shown to beat a random-entry control.
