"""Parameter sensitivity sweep for break-fade.

Answers: is the negative backtest a property of the rules, or of the particular
stop/target/level choices? Run:  python3 tools/sweep.py
"""

from __future__ import annotations

import sys
from dataclasses import replace
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aar import config as cm
from aar import strategies  # noqa: F401
from aar.backtest.engine import BacktestEngine
from aar.backtest.report import compute_metrics
from aar.core.rules import get_strategy
from aar.core.sessions import daterange
from aar.data.history import HistoryStore
from aar.data.rest import FuturesClient

START, END = date(2025, 8, 1), date(2026, 8, 1)


def load():
    cfg = cm.load()
    store = HistoryStore(cfg.market.symbol, cfg.market.interval, cfg.paths.cache_dir,
                         FuturesClient(testnet=False), verbose=False)
    return cfg, store.load_range(START, END), list(daterange(START, END))


def run(cfg, candles, days, **params):
    eng = BacktestEngine(cfg, {})
    res = eng.run(candles, days, lambda: get_strategy("break-fade", cfg=cfg, **params))
    return compute_metrics(res)


def header(title):
    print(f"\n{title}")
    print(f"  {'variant':<26}{'trades':>7}{'win%':>7}{'gross':>11}{'fees':>11}"
          f"{'net':>11}{'ret%':>9}{'PF':>7}")
    print("  " + "-" * 89)


def row(label, m):
    print(f"  {label:<26}{m.trades:>7}{m.win_rate:>7.1f}{m.gross_pnl:>11,.0f}"
          f"{-m.fees:>11,.0f}{m.net_pnl:>11,.0f}{m.return_pct:>9.1f}{m.profit_factor:>7.2f}")


def main():
    cfg, candles, days = load()
    print(f"BTCUSDT perp  {START} -> {END}   {len(candles):,} candles")
    print(f"capital {cfg.backtest.initial_equity:,.0f} USDT   leverage {cfg.backtest.leverage:g}x"
          f"   taker {cfg.backtest.taker_fee*100:.3f}%")

    # ---- 1. stop size, R:R held at 1:7.5 ------------------------------------
    header("1. Stop size (target scaled to keep R:R at 1:7.5, risk fixed at 3%)")
    for stop in (100, 200, 300, 500, 750, 1000, 1500):
        m = run(cfg, candles, days, stop_usdt=stop, target_usdt=stop * 7.5)
        row(f"stop ${stop} / tgt ${stop*7.5:.0f}", m)

    # ---- 2. reward:risk at the spec's $100 stop -----------------------------
    header("2. Reward:risk multiple (stop fixed at $100 per spec)")
    for rr in (1, 2, 3, 5, 7.5, 10, 15):
        m = run(cfg, candles, days, stop_usdt=100, target_usdt=100 * rr)
        row(f"R:R 1:{rr:g}", m)

    # ---- 3. which levels arm a trigger --------------------------------------
    header("3. Which levels arm a trigger (spec stop/target)")
    for kinds in ("all", "full", "anchor"):
        m = run(cfg, candles, days, level_kinds=kinds)
        row(f"levels = {kinds}", m)

    # ---- 4. fee sensitivity --------------------------------------------------
    header("4. Fee sensitivity (spec params) — how much of this is costs?")
    base_fee = cfg.backtest.taker_fee
    for label, fee in (("taker 0.050% (real)", 0.0005),
                       ("maker 0.020%", 0.0002),
                       ("VIP/rebate 0.010%", 0.0001),
                       ("zero fees", 0.0)):
        cfg.backtest.taker_fee = fee
        m = run(cfg, candles, days)
        row(label, m)
    cfg.backtest.taker_fee = base_fee

    # ---- 5. wick tolerance ---------------------------------------------------
    header("5. Wick tolerance (step 2) — 0 ticks is the literal 100%-body rule")
    for w in (0, 1, 2, 5, 10, 20):
        m = run(cfg, candles, days, max_wick_ticks=w)
        row(f"wick <= {w} tick (${w*0.1:.1f})", m)

    # ---- 6. direction: is the fade the right way round? ---------------------
    header("6. Sanity check — does the OPPOSITE direction do better?")
    m = run(cfg, candles, days)
    row("fade (as specified)", m)
    print("  note: a mirrored 'follow the break' variant would need its own strategy class;")
    print("        the gross figure above already tells us the fade edge is positive but small.")

    # ---- 7. best combination found ------------------------------------------
    header("7. Larger stop + all levels, at maker fees")
    cfg.backtest.taker_fee = 0.0002
    for stop in (300, 500, 750):
        m = run(cfg, candles, days, stop_usdt=stop, target_usdt=stop * 7.5)
        row(f"stop ${stop} @ maker", m)
    cfg.backtest.taker_fee = base_fee
    print()


if __name__ == "__main__":
    main()
