"""Backtest engine mechanics.

These use throwaway strategies defined in-test. They verify the *engine*
(fills, fees, stops, funding, flattening, sizing limits), not any trading idea —
the real rules are the user's to write.
"""

import unittest

from aar.core import models
from datetime import date

from aar.backtest.engine import BacktestEngine
from aar.config import Config
from aar.core.models import Action, Candle, Side, Signal
from aar.core.rules import BaseStrategy
from aar.core.sessions import window_for

models.set_interval_ms(900000)   # tests are written against 15m candles

WIN = window_for(date(2026, 8, 1))


def session_candles():
    """140 candles producing two clean dojis in opposite halves."""
    out, t = [], WIN.session_start_ms
    specs = [
        (60_500.0, 60_600.0, 60_400.0, 60_500.0),   # upper doji
        (60_000.0, 60_050.0, 59_900.0, 60_000.0),   # lower doji
    ]
    for o, h, l, c in specs:
        out.append(Candle(t, o, h, l, c, 1.0)); t += 180_000
    while len(out) < 140:
        out.append(Candle(t, 60_200.0, 60_260.0, 60_150.0, 60_250.0, 1.0)); t += 180_000
    return out


def london_candles(n=120, price=60_100.0, drift=0.0, wick=30.0):
    out, t = [], WIN.session_end_ms
    p = price
    for _ in range(n):
        out.append(Candle(t, p, p + wick, p - wick, p + drift, 1.0))
        p += drift
        t += 180_000
    return out


ANCHOR = Candle(WIN.anchor_open_ms, 60_100.0, 60_100.0, 60_100.0, 60_100.0, 1.0)


def cfg_for(**over):
    c = Config()
    c.backtest.initial_equity = 100_000.0
    c.backtest.slippage_ticks = 0
    c.backtest.taker_fee = 0.0
    c.backtest.leverage = 1.0
    c.risk.max_qty = 10.0
    for k, v in over.items():
        obj, _, attr = k.rpartition(".")
        target = c
        for part in obj.split("."):
            target = getattr(target, part)
        setattr(target, attr, v)
    return c


class OpenOnce(BaseStrategy):
    name = "test-open-once"

    def __init__(self, action=Action.OPEN_LONG, qty=1.0, stop=None, target=None):
        self.action, self.qty, self.stop, self.target = action, qty, stop, target
        self.fired = False
        self.contexts = []

    def on_candle(self, ctx):
        self.contexts.append(ctx)
        if self.fired:
            return ()
        self.fired = True
        sigs = [Signal(self.action, qty=self.qty, reason="test-entry")]
        if self.stop:
            sigs.append(Signal(Action.SET_STOP, price=self.stop))
        if self.target:
            sigs.append(Signal(Action.SET_TARGET, price=self.target))
        return sigs


class TestEngineMechanics(unittest.TestCase):
    def run_day(self, strat, cfg=None, london=None, funding=None):
        cfg = cfg or cfg_for()
        eng = BacktestEngine(cfg, funding or {})
        return eng.run_day(WIN, ANCHOR, session_candles(),
                           london if london is not None else london_candles(),
                           strat, cfg.backtest.initial_equity)

    def test_null_strategy_trades_nothing(self):
        from aar.core.rules import NullStrategy
        r = self.run_day(NullStrategy())
        self.assertEqual(r.trades, [])
        self.assertEqual(r.equity_end, r.equity_start)

    def test_long_profits_on_upward_drift(self):
        r = self.run_day(OpenOnce(qty=1.0), london=london_candles(drift=1.0))
        self.assertEqual(len(r.trades), 1)
        t = r.trades[0]
        self.assertIs(t.side, Side.LONG)
        self.assertGreater(t.pnl_gross, 0)
        self.assertEqual(t.exit_reason, "london_close")

    def test_short_profits_on_downward_drift(self):
        r = self.run_day(OpenOnce(action=Action.OPEN_SHORT, qty=1.0),
                         london=london_candles(drift=-1.0))
        t = r.trades[0]
        self.assertIs(t.side, Side.SHORT)
        self.assertGreater(t.pnl_gross, 0)

    def test_stop_is_honoured_intrabar(self):
        r = self.run_day(OpenOnce(qty=1.0, stop=60_000.0),
                         london=london_candles(drift=-5.0))
        self.assertEqual(r.trades[0].exit_reason, "stop")
        self.assertAlmostEqual(r.trades[0].exit_price, 60_000.0, places=4)

    def test_target_is_honoured_intrabar(self):
        r = self.run_day(OpenOnce(qty=1.0, target=60_300.0),
                         london=london_candles(drift=5.0))
        self.assertEqual(r.trades[0].exit_reason, "target")
        self.assertAlmostEqual(r.trades[0].exit_price, 60_300.0, places=4)

    def test_stop_wins_when_one_candle_contains_both(self):
        """3m data cannot say which came first, so the engine must take the worse branch."""
        # a single very wide candle spanning both stop and target
        wide = [Candle(WIN.session_end_ms, 60_100.0, 60_100.0, 60_100.0, 60_100.0, 1.0),
                Candle(WIN.session_end_ms + 180_000, 60_100.0, 61_000.0, 59_000.0, 60_100.0, 1.0)]
        r = self.run_day(OpenOnce(qty=1.0, stop=59_500.0, target=60_800.0), london=wide)
        self.assertEqual(r.trades[0].exit_reason, "stop")

    def test_fees_are_charged_on_both_sides(self):
        cfg = cfg_for(**{"backtest.taker_fee": 0.0005})
        r = self.run_day(OpenOnce(qty=1.0), cfg=cfg, london=london_candles())
        t = r.trades[0]
        self.assertGreater(t.fees, 0)
        # entry + exit at ~60,100 each: 2 * 1.0 * 60100 * 0.0005 ~= 60.1
        self.assertAlmostEqual(t.fees, 2 * 60_100.0 * 0.0005, delta=2.0)

    def test_funding_charged_at_0800_utc_for_a_long(self):
        stamp = None
        from aar.core.sessions import funding_times_between
        stamp = funding_times_between(WIN.session_end_ms, WIN.london_end_ms)[0]
        r = self.run_day(OpenOnce(qty=1.0), funding={stamp: 0.0001})
        self.assertGreater(r.trades[0].funding, 0)  # long pays a positive rate

    def test_funding_is_received_by_a_short_on_positive_rate(self):
        from aar.core.sessions import funding_times_between
        stamp = funding_times_between(WIN.session_end_ms, WIN.london_end_ms)[0]
        r = self.run_day(OpenOnce(action=Action.OPEN_SHORT, qty=1.0),
                         funding={stamp: 0.0001})
        self.assertLess(r.trades[0].funding, 0)

    def test_slippage_works_against_entry_and_exit(self):
        cfg = cfg_for(**{"backtest.slippage_ticks": 10})  # $1.00
        r = self.run_day(OpenOnce(qty=1.0), cfg=cfg, london=london_candles())
        t = r.trades[0]
        self.assertGreater(t.entry_price, 60_100.0)  # paid up to buy
        self.assertLess(t.exit_price, 60_100.0)      # sold down to exit

    def test_explicit_close_signal(self):
        class CloseAfterTen(OpenOnce):
            def on_candle(self, ctx):
                if ctx.index == 10 and ctx.position:
                    return [Signal(Action.CLOSE, reason="ten")]
                return super().on_candle(ctx)
        r = self.run_day(CloseAfterTen(qty=1.0))
        self.assertEqual(r.trades[0].exit_reason, "ten")

    def test_below_min_notional_is_refused(self):
        r = self.run_day(OpenOnce(qty=0.0001))  # ~$6 notional, min is $50
        self.assertEqual(r.trades, [])

    def test_max_trades_per_day_is_enforced(self):
        class Churn(BaseStrategy):
            name = "churn"
            def on_candle(self, ctx):
                if ctx.position is None:
                    return [Signal(Action.OPEN_LONG, qty=1.0)]
                return [Signal(Action.CLOSE)]
        cfg = cfg_for(**{"risk.max_trades_per_day": 3})
        r = self.run_day(Churn(), cfg=cfg)
        self.assertEqual(len(r.trades), 3)

    def test_open_position_is_booked_even_when_flatten_is_disabled(self):
        """Levels expire at 18:30 and nothing carries overnight, so an open
        position must still be booked — dropping it would strand the entry fee
        and hide the trade from the results entirely."""
        cfg = cfg_for(**{"risk.flatten_at_london_close": False})
        r = self.run_day(OpenOnce(qty=1.0), cfg=cfg)
        self.assertEqual(len(r.trades), 1)
        self.assertEqual(r.trades[0].exit_reason, "day_end_unflattened")

    def test_invalid_day_is_skipped_without_trading(self):
        cfg = cfg_for()
        eng = BacktestEngine(cfg, {})
        flat = [Candle(WIN.session_start_ms + i * 180_000,
                       60_000.0, 60_100.0, 59_900.0, 60_050.0, 1.0) for i in range(140)]
        from aar.core.models import DojiMode
        from dataclasses import replace
        cfg.levels = replace(cfg.levels, doji_mode=DojiMode.STRICT)
        r = eng.run_day(WIN, ANCHOR, flat, london_candles(), OpenOnce(qty=1.0), 100_000.0)
        self.assertFalse(r.levels.valid)
        self.assertEqual(r.trades, [])
        self.assertIsNotNone(r.skipped)


class TestContextHelpers(unittest.TestCase):
    def test_helpers_see_the_frozen_grid(self):
        strat = OpenOnce(qty=1.0)
        cfg = cfg_for()
        BacktestEngine(cfg, {}).run_day(
            WIN, ANCHOR, session_candles(), london_candles(), strat, 100_000.0)
        ctx = strat.contexts[0]
        self.assertTrue(ctx.levels.valid)
        self.assertEqual(ctx.spacing, ctx.levels.spacing_d)
        self.assertEqual(len(ctx.levels.levels), 13)

        above = ctx.level_above(ctx.price)
        below = ctx.level_below(ctx.price)
        self.assertIsNotNone(above)
        self.assertIsNotNone(below)
        self.assertGreater(above.price, ctx.price)
        self.assertLess(below.price, ctx.price)

        nearest = ctx.nearest_level()
        self.assertLessEqual(
            abs(nearest.price - ctx.price),
            min(abs(above.price - ctx.price), abs(ctx.price - below.price)) + 1e-9,
        )

    def test_cross_detection(self):
        strat = OpenOnce(qty=1.0)
        cfg = cfg_for()
        # drift upward strongly so some candle crosses a level
        BacktestEngine(cfg, {}).run_day(
            WIN, ANCHOR, session_candles(), london_candles(drift=8.0), strat, 100_000.0)
        self.assertTrue(any(c.crossed_up() for c in strat.contexts))


if __name__ == "__main__":
    unittest.main()
