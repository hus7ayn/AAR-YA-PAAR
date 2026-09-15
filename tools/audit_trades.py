"""Independently re-derive every backtest trade from raw candles.

This does NOT use the strategy class. It reloads the candles, recomputes the
levels, recomputes the EMA, and checks each recorded trade against the seven
steps of the execution plan from first principles. If the strategy code and
this script ever disagree, one of them is wrong.

    python3 tools/audit_trades.py [path/to/trades.csv]
"""

from __future__ import annotations

import bisect
import csv
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aar import config as cm
from aar import strategies  # noqa: F401
from aar.core.levels import compute_levels
from aar.core.models import interval_to_ms
from aar.core.sessions import anchor_open_ms_for, to_ist_str, window_for
from aar.data.history import HistoryStore
from aar.data.rest import FuturesClient
from aar.strategies.break_fade import Ema, body_ratio, direction, qualifies, wick_ticks

CHECKS = [
    "1  break crosses a key level",
    "2a break candle has a colour (red/green)",
    "2b opposite candle has a colour (red/green)",
    "3  break crosses the 7 EMA",
    "4a opposite candle is opposite direction",
    "4b trade took the opposite candle's direction",
    "4c entry price == opposite candle close",
    "5  stop/target distances honoured",
    "6  size == (risk% x capital) / stop",
    "7  notional within leverage",
]


def _ema_ok(c, d, ema, mode):
    """Step 3, mirroring the strategy's ema_mode."""
    if d > 0 and c.open < ema <= c.close:
        return True
    if d < 0 and c.close <= ema < c.open:
        return True
    if mode in ("cross_or_touch", "touch"):
        if d > 0 and c.low <= ema < c.close:
            return True
        if d < 0 and c.high >= ema > c.close:
            return True
    if mode == "touch":
        return c.low <= ema <= c.high
    return False


def audit_one(cfg, store, t, verbose=False, level_store=None, balance=None):
    """`store` holds execution candles; `level_store` the (finer) grid candles."""
    d = date.fromisoformat(t["date"])
    exec_ms = interval_to_ms(cfg.market.interval)
    lvl_ms = interval_to_ms(cfg.market.level_interval)

    # Execution timeframe: the setup candles and the EMA live here.
    win = window_for(d, exec_ms)
    cs = store.load_range(d, d)
    times = [c.open_time for c in cs]
    sess = cs[bisect.bisect_left(times, win.session_start_ms):
              bisect.bisect_left(times, win.session_end_ms)]
    lon = cs[bisect.bisect_left(times, win.session_end_ms):
             bisect.bisect_left(times, win.london_end_ms)]

    # Level timeframe: the grid is rebuilt from its own, finer candles.
    lstore = level_store or store
    lwin = window_for(d, lvl_ms)
    lcs = lstore.load_range(d, d)
    ltimes = [c.open_time for c in lcs]
    lsess = lcs[bisect.bisect_left(ltimes, lwin.session_start_ms):
                bisect.bisect_left(ltimes, lwin.session_end_ms)]
    anc_ms = interval_to_ms(cfg.market.anchor_interval)
    awin = window_for(d, anc_ms)
    want_anchor = anchor_open_ms_for(awin, cfg.market.anchor_position)
    anchor_src = cs if anc_ms == exec_ms else lcs
    lanchor = next((c for c in anchor_src if c.open_time == want_anchor), None)
    ls = compute_levels(lanchor, lsess, lwin, cfg.levels)

    entry = float(t["entry_price"])
    tick = cfg.market.tick_size
    slip = cfg.backtest.slippage_ticks * tick + tick / 2

    # The engine records which candle produced the signal. With a post-only
    # entry the fill lands on a LATER candle, so inferring the setup from the
    # fill price would pick the wrong candle whenever another close happens to
    # match. Use the recorded time.
    sig_t = int(t.get("signal_time") or 0) or int(t["entry_time"])
    idx = [k for k, c in enumerate(lon) if c.open_time == sig_t]
    if not idx or idx[0] == 0:
        return {c: False for c in CHECKS}
    i = idx[0]
    opp, brk = lon[i], lon[i - 1]

    ema = Ema(cfg.strategy_params.get("ema_period", 7))
    for c in sess:
        ema.update(c.close)
    for c in lon[:i - 1]:
        ema.update(c.close)
    ema_at_break = ema.update(brk.close)

    db, do = direction(brk), direction(opp)
    _lt = cfg.strategy_params.get("level_cross_tol_ticks", 0) * cfg.market.tick_size
    _db = 1 if brk.close > brk.open else -1
    crossed = [l for l in ls.levels
               if (_db > 0 and brk.open < l.price + _lt and brk.close >= l.price)
               or (_db < 0 and brk.open > l.price - _lt and brk.close <= l.price)]

    p = cfg.strategy_params
    max_wick = p.get("max_wick_ticks", None)
    body_min = p.get("body_min_pct", 0.0) / 100.0
    tick = cfg.market.tick_size
    stop_usdt = p.get("stop_usdt", 100.0)
    target_usdt = p.get("target_usdt", 756.0)
    risk_pct = p.get("risk_pct", 3.0)
    cap = cfg.backtest.initial_equity
    exit_ = float(t["exit_price"])

    if t["exit_reason"] == "stop":
        dist_ok = abs(abs(exit_ - entry) - stop_usdt) <= 1.0
    elif t["exit_reason"] == "target":
        dist_ok = abs(abs(exit_ - entry) - target_usdt) <= 1.0
    else:
        dist_ok = abs(exit_ - entry) <= target_usdt + 1.0

    res = {
        CHECKS[0]: bool(crossed),
        CHECKS[1]: qualifies(brk, tick, max_wick, body_min),
        CHECKS[2]: qualifies(opp, tick, max_wick, body_min),
        CHECKS[3]: _ema_ok(brk, db, ema_at_break,
                           p.get("ema_mode", "cross")),
        CHECKS[4]: do == -db and do != 0,
        CHECKS[5]: t["side"] == ("long" if do > 0 else "short"),
        CHECKS[6]: abs(entry - opp.close) <= slip,
        CHECKS[7]: dist_ok,
        # With compound sizing the balance moves, so verify against the balance
        # carried in by the caller; otherwise against the fixed starting capital.
        CHECKS[8]: abs(float(t["qty"])
                       - round(((balance if balance is not None else cap)
                                * risk_pct / 100.0) / stop_usdt
                               / cfg.market.step_size) * cfg.market.step_size) < 1e-9,
        CHECKS[9]: float(t["qty"]) * entry <= cap * cfg.backtest.leverage,
    }

    if verbose:
        print(f"TRADE {t['date']} {t['side'].upper()} entry {entry:,.1f} "
              f"exit {exit_:,.1f} ({t['exit_reason']})")
        print(f"  BREAK    {to_ist_str(brk.open_time,'%H:%M')} O{brk.open:,.1f} C{brk.close:,.1f} "
              f"body {body_ratio(brk)*100:.1f}% wick {wick_ticks(brk,tick)}  EMA {ema_at_break:,.1f}")
        print(f"  OPPOSITE {to_ist_str(opp.open_time,'%H:%M')} O{opp.open:,.1f} C{opp.close:,.1f} "
              f"body {body_ratio(opp)*100:.1f}% wick {wick_ticks(opp,tick)}")
        for k, v in res.items():
            print(f"    {'PASS' if v else 'FAIL'}  step {k}")
        print()
    return res


def main():
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("out/reports/trades.csv")
    cfg = cm.load()
    store = HistoryStore(cfg.market.symbol, cfg.market.interval, cfg.paths.cache_dir,
                         FuturesClient(testnet=False), verbose=False)
    level_store = HistoryStore(cfg.market.symbol, cfg.market.level_interval,
                               cfg.paths.cache_dir, FuturesClient(testnet=False),
                               verbose=False)
    rows = list(csv.DictReader(path.open()))
    if not rows:
        print("no trades to audit")
        return 0

    print(f"Auditing {len(rows)} trades from {path} against the 7-step spec\n")
    for t in rows[:2]:
        audit_one(cfg, store, t, verbose=True, level_store=level_store)

    agg = {c: 0 for c in CHECKS}
    bal = cfg.backtest.initial_equity
    compound = bool(cfg.strategy_params.get("compound", False))
    for t in sorted(rows, key=lambda r: int(r["entry_time"])):
        for k, v in audit_one(cfg, store, t, level_store=level_store,
                              balance=bal if compound else None).items():
            agg[k] += 1 if v else 0
        bal += float(t["pnl_net"])

    print(f"=== all {len(rows)} trades ===")
    failed = 0
    for k, v in agg.items():
        ok = v == len(rows)
        failed += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {v}/{len(rows)}  step {k}")
    print()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
