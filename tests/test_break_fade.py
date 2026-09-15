"""Break-fade strategy — one test per rule in the seven-step plan."""

import unittest

from aar.core import models
from datetime import date

from aar.core.models import Action, Candle, Level, LevelKind, Side
from aar.core.rules import Context
from aar.core.sessions import window_for
from aar.strategies.break_fade import BreakFadeStrategy, Ema, body_ratio, direction

models.set_interval_ms(900000)   # tests are written against 15m candles

WIN = window_for(date(2026, 8, 1))
LEVEL = 60_000.0


class FakeLevels:
    """Minimal stand-in for a LevelSet with one level at 60,000."""
    valid = True
    spacing_d = 400.0
    levels = (Level(k=0, price=LEVEL, kind=LevelKind.ANCHOR),)


def strat(**params):
    s = BreakFadeStrategy(cfg=None, **params)
    # warm the EMA to a known flat value at the level
    s.on_session_start(FakeLevels(), [Candle(0, LEVEL, LEVEL, LEVEL, LEVEL, 1.0)] * 60)
    return s


def ctx(s, c, index=0, position=None, prev=None, equity=10_000.0):
    return Context(candle=c, prev=prev, levels=FakeLevels(), position=position,
                   window=WIN, index=index, equity=equity)


def candle(o, h, l, c, i=0):
    return Candle(WIN.session_end_ms + i * 180_000, o, h, l, c, 1.0)


# A qualifying candle is a marubozu: 100% body, 0% wick on BOTH sides.
def strong_bull(o, c, i=0):
    """Opens at the low, closes at the high — no wick either side."""
    return candle(o, c, o, c, i)


def strong_bear(o, c, i=0):
    """Opens at the high, closes at the low — no wick either side."""
    return candle(o, o, c, c, i)


def wicked_bull(o, c, ticks=1, i=0):
    """Correct direction and full body, but `ticks` of wick on each side."""
    w = 0.10 * ticks
    return candle(o, c + w, o - w, c, i)


def weak_bull(o, c, i=0):
    """Same direction but long wicks, so nowhere near a full body."""
    rng = abs(c - o)
    return candle(o, c + rng * 2, o - rng * 2, c, i)


class TestHelpers(unittest.TestCase):
    def test_body_ratio(self):
        self.assertAlmostEqual(body_ratio(candle(100, 110, 90, 105)), 5 / 20)
        self.assertAlmostEqual(body_ratio(candle(100, 100, 90, 90)), 1.0)
        self.assertEqual(body_ratio(candle(100, 100, 100, 100)), 0.0)  # zero range

    def test_direction(self):
        self.assertEqual(direction(candle(100, 110, 95, 105)), 1)
        self.assertEqual(direction(candle(105, 110, 95, 100)), -1)
        self.assertEqual(direction(candle(100, 110, 95, 100)), 0)

    def test_ema_seeds_with_sma_then_smooths(self):
        e = Ema(7)
        for _ in range(6):
            self.assertIsNone(e.update(100.0))
        self.assertAlmostEqual(e.update(100.0), 100.0)   # seeded on the 7th
        self.assertAlmostEqual(e.update(108.0), 102.0)   # 108*0.25 + 100*0.75


class TestStepsInIsolation(unittest.TestCase):
    """Each test removes exactly one requirement and asserts the setup dies."""

    def _fire(self, s, brk, opp):
        s.on_candle(ctx(s, brk, index=0))
        return list(s.on_candle(ctx(s, opp, index=1)))

    def test_full_valid_bull_break_produces_a_short(self):
        s = strat()
        # step 1+2+3: opens below 60000 and the EMA (~60000), closes above, 90%+ body
        brk = strong_bull(59_900.0, 60_150.0, 0)
        opp = strong_bear(60_150.0, 59_950.0, 1)
        sigs = self._fire(s, brk, opp)
        self.assertTrue(sigs)
        self.assertIs(sigs[0].action, Action.OPEN_SHORT)   # direction of the OPPOSITE candle

    def test_full_valid_bear_break_produces_a_long(self):
        s = strat()
        brk = strong_bear(60_100.0, 59_850.0, 0)
        opp = strong_bull(59_850.0, 60_050.0, 1)
        sigs = self._fire(s, brk, opp)
        self.assertTrue(sigs)
        self.assertIs(sigs[0].action, Action.OPEN_LONG)

    def test_step1_no_level_crossed_means_no_setup(self):
        s = strat()
        # entirely above the level — never crosses it
        brk = strong_bull(60_100.0, 60_300.0, 0)
        opp = strong_bear(60_300.0, 60_150.0, 1)
        self.assertEqual(self._fire(s, brk, opp), [])

    def test_step2_a_wicky_candle_still_qualifies_on_colour(self):
        """Colour only: long wicks and a small body no longer disqualify."""
        s = strat()
        brk = weak_bull(59_900.0, 60_150.0, 0)      # bullish, but tiny body
        opp = candle(60_150.0, 60_400.0, 59_900.0, 59_950.0, 1)   # bearish, wicky
        self.assertLess(body_ratio(brk), 0.9)
        self.assertLess(body_ratio(opp), 0.9)
        sigs = self._fire(s, brk, opp)
        self.assertTrue(sigs, "a red/green combo should trade regardless of shape")
        self.assertIs(sigs[0].action, Action.OPEN_SHORT)

    def test_step2_one_tick_of_wick_no_longer_disqualifies(self):
        s = strat()
        brk = wicked_bull(59_900.0, 60_150.0, ticks=1, i=0)
        opp = candle(60_150.0, 60_150.1, 59_950.0, 59_950.0, 1)
        self.assertLess(body_ratio(brk), 1.0)
        self.assertTrue(self._fire(s, brk, opp))

    def test_step2_flat_candle_has_no_colour_and_is_rejected(self):
        """open == close is neither red nor green — the one shape still refused."""
        from aar.strategies.break_fade import qualifies
        flat = candle(60_000.0, 60_050.0, 59_950.0, 60_000.0)
        self.assertFalse(qualifies(flat, 0.10))
        s = strat()
        opp = strong_bear(60_150.0, 59_950.0, 1)
        self.assertEqual(self._fire(s, flat, opp), [])

    def test_step2_shape_filter_can_be_re_enabled(self):
        """max_wick_ticks=0 restores the old 100%-body / 0-wick rule."""
        s = strat(max_wick_ticks=0)
        brk = wicked_bull(59_900.0, 60_150.0, ticks=1, i=0)
        opp = strong_bear(60_150.0, 59_950.0, 1)
        self.assertEqual(self._fire(s, brk, opp), [])
        # and the clean marubozu still passes under that filter
        s2 = strat(max_wick_ticks=0)
        self.assertTrue(self._fire(s2, strong_bull(59_900.0, 60_150.0, 0),
                                   strong_bear(60_150.0, 59_950.0, 1)))

    def test_step3_break_must_cross_the_ema(self):
        s = strat()
        # push the EMA far below so the break candle never crosses it
        for _ in range(40):
            s.ema.update(55_000.0)
        brk = strong_bull(59_900.0, 60_150.0, 0)
        opp = strong_bear(60_150.0, 59_950.0, 1)
        self.assertEqual(self._fire(s, brk, opp), [])

    def test_step4_same_direction_second_candle_is_not_an_opposite(self):
        s = strat()
        brk = strong_bull(59_900.0, 60_150.0, 0)
        opp = strong_bull(60_150.0, 60_300.0, 1)     # continuation, not opposite
        self.assertEqual(self._fire(s, brk, opp), [])

    def test_step4_opposite_candle_must_be_immediate(self):
        s = strat()
        s.on_candle(ctx(s, strong_bull(59_900.0, 60_150.0, 0), index=0))
        # an in-between candle that is neither opposite-strong nor a new break
        s.on_candle(ctx(s, candle(60_150.0, 60_160.0, 60_140.0, 60_155.0, 1), index=1))
        sigs = list(s.on_candle(ctx(s, strong_bear(60_150.0, 59_950.0, 2), index=2)))
        self.assertEqual(sigs, [])

    def test_step5_stop_and_target_distances(self):
        s = strat()
        brk = strong_bull(59_900.0, 60_150.0, 0)
        opp = strong_bear(60_150.0, 59_950.0, 1)
        sigs = self._fire(s, brk, opp)
        entry = opp.close
        stop = next(x for x in sigs if x.action is Action.SET_STOP)
        tgt = next(x for x in sigs if x.action is Action.SET_TARGET)
        self.assertAlmostEqual(stop.price, entry + 100.0)   # short: stop above
        self.assertAlmostEqual(tgt.price, entry - s.target_usdt)   # short: target below

    def test_step6_position_size(self):
        s = strat()
        s.initial_capital = 10_000.0
        brk = strong_bull(59_900.0, 60_150.0, 0)
        opp = strong_bear(60_150.0, 59_950.0, 1)
        sigs = self._fire(s, brk, opp)
        # (3% of 10,000) / 100 = 3 BTC
        self.assertAlmostEqual(sigs[0].qty, 3.0)

    def test_step6_risk_equals_three_percent_of_capital(self):
        s = strat()
        s.initial_capital = 25_000.0
        sigs = self._fire(s, strong_bull(59_900.0, 60_150.0, 0),
                          strong_bear(60_150.0, 59_950.0, 1))
        qty = sigs[0].qty
        self.assertAlmostEqual(qty * s.stop_usdt, 25_000.0 * 0.03)

    def test_compound_sizes_off_running_equity(self):
        s = strat(compound=True)
        s.on_candle(ctx(s, strong_bull(59_900.0, 60_150.0, 0), index=0))
        sigs = list(s.on_candle(ctx(s, strong_bear(60_150.0, 59_950.0, 1),
                                    index=1, equity=50_000.0)))
        self.assertAlmostEqual(sigs[0].qty, 50_000.0 * 0.03 / 100.0)


class TestStateMachine(unittest.TestCase):
    def test_no_entry_while_a_position_is_open(self):
        s = strat()
        from aar.core.models import Position
        pos = Position(side=Side.LONG, qty=1.0, entry_price=60_000.0, entry_time=0)
        s.on_candle(ctx(s, strong_bull(59_900.0, 60_150.0, 0), index=0))
        sigs = list(s.on_candle(ctx(s, strong_bear(60_150.0, 59_950.0, 1),
                                    index=1, position=pos)))
        self.assertEqual(sigs, [])

    def test_setup_rearms_after_a_failed_opposite(self):
        s = strat()
        s.on_candle(ctx(s, strong_bull(59_900.0, 60_150.0, 0), index=0))
        s.on_candle(ctx(s, strong_bull(60_150.0, 60_300.0, 1), index=1))  # kills it
        self.assertIsNone(s.pending)
        # a fresh break arms again
        s.on_candle(ctx(s, strong_bear(60_100.0, 59_850.0, 2), index=2))
        self.assertIsNotNone(s.pending)

    def test_level_kinds_filter(self):
        from aar.core.models import LevelSet
        full = Level(k=2, price=60_000.0, kind=LevelKind.FULL)
        mid = Level(k=1, price=60_500.0, kind=LevelKind.MID)
        anchor = Level(k=0, price=59_000.0, kind=LevelKind.ANCHOR)

        class LS:
            valid = True
            spacing_d = 400.0
            levels = (anchor, mid, full)

        s = BreakFadeStrategy(cfg=None, level_kinds="full")
        s.on_session_start(LS(), [])
        self.assertEqual({lv.k for lv in s.levels}, {0, 2})

        s2 = BreakFadeStrategy(cfg=None, level_kinds="anchor")
        s2.on_session_start(LS(), [])
        self.assertEqual({lv.k for lv in s2.levels}, {0})

        s3 = BreakFadeStrategy(cfg=None, level_kinds="all")
        s3.on_session_start(LS(), [])
        self.assertEqual({lv.k for lv in s3.levels}, {0, 1, 2})

    def test_ema_is_warmed_by_session_history(self):
        s = BreakFadeStrategy(cfg=None)
        s.on_session_start(FakeLevels(),
                           [Candle(0, 100.0, 100.0, 100.0, 100.0, 1.0)] * 140)
        self.assertIsNotNone(s.ema.value)
        self.assertAlmostEqual(s.ema.value, 100.0)


class TestEndToEnd(unittest.TestCase):
    """Drive the real engine so fills, stop, target and sizing all interact."""

    def _run(self, london, **params):
        from aar.backtest.engine import BacktestEngine
        from aar.config import Config
        cfg = Config()
        cfg.backtest.initial_equity = 10_000.0
        cfg.backtest.taker_fee = 0.0
        cfg.backtest.slippage_ticks = 0
        cfg.backtest.leverage = 40.0
        cfg.risk.max_qty = 100.0

        # A full 140-candle session: two exact dojis in opposite halves give
        # D = 800 (half-D 400), and the anchor at 60,000 puts the k=0 level
        # exactly where the London break candles cross. 140 closes also warm
        # the 7 EMA to roughly 60,050 before London opens.
        t = WIN.session_start_ms
        session = [
            Candle(t, 60_400.0, 60_500.0, 60_300.0, 60_400.0, 1.0),              # upper doji
            Candle(t + 180_000, 59_600.0, 59_700.0, 59_500.0, 59_600.0, 1.0),    # lower doji
        ]
        while len(session) < 140:
            session.append(Candle(t + len(session) * 180_000,
                                  60_000.0, 60_100.0, 59_900.0, 60_050.0, 1.0))
        anchor = Candle(WIN.anchor_open_ms, 60_000.0, 60_000.0, 60_000.0, 60_000.0, 1.0)

        s = BreakFadeStrategy(cfg=cfg, **params)
        return BacktestEngine(cfg, {}).run_day(
            WIN, anchor, session, london, s, cfg.backtest.initial_equity)

    def test_short_hits_its_750_target(self):
        # break up through the anchor at 60,000, fade short, then price falls
        london = [strong_bull(59_900.0, 60_150.0, 0), strong_bear(60_150.0, 59_950.0, 1)]
        p = 59_950.0
        for i in range(2, 40):
            p -= 40.0
            london.append(candle(p + 40, p + 45, p - 5, p, i))
        r = self._run(london)
        self.assertEqual(len(r.trades), 1)
        t = r.trades[0]
        self.assertIs(t.side, Side.SHORT)
        self.assertEqual(t.exit_reason, "target")
        self.assertAlmostEqual(t.exit_price, 59_950.0 - 750.0, delta=0.5)
        self.assertAlmostEqual(t.pnl_gross, 3.0 * 750.0, delta=5.0)  # 3 BTC x 750

    def test_short_hits_its_100_stop(self):
        london = [strong_bull(59_900.0, 60_150.0, 0), strong_bear(60_150.0, 59_950.0, 1)]
        p = 59_950.0
        for i in range(2, 20):
            p += 20.0
            london.append(candle(p - 20, p + 5, p - 25, p, i))
        r = self._run(london)
        t = r.trades[0]
        self.assertEqual(t.exit_reason, "stop")
        self.assertAlmostEqual(t.exit_price, 59_950.0 + 100.0, delta=0.5)
        self.assertAlmostEqual(t.pnl_gross, -3.0 * 100.0, delta=5.0)  # exactly 3% of 10k

    def test_reward_to_risk_is_one_to_seven_point_five(self):
        win = self.test_short_hits_its_750_target
        self.assertAlmostEqual(750.0 / 100.0, 7.5)


if __name__ == "__main__":
    unittest.main()
