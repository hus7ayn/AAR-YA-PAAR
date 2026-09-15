"""Command line entry point.

    python3 -m aar levels   --date 2026-08-15
    python3 -m aar diagnose --start 2025-08-01 --end 2026-08-01
    python3 -m aar chart    --date 2026-08-15
    python3 -m aar backtest --start 2025-08-01 --end 2026-08-01
    python3 -m aar verify
    python3 -m aar live     --once
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from datetime import date as Date, datetime, timedelta

from . import config as configmod
from . import strategies  # noqa: F401  — registers the strategy implementations
from .backtest.engine import BacktestEngine
from .backtest.report import (
    compute_metrics, diagnose, render_diagnostics_table,
    write_levels_json, write_trades_csv,
)
from .core.levels import compute_levels
from .core.models import DojiMode
from .core.rules import available, get_strategy
from .core.sessions import daterange, to_ist_str, window_for
from .data.history import HistoryStore, find_gaps
from .data.rest import FuturesClient



def _parse_date(s: str) -> Date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _store(cfg, interval: str | None = None) -> HistoryStore:
    return HistoryStore(cfg.market.symbol, interval or cfg.market.interval,
                        cfg.paths.cache_dir, client=FuturesClient(testnet=False))


def _level_store(cfg) -> HistoryStore:
    """Candles for the level grid — usually a finer timeframe than execution."""
    return _store(cfg, cfg.market.level_interval)


def _level_win(cfg, d):
    from .core.models import interval_to_ms
    return window_for(d, interval_to_ms(cfg.market.level_interval))


def _anchor_candle(cfg, d):
    """The anchor is taken from its OWN timeframe — the candle that CLOSES at
    05:30 IST there. At 15m that is the 05:15-05:30 candle, not the 3m 05:27."""
    from .core.models import interval_to_ms
    from .core.sessions import anchor_open_ms_for
    ams = interval_to_ms(cfg.market.anchor_interval)
    aw = window_for(d, ams)
    want = anchor_open_ms_for(aw, cfg.market.anchor_position)
    for c in _store(cfg, cfg.market.anchor_interval).load_range(d, d):
        if c.open_time == want:
            return c
    return None


def _slice_day(candles, win):
    import bisect
    times = [c.open_time for c in candles]

    def sl(lo, hi):
        return candles[bisect.bisect_left(times, lo):bisect.bisect_left(times, hi)]

    by_time = {c.open_time: c for c in candles}
    return (by_time.get(win.anchor_open_ms),
            sl(win.session_start_ms, win.session_end_ms),
            sl(win.session_end_ms, win.london_end_ms))


# --------------------------------------------------------------------- levels

def cmd_levels(args, cfg):
    d = _parse_date(args.date) if args.date else Date.today()
    win = _level_win(cfg, d)
    candles = _level_store(cfg).load_range(d, d)
    _, session, london = _slice_day(candles, win)
    ls = compute_levels(_anchor_candle(cfg, d), session, win, cfg.levels)

    if args.json:
        print(json.dumps(ls.to_json_obj(), indent=2))
        return 0

    print(f"\n  {ls.date}  {cfg.market.symbol}  ({cfg.levels.doji_mode.value} mode)")
    print(f"  {'-'*62}")
    _lbl = ("05:30-05:45" if cfg.market.anchor_position == "first_session"
            else "05:15-05:30")
    print(f"  anchor ({cfg.market.anchor_interval} {_lbl} IST close) {ls.anchor:>12,.1f}")
    print(f"  session high / low               {ls.session_high:>12,.1f} / {ls.session_low:,.1f}")
    print(f"  session candles                  {ls.session_candle_count:>12}")

    if not ls.valid:
        print(f"\n  INVALID: {ls.reason.value}\n")
        return 1

    print(f"  upper doji close                 {ls.upper_doji.close:>12,.1f}  "
          f"@ {to_ist_str(ls.upper_doji.open_time,'%H:%M')} "
          f"(body {ls.upper_doji.body_ticks} tick)")
    print(f"  lower doji close                 {ls.lower_doji.close:>12,.1f}  "
          f"@ {to_ist_str(ls.lower_doji.open_time,'%H:%M')} "
          f"(body {ls.lower_doji.body_ticks} tick)")
    print(f"  D (spacing)                      {ls.spacing_d:>12,.1f}")
    print(f"  half-D (midpoint step)           {ls.spacing_d/2:>12,.1f}")
    print(f"\n  Levels valid {to_ist_str(ls.valid_from_ms)} -> {to_ist_str(ls.valid_to_ms)} IST\n")
    print(f"    {'k':>3}  {'kind':<7} {'price':>12}")
    for lv in reversed(ls.levels):
        mark = " <- anchor" if lv.k == 0 else ""
        print(f"    {lv.k:>+3}  {lv.kind.value:<7} {lv.price:>12,.1f}{mark}")
    print()

    out = cfg.paths.levels_dir / f"{ls.date}.json"
    cfg.paths.ensure()
    out.write_text(json.dumps(ls.to_json_obj(), indent=2, sort_keys=True))
    print(f"  written: {out}\n")
    return 0


# ------------------------------------------------------------------ diagnose

def cmd_diagnose(args, cfg):
    start = _parse_date(args.start or cfg.backtest.start)
    end = _parse_date(args.end or cfg.backtest.end)
    days = list(daterange(start, end))

    print(f"\n  Loading {cfg.market.symbol} {cfg.market.interval} candles "
          f"{start} -> {end} ...")
    candles = _level_store(cfg).load_range(start, end)
    print(f"  {len(candles):,} candles loaded")
    gaps = find_gaps(candles)
    if gaps:
        missing = sum(n for _, n in gaps)
        print(f"  [warn] {len(gaps)} gap(s), {missing} missing candles")

    modes = [DojiMode.STRICT, DojiMode.TICK, DojiMode.ABS, DojiMode.ADAPTIVE]
    diags, per_mode = [], {}
    for mode in modes:
        lcfg = replace(cfg.levels, doji_mode=mode)
        sets = []
        for d in days:
            win = _level_win(cfg, d)
            _, session, _ = _slice_day(candles, win)
            anchor = _anchor_candle(cfg, d)
            if anchor is None and not session:
                continue  # no data at all for this day
            sets.append(compute_levels(anchor, session, win, lcfg))
        label = mode.value
        if mode is DojiMode.ABS:
            label = f"abs({cfg.levels.doji_tol_ticks}t)"
        diags.append(diagnose(label, sets))
        per_mode[label] = sets

    print(f"\n  Doji-mode comparison — {len(days)} calendar days\n")
    print("  " + render_diagnostics_table(diags).replace("\n", "\n  "))

    print("\n  Failure breakdown")
    for dg in diags:
        if dg.reasons:
            parts = ", ".join(f"{k}={v}" for k, v in sorted(dg.reasons.items()))
            print(f"    {dg.mode:<12} {parts}")
        else:
            print(f"    {dg.mode:<12} (none)")

    cfg.paths.ensure()
    rep = cfg.paths.reports_dir / "doji_mode_diagnostics.json"
    rep.write_text(json.dumps([d.summary_row() for d in diags], indent=2))
    for label, sets in per_mode.items():
        safe = label.replace("(", "_").replace(")", "").replace("+", "")
        write_levels_json(sets, cfg.paths.reports_dir / f"levels_{safe}.json")
    print(f"\n  written: {rep}")
    print(f"  written: {cfg.paths.reports_dir}/levels_<mode>.json\n")
    return 0


# --------------------------------------------------------------------- chart

def cmd_chart(args, cfg):
    from .viz.chart import render_day

    cfg.paths.ensure()
    if args.date:
        days = [_parse_date(args.date)]
    else:
        start = _parse_date(args.start or cfg.backtest.start)
        end = _parse_date(args.end or cfg.backtest.end)
        days = list(daterange(start, end))[: args.limit]

    candles = _store(cfg).load_range(days[0], days[-1])
    written = []
    for d in days:
        win = window_for(d)
        anchor, session, london = _slice_day(candles, win)
        if not session:
            continue
        ls = compute_levels(anchor, session, win, cfg.levels)
        day_candles = session + london
        p = render_day(day_candles, ls, win, cfg.paths.charts_dir / f"{d.isoformat()}.svg")
        written.append(p)
        state = "ok" if ls.valid else f"INVALID {ls.reason.value}"
        print(f"  {d}  {state:<28} {p}")
    print(f"\n  {len(written)} chart(s) in {cfg.paths.charts_dir}\n")
    return 0


# ------------------------------------------------------------------ backtest

def cmd_backtest(args, cfg):
    start = _parse_date(args.start or cfg.backtest.start)
    end = _parse_date(args.end or cfg.backtest.end)
    days = list(daterange(start, end))
    name = args.strategy or cfg.strategy_name

    print(f"\n  Backtest {cfg.market.symbol}  {start} -> {end}")
    print(f"  strategy: {name}   (registered: {', '.join(available())})")
    if name == "null":
        print("  note: the 'null' strategy takes no trades — this run validates the")
        print("        pipeline and level coverage, not profitability.")

    candles = _store(cfg).load_range(start, end)
    print(f"  {len(candles):,} candles loaded")

    funding = {}
    if cfg.backtest.use_real_funding:
        try:
            win_a, win_b = window_for(days[0]), window_for(days[-1])
            client = FuturesClient(testnet=False)
            rows = client.funding_history(cfg.market.symbol,
                                          win_a.session_start_ms, win_b.london_end_ms)
            funding = dict(rows)
            print(f"  {len(funding)} funding stamps loaded")
        except Exception as e:
            print(f"  [warn] funding history unavailable ({e}); using flat rate")

    lvl = _level_store(cfg).load_range(start, end) \
        if cfg.market.level_interval != cfg.market.interval else candles
    if lvl is not candles:
        print(f"  {len(lvl):,} {cfg.market.level_interval} candles for the level grid")

    engine = BacktestEngine(cfg, funding)
    res = engine.run(candles, days, lambda: get_strategy(name, cfg=cfg),
                     level_candles=lvl)
    m = compute_metrics(res)

    print(f"\n  Results\n{'-'*66}")
    print(m.render())

    cfg.paths.ensure()
    write_trades_csv(res, cfg.paths.reports_dir / "trades.csv")
    write_levels_json([d.levels for d in res.days],
                      cfg.paths.reports_dir / "backtest_levels.json")
    print(f"\n  written: {cfg.paths.reports_dir}/trades.csv")
    print(f"  written: {cfg.paths.reports_dir}/backtest_levels.json\n")
    return 0


# -------------------------------------------------------------------- verify

def cmd_verify(args, cfg):
    """Cross-check cached/archive data and the engine against the live exchange."""
    ok = True
    client = FuturesClient(testnet=False)

    print("\n  1. exchange filters vs config")
    f = client.filters(cfg.market.symbol)
    for key, got in f.items():
        want = getattr(cfg.market, key, None)
        match = want is not None and abs(want - got) < 1e-12
        ok &= match
        print(f"     {key:<14} exchange={got:<10} config={want:<10} "
              f"{'OK' if match else 'MISMATCH'}")

    print("\n  2. anchor candle from live REST vs archive")
    d = _parse_date(args.date) if args.date else (Date.today() - timedelta(days=1))
    win = window_for(d)
    live = client.klines(cfg.market.symbol, cfg.market.interval,
                         win.anchor_open_ms, win.anchor_open_ms + 1, 1)
    if not live:
        print("     [warn] no live candle returned")
        ok = False
    else:
        lc = live[0]
        candles = _store(cfg).load_range(d, d)
        anchor, session, _ = _slice_day(candles, win)
        print(f"     date                {d}")
        from .core.sessions import to_ist_str as _t
        expect_anchor = _t(win.anchor_open_ms, '%H:%M')
        print(f"     anchor open (IST)   {to_ist_str(lc.open_time,'%Y-%m-%d %H:%M')}"
              f"  (expect {expect_anchor})")
        ok &= lc.open_time == win.anchor_open_ms
        print(f"     live close          {lc.close:,.1f}")
        if anchor:
            same = abs(anchor.close - lc.close) < 1e-9
            ok &= same
            print(f"     archive close       {anchor.close:,.1f}  "
                  f"{'OK' if same else 'MISMATCH'}")
        else:
            ok = False
            print("     archive close       MISSING")

        from .core.sessions import expected_session_candles
        need = expected_session_candles()
        print(f"     session candles     {len(session)}  (expect {need})")
        ok &= len(session) == need
        ls = compute_levels(anchor, session, win, cfg.levels)
        # An invalid day is a legitimate outcome, not a system failure.
        print(f"     levels              {'valid' if ls.valid else ls.reason.value}"
              f"{'' if ls.valid else '  (a valid outcome, not a failure)'}")
        if ls.valid:
            print(f"     D                   {ls.spacing_d:,.1f}")

    print("\n  3. determinism")
    if 'ls' in dir():
        a = json.dumps(ls.to_json_obj(), sort_keys=True)
        b = json.dumps(compute_levels(anchor, session, win, cfg.levels).to_json_obj(),
                       sort_keys=True)
        same = a == b
        ok &= same
        print(f"     repeat run identical  {'OK' if same else 'DIFFERS'}")

    print(f"\n  {'ALL CHECKS PASSED' if ok else 'SOME CHECKS FAILED'}\n")
    return 0 if ok else 1


# ---------------------------------------------------------------------- live

def cmd_live(args, cfg):
    from .live.runner import LiveRunner
    runner = LiveRunner(cfg)
    return runner.run_once() if args.once else runner.run_forever()


# ---------------------------------------------------------------------- main

def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="aar", description="AAR-YA-PAAR session level system")
    p.add_argument("--config", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("levels", help="compute and print levels for one day")
    sp.add_argument("--date")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_levels)

    sp = sub.add_parser("diagnose", help="compare doji modes over a date range")
    sp.add_argument("--start")
    sp.add_argument("--end")
    sp.set_defaults(fn=cmd_diagnose)

    sp = sub.add_parser("chart", help="render SVG charts with the level grid")
    sp.add_argument("--date")
    sp.add_argument("--start")
    sp.add_argument("--end")
    sp.add_argument("--limit", type=int, default=10)
    sp.set_defaults(fn=cmd_chart)

    sp = sub.add_parser("backtest", help="replay a strategy over history")
    sp.add_argument("--start")
    sp.add_argument("--end")
    sp.add_argument("--strategy")
    sp.set_defaults(fn=cmd_backtest)

    sp = sub.add_parser("verify", help="cross-check engine and data against live exchange")
    sp.add_argument("--date")
    sp.set_defaults(fn=cmd_verify)

    sp = sub.add_parser("live", help="run the scheduler (testnet by default)")
    sp.add_argument("--once", action="store_true")
    sp.set_defaults(fn=cmd_live)

    args = p.parse_args(argv)
    cfg = configmod.load(args.config)
    cfg.paths.ensure()
    return args.fn(args, cfg)


if __name__ == "__main__":
    sys.exit(main())
