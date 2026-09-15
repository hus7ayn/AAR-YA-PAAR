# AAR-YA-PAAR — BTC/USDT Session Level Grid

Formal specification of the strategy, the assumptions made where the brief was
ambiguous, and what 12 months of real BTCUSDT perpetual data say about it.

- **Market:** Binance USD-M Perpetual Futures, `BTCUSDT`, **isolated margin**
- **Execution candles:** 15-minute
- **Level-grid candles:** 3-minute (see [Why the grid keeps its own timeframe](#why-the-grid-keeps-its-own-timeframe))
- **Timezone:** IST (Asia/Kolkata, UTC+5:30, no DST)
- **Observation session:** 05:30 – 12:30 IST
- **Trading session:** 12:30 – 18:30 IST (London)

Because IST is a fixed UTC+5:30 offset with no daylight saving, every boundary
lands on an exact UTC hour, so the observation session divides cleanly at any
timeframe — **140** 3m candles or **28** 15m candles, and London is **24** 15m
candles:

| IST | UTC |
|---|---|
| 05:27 (3m anchor candle opens) | 23:57 previous day |
| 05:30 (observation opens) | 00:00 |
| 12:30 (levels frozen, London opens) | 07:00 |
| 18:30 (levels expire) | 13:00 |

---

## Part 1 — Building the level grid

### Step 1: the anchor

**A** = the close of the 3-minute candle covering **05:27 – 05:30 IST**.

This is the candle that *closes at* 05:30 — the last candle before the
observation window opens, not the first one inside it. Practically it is the
session's opening price. The data loader must therefore fetch one candle
earlier than the session start.

### Step 2: the two reference dojis

Over the 140 observation candles, record `sessionHigh = max(high)`,
`sessionLow = min(low)`, and `mid = (sessionHigh + sessionLow) / 2`.

A **doji** is a candle whose open equals its close. Two are selected:

- **Upper doji `Pu`** — among dojis whose **close** is in the top half (`close >= mid`), the one with the highest close.
- **Lower doji `Pl`** — among dojis whose **close** is in the bottom half (`close <= mid`), the one with the lowest close.
- The two must be **distinct candles**.

Ties break to the later candle, so the result is deterministic.

Ranking is on the **close**, not the wick. A doji's open equals its close, so its
close *is* the price level it contributes — and the brief asks for two *price
levels* near the extremes, so it is those levels that must be near. Ranking by
`high`/`low` instead let a long-wicked candle win a slot while the level it
actually contributed sat far from the extreme; that produced spacings up to
**1,480x** apart across the sample, versus **156x** after the correction.

> **Two guards were added here, and they are not decorative.** See
> [Why the doji rule needs a tolerance](#why-the-doji-rule-needs-a-tolerance) —
> the literal rule produces no usable grid on **94.8% of real days**.

### Step 3: the spacing

```
D = |Pu.close - Pl.close|
```

The sign is discarded, per the brief: the difference "can be positive or
negative but we will consider both as positive".

If either doji is missing, or `D` is below one tick, the day is marked
**INVALID** with a machine-readable reason and no trading occurs.

### Step 4: the grid

Using **A** as the reference, levels are marked on both sides at multiples of
`D`, together with their midpoints:

```
level(k) = A + k * (D / 2)      for k in [-2N, +2N]
```

| `k` | meaning |
|---|---|
| `0` | the anchor itself |
| even, non-zero | a full-`D` level |
| odd | a midpoint |

With `N = 3` (default) this gives **13 levels**, spanning `A - 3D` to `A + 3D`,
symmetric about the anchor.

### Step 5: validity

The grid is computed once at **12:30 IST**, frozen, and used unchanged through
the London session until **18:30 IST**. It is never recomputed intraday.

---

## Part 2 — The execution plan

A setup is two consecutive candles.

### Step 1 — the break candle

A candle that **opens on one side of a key level and closes on the other**:

- bullish break: `open < level <= close`
- bearish break: `close <= level < open`

### Step 2 — candle colour

Both candles in the setup need only a **colour**: the break candle red or green,
and the next candle the opposite colour.

```
bullish break candle  ->  close > open
bearish break candle  ->  close < open
```

There is no body-to-range ratio and no wick test. The only shape rejected is a
**flat** candle (`open == close`), which has no colour to be opposite of.

The previous 100%-body / 0-wick marubozu filter is still available via
`max_wick_ticks` but is off by default. At 15m it is unusable: setting
`max_wick_ticks = 0` on this timeframe produced **zero trades in 12 months**,
because a 15m candle with no wick on either side essentially never occurs.

### Step 3 — the 7 EMA filter

The break candle must cross the **7-period EMA** of 3-minute closes in the same
direction it crossed the level (`open < ema <= close`, or the mirror).

The EMA is warmed across the 140 observation candles, so it is already
converged when London opens rather than blind for its first seven bars.

### Step 4 — entry

The **next** candle must be the **opposite colour** to the break candle. Enter at
its close, **in the direction of the opposite candle**.

> A bullish break followed by a bearish opposite candle is therefore a **SHORT**.
> The strategy fades the break. This was confirmed directly:
> *"execute the trade on the direction of the opposite candle."*

```
                    ┌───┐
                    │green            ┌───┐
      level ────────┼───┼─────────────┤ RED ├────────
                    │   │             │   │
                    └───┘             └───┘

        candle 1 (break)         candle 2 (opposite)
        opens below the level    opposite colour
        closes above it          any shape
        crosses the 7 EMA

                 ENTER SHORT at candle 2's close
                 stop +100   target -756
```

### Step 5 — stop and target

- Stop loss: **100 USDT** of BTC price movement
- Take profit: **756 USDT** of BTC price movement

Reward:risk is therefore **1 : 7.56**.

*Units note:* these are read as price distances rather than account P&L,
because that is the only reading under which Steps 5 and 6 agree. With 10,000
USDT capital, Step 6 gives 3 BTC, and a 100 USDT adverse price move on 3 BTC
loses exactly 300 USDT — precisely the 3% being risked. Dimensionally, "divide
by stop loss" only makes sense if the stop is a distance.

Entries are **limit orders**, placed `entry_buffer_seconds` (default 12) before
the candle closes so the order is already resting when the signal confirms
rather than chasing the market afterwards.

### Step 6 — position size

```
qty = (3% x capital) / 100 USDT
```

At 10,000 USDT capital that is **3 BTC**. Sizing is off the starting capital by
default (`compound = false`), matching "total capital entered by the user".

### Step 7 — leverage and margin

**40x, isolated margin.** Isolated caps the loss on a position at its own posted
margin; crossed puts the whole wallet behind it. The backtest's liquidation model
assumes isolated, and the runner sets `marginType=ISOLATED` on the exchange at
startup so the two agree.

 At 10,000 USDT capital and BTC near 89,000, a 3 BTC position is roughly
267,000 USDT notional — about 27x — so 40x is required and leaves some headroom.
The engine refuses any entry whose initial margin exceeds `equity x leverage`.

---

## Why the grid keeps its own timeframe

Moving execution to 15m broke the level rule outright. The doji test needs
`open == close` (within a tolerance), and a 15m candle has five times as long to
move:

| | 3m | 15m |
|---|---|---|
| median \|close − open\| | ~$8 | **$82** |
| candles within $1.00 of flat | ~7% | **0.88%** |
| session candles to search | 140 | **28** |
| **days with a valid grid** | **89.3%** | **11.5%** |

Reaching 87% coverage on 15m needs a tolerance of about **$50** — but on a
timeframe whose median body is $82, a "$50 doji" is just a below-average candle,
not a candle that opened and closed at the same price. The rule would keep its
syntax and lose its meaning.

So the two timeframes are configured separately:

```toml
interval       = "15m"   # execution: the setup, entries and exits
level_interval = "3m"    # the grid: where the doji rule still means something
```

The grid is a **daily structure** computed once at 12:30 and then frozen, so
building it from finer candles costs nothing at execution time. Set
`level_interval = "15m"` to run everything on one timeframe and accept 11.5%
coverage.

## Why the doji rule needs a tolerance

The brief specifies dojis as open **equal to** close. Tested against 366 days of
real BTCUSDT perpetual data, that rule leaves the strategy undefined almost
every day:

| `doji_mode` | tolerance | valid days | median `D` | grid ÷ session range |
|---|---|---|---|---|
| `strict` | 0 (literal spec) | **5.2%** | $201 | 2.69 |
| `tick` | 1 tick ($0.10) | 12.8% | $244 | 2.70 |
| `abs` | 10 ticks ($1.00) | 41.3% | $375 | 2.93 |
| **`adaptive`** | smallest that works, per day | **89.3%** | **$442** | 2.75 |

Two distinct failure modes appear under the literal rule:

1. **No doji at all** — 257 of 366 days had zero candles with an exact
   `open == close` in the seven-hour window.
2. **`D` collapses to zero** — when one candle qualifies as *both* the upper and
   the lower reference, the difference is 0 and all 13 levels stack on the
   anchor. The opposite-halves rule plus the distinctness requirement prevent
   this. (Half-membership alone was not enough: a candle straddling `mid`
   satisfied both sides. That gap was found in audit and is now closed.)

**`adaptive` is the default.** It uses the *smallest tolerance that works on each
individual day* rather than one loose setting applied to every day, which keeps
`D` far tighter than a fixed wide tolerance at the same coverage. Set
`doji_mode = "strict"` in `config.toml` to reproduce the literal specification.

---

## Backtest results

12 months, 2025-08-01 → 2026-08-01, 10,000 USDT, 40x isolated, maker-rate
execution, real funding. **Stop 300 / target 2268 (R:R 1:7.56).**

```
  Trades 49    Win rate 22.4%    PF 0.22
  Gross -5,459.80   Fees -3,374.59   Funding +34.92
  Net   -8,799.47   (-87.99%)   Max DD 88.25%
  Entries refused (margin) 482
```

All 49 trades pass all ten independent spec checks (`tools/audit_trades.py`).

> **Do not compare this -88% against the stop-100 run's -56%.** Both headline
> numbers are artifacts of *when each configuration's margin gate froze the
> account*, not of stop quality. See below.

### The margin gate invalidates naive net-return comparisons

With `compound = false` the position size is fixed off the STARTING capital
while equity falls, so entries stop being affordable below a fixed equity floor
— 8,250 USDT at stop 100, 2,750 at stop 300. Stop 100 trips its floor on **day
six** of the year and spends most of the backtest frozen; stop 300 keeps trading
down to 1,201. The gate acts as an accidental circuit breaker, and it froze the
stop-100 run earlier, which is most of why it "lost less".

It also censors the sample: **444 of 547 signals refused** at stop 100. The
surviving 98 trades show mean gross **+0.274R**; the full uncensored sample
shows **-0.075R**. The apparent gross edge was survivorship, not signal.

### The valid comparison: constant size, no gate

Fixing size at 1.000 BTC against an account large enough that nothing is ever
refused (0 margin rejects, full date coverage) makes the comparison
size-independent. `R` = price move ÷ stop distance.

| config | n | gross R | cost R | **net R** | t(net=0) |
|---|---|---|---|---|---|
| stop $100 / tgt $756 | 401 | -0.075 | 0.538 | **-0.613** | -5.57 |
| stop $300 / tgt $2268 | 309 | -0.112 | 0.181 | **-0.293** | -4.08 |
| stop $300 / tgt $600 | 321 | -0.095 | 0.170 | **-0.266** | -4.36 |
| stop $300 / tgt $300 | 359 | -0.082 | 0.157 | **-0.238** | -4.94 |

**The wider stop is better, not worse.** It cuts the cost burden from 0.538R to
0.181R — a 66% reduction — because risk is held at 3% of capital, so a 3x wider
stop means a 3x smaller position and therefore 3x less fee per unit of risk.
Position size cancels out of the P&L entirely; only the fee term survives.

### But there is no edge to protect

Gross R is negative at every setting and never significantly different from zero
(t between -0.70 and -1.76). The decisive test is not "is the edge non-zero" but
"is the edge large enough to cover its own costs":

```
H0: mean gross R = mean cost R          (the trade at least breaks even)

  stop $100 / 1:7.56   gross -0.075R  vs cost 0.538R   t = -5.73   REJECTED
  stop $300 / 1:7.56   gross -0.112R  vs cost 0.181R   t = -4.11   REJECTED
  stop $300 / 1:2      gross -0.095R  vs cost 0.170R   t = -4.46   REJECTED
  stop $300 / 1:1      gross -0.082R  vs cost 0.157R   t = -5.13   REJECTED
```

Breakeven is rejected at every setting at p < 1e-4. This is a much stronger
statement than "no detectable edge": the strategy is **decisively unprofitable
after costs**, and that holds even though the gross edge itself is too small to
measure.

The $2,268 target is also effectively dead — it is reached on ~1% of trades, so
"R:R 1:7.56" describes almost none of the actual outcomes. Most exits are
time-based at the London close.

### Audit

An adversarial audit ran three independent module reviews, then had every
finding attacked by a separate agent instructed to refute it. 21 issues were
raised; **12 were refuted, 9 confirmed and fixed**, each with a regression test
in `tests/test_audit_regressions.py`:

| severity | defect | effect |
|---|---|---|
| critical | dojis ranked by wick, level taken from close | wrong `D`; max/min spacing ratio 1,480 → 156 |
| critical | live runner had no `SET_TARGET` branch | the 750 target was never placed on the exchange — live carried a stop and no take-profit |
| critical | liquidation checked before the stop | a fast candle booked a full-margin wipe instead of a bounded $300 loss, ~12x overstated |
| major | overlapping doji pools | a candle straddling `mid` could take both slots, collapsing `D` |
| major | `adaptive` exhaustion reported the wrong reason | diagnostics blamed a missing doji when spacing was the real cause |
| major | Step 6 / Step 7 margin conflict | 75 silently refused entries, now surfaced |
| minor | unflattened position discarded at day end | entry fee stranded, trade hidden from results |
| minor | `blown` / `margin_rejects` never reported | refused entries looked like an absence of signals |
| minor | dead `require_level_side` config knob | removed |

The three critical defects were all found by the audit, not by the 68 tests that
were passing at the time.

---

## Configuration

All of the above is driven from `config.toml`.

```toml
[levels]
doji_mode = "adaptive"          # strict | tick | abs | adaptive
steps_per_side = 3              # N -> 4N+1 = 13 levels
require_opposite_halves = true  # prevents the D = 0 collapse

[strategy]
name = "break-fade"

[strategy.break_fade]
max_wick_ticks = 0              # step 2: 0 = literal 100% body / 0% wick
ema_period = 7
stop_usdt = 100.0
target_usdt = 750.0
risk_pct = 3.0
opposite_within = 1             # opposite candle must be the very next candle
level_kinds = "all"             # all | full | anchor
compound = false

[backtest]
leverage = 40.0
```

## Assumptions recorded

Where the brief was open to more than one reading, this is what was chosen and
why. All are configurable.

| # | Ambiguity | Chosen | Why |
|---|---|---|---|
| 1 | "3 minute candle closing" at 05:30 | the 05:27–05:30 candle | user-confirmed |
| 2 | direction on the opposite candle | trade the opposite candle's direction | user-confirmed |
| 3 | "100% body, 0% wick, both sides" | `high == max(o,c)` and `low == min(o,c)`; flat candles excluded | user-specified; `max_wick_ticks` relaxes it |
| 4 | stop/target units | price distance | only reading where Steps 5 and 6 agree |
| 5 | "opposite candle" timing | the immediately next candle | `opposite_within = 1`, configurable |
| 6 | which levels are "key" | all 13 | `level_kinds`, configurable |
| 7 | "total capital entered by the user" | fixed starting capital | matches the wording; `compound` toggles |
| 8 | EMA value used for the cross test | the value including the current close | matches how an EMA reads on a chart |
| 9 | stop and target inside one candle | assume the **stop** filled first | 3m data cannot resolve order; take the worse branch |
| 10 | trades per day | re-arm, capped at 10 | `max_trades_per_day` |
| 11 | "near the session high/low" | nearest by the doji's **close** | the close *is* the price level the doji marks |
| 12 | position left open at 18:30 | always booked at the last close | levels expire; nothing carries overnight |

## Risk notes

- 40x leverage on a 3 BTC position is a 267,000 USDT exposure against 10,000
  USDT of capital. A 2.5% adverse move without a stop would be liquidation.
- **Steps 6 and 7 conflict once BTC rises above `equity x 40 / 3`.** The ceiling
  falls as equity falls, so after a drawdown the system silently stops being able
  to enter. 75 of 113 signals were refused this way in the backtest.
- The backtest assumes the stop always fills at exactly its price. Real stops
  slip, and slip worst in exactly the fast moves this strategy trades.
- Indian tax treatment of crypto derivatives is unsettled. Noted, not advice.
- Live trading requires `live.testnet = false`, `live.dry_run = false`, **and**
  the environment variable `AAR_ALLOW_LIVE=1`. Three separate locks.
