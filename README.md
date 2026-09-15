# AAR-YA-PAAR

Rule-based automated trading system for **BTC/USDT USD-M perpetual futures** on
Binance. Builds a grid of price levels from the 05:30–12:30 IST session, then
trades a break-fade setup against that grid during the London session
(12:30–18:30 IST). Execution runs on 15m candles; the grid is built from 3m.

**The strategy specification lives in [STRATEGY.md](STRATEGY.md)** — including
the 12-month backtest and what it shows.

> **Read the results before trading this.** On a size-independent basis the
> strategy is **decisively unprofitable after costs**: net R per trade is -0.61
> (stop $100) and -0.29 (stop $300), and the hypothesis that it merely breaks
> even is rejected at p < 1e-4 at every setting tested. The gross edge is not
> significantly different from zero. Widening the stop 100 -> 300 *helps* (it
> cuts cost from 0.538R to 0.181R) but cannot rescue a strategy with no edge.
> See [Backtest results](STRATEGY.md#backtest-results).

## Requirements

Python 3.11+. **No third-party packages** — everything is standard library
(`tomllib`, `urllib`, `hmac`, `zipfile`, `zoneinfo`). Nothing to install, no
virtualenv needed.

## Quick start

```bash
# levels for one day, printed and saved as JSON
python3 -m aar levels --date 2026-08-15

# cross-check the engine and cached data against the live exchange
python3 -m aar verify

# compare all four doji modes over a date range
python3 -m aar diagnose --start 2025-08-01 --end 2026-08-01

# render SVG charts with the grid overlaid
python3 -m aar chart --start 2026-08-01 --end 2026-08-10

# backtest
python3 -m aar backtest --start 2025-08-01 --end 2026-08-01

# parameter sensitivity sweep
python3 tools/sweep.py

# independently re-derive every backtest trade from raw candles
python3 tools/audit_trades.py

# tests
python3 -m unittest discover -s tests -t .
```

First run downloads monthly candle archives from `data.binance.vision` into
`out/cache/` (~660 KB per month). Later runs read the cache.

## Layout

```
aar/
  core/
    models.py      value types; prices compared as integer ticks, never floats
    sessions.py    IST window arithmetic; funding stamp calculation
    levels.py      THE LEVEL ENGINE — pure function, candles in, LevelSet out
    rules.py       Strategy protocol + Context + registry
  strategies/
    break_fade.py  the 7-step execution plan
  data/
    rest.py        Binance USD-M REST client, HMAC-signed
    history.py     bulk archive loader + CSV cache + gap detection
  backtest/
    engine.py      candle replay: fills, fees, funding, stops, liquidation
    report.py      metrics and the doji-mode diagnostics
  live/
    broker.py      order placement, filter compliance, kill switch
    runner.py      daily scheduler
  viz/
    chart.py       hand-rolled SVG candle chart
tools/
  sweep.py         parameter sensitivity sweep
  audit_trades.py  re-derives every trade from raw candles, independent of the strategy code
tests/             93 tests, incl. regressions for all 9 audited defects
```

`core/levels.py` is a **pure function** — no network, no clock, no config reads.
The backtest and the live runner call that same function, which is what stops
them from drifting apart.

## Adding your own rules

Subclass `BaseStrategy`, register it, and point `config.toml` at it:

```python
from aar.core.models import Action, Signal
from aar.core.rules import BaseStrategy, register

@register("my-strategy")
class MyStrategy(BaseStrategy):
    def on_session_start(self, levels, history=()):
        ...   # warm indicators on the 140 observation candles

    def on_candle(self, ctx):
        if ctx.crossed_up() and ctx.position is None:
            return [Signal(Action.OPEN_LONG, qty=0.01, reason="crossed a level")]
        return ()
```

`ctx` gives you the frozen grid plus helpers: `ctx.price`, `ctx.spacing` (the
day's `D`), `ctx.nearest_level()`, `ctx.level_above()`, `ctx.level_below()`,
`ctx.crossed_up()`, `ctx.crossed_down()`, `ctx.levels_crossed()`,
`ctx.is_last_candle`.

Import it from `aar/strategies/__init__.py` so the registry sees it.

## Live trading

Defaults are safe. Placing a real order requires **all three**:

```toml
[live]
testnet = false
dry_run = false
```
```bash
export AAR_ALLOW_LIVE=1
```

Without all three the broker logs what it *would* send and sends nothing.

```bash
# credentials
export AAR_TESTNET_API_KEY=...   AAR_TESTNET_API_SECRET=...
export AAR_LIVE_API_KEY=...      AAR_LIVE_API_SECRET=...

python3 -m aar live --once   # run today's session immediately, no waiting
python3 -m aar live          # schedule: freeze levels 12:30, trade to 18:30 IST
```

Market data always comes from the live exchange; only order execution honours
the testnet flag. The runner syncs tick/step/notional filters from the exchange
at startup rather than trusting config, and flattens on London close.

## Outputs

| path | contents |
|---|---|
| `out/cache/` | monthly candle CSVs |
| `out/levels/` | per-day level JSON |
| `out/reports/` | `trades.csv`, doji diagnostics, level dumps |
| `out/charts/` | per-day SVG charts |
