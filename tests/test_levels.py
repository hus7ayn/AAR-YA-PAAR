"""Golden cases for the level engine.

The no-doji and D==0 cases are regressions for the two failure modes found by
probing 14 real days of BTC/USDT before this code was written: the literal
`open == close` rule produced no usable grid on 9 of them.
"""

import unittest

from aar.core import models
from datetime import date

from aar.core.levels import LevelConfig, compute_levels
from aar.core.models import Candle, DojiMode, InvalidReason, LevelKind
from aar.core.sessions import window_for

models.set_interval_ms(900000)   # tests are written against 15m candles

WIN = window_for(date(2026, 8, 1))
CFG = LevelConfig()  # tick mode, opposite halves on, N=3


def session(specs):
    """specs: list of (open, high, low, close); filled out to 140 candles."""
    out = []
    t = WIN.session_start_ms
    for (o, h, l, c) in specs:
        out.append(Candle(t, o, h, l, c, 1.0))
        t += 180_000
    # Pad to a full session with candles that are neither dojis (body $50 = 500
    # ticks) nor capable of moving the extremes (range sits inside 59900-60600).
    while len(out) < 140:
        out.append(Candle(t, 60_200.0, 60_260.0, 60_150.0, 60_250.0, 1.0))
        t += 180_000
    return out


def anchor(price):
    return Candle(WIN.anchor_open_ms, price, price, price, price, 1.0)


class TestHappyPath(unittest.TestCase):
    def setUp(self):
        # Dojis are ranked by their CLOSE, so the upper doji's close must sit in
        # the top half of the session range (mid = 60,250 here) and the lower
        # doji's close in the bottom half.
        self.s = session([
            (60_550.0, 60_600.0, 60_500.0, 60_560.0),   # sets session high 60,600; not a doji
            (60_500.0, 60_550.0, 60_450.0, 60_500.0),   # upper doji, close 60,500
            (60_000.0, 60_050.0, 59_900.0, 60_000.0),   # lower doji, close 60,000, low 59,900
            (59_950.0, 60_000.0, 59_910.0, 59_980.0),   # not a doji
        ])
        self.ls = compute_levels(anchor(60_100.0), self.s, WIN, CFG)

    def test_valid(self):
        self.assertTrue(self.ls.valid, self.ls.reason)
        self.assertIsNone(self.ls.reason)

    def test_extremes_and_anchor(self):
        self.assertEqual(self.ls.session_high, 60_600.0)
        self.assertEqual(self.ls.session_low, 59_900.0)
        self.assertEqual(self.ls.anchor, 60_100.0)

    def test_spacing_is_absolute_difference_of_doji_closes(self):
        self.assertEqual(self.ls.upper_doji.close, 60_500.0)
        self.assertAlmostEqual(
            self.ls.spacing_d,
            abs(self.ls.upper_doji.close - self.ls.lower_doji.close),
        )
        self.assertGreater(self.ls.spacing_d, 0)

    def test_grid_shape_is_4N_plus_1(self):
        self.assertEqual(len(self.ls.levels), 4 * CFG.steps_per_side + 1)  # 13
        self.assertEqual(self.ls.levels[0].k, -6)
        self.assertEqual(self.ls.levels[-1].k, 6)

    def test_grid_geometry(self):
        d, a = self.ls.spacing_d, self.ls.anchor
        by_k = {lv.k: lv for lv in self.ls.levels}
        self.assertAlmostEqual(by_k[0].price, a)
        self.assertAlmostEqual(by_k[2].price, a + d)        # full step up
        self.assertAlmostEqual(by_k[-2].price, a - d)       # full step down
        self.assertAlmostEqual(by_k[1].price, a + d / 2)    # midpoint
        self.assertAlmostEqual(by_k[-1].price, a - d / 2)
        self.assertAlmostEqual(by_k[6].price, a + 3 * d)    # outermost

    def test_level_kinds(self):
        by_k = {lv.k: lv for lv in self.ls.levels}
        self.assertIs(by_k[0].kind, LevelKind.ANCHOR)
        self.assertIs(by_k[2].kind, LevelKind.FULL)
        self.assertIs(by_k[1].kind, LevelKind.MID)

    def test_grid_is_symmetric_about_anchor(self):
        by_k = {lv.k: lv.price for lv in self.ls.levels}
        for k in range(1, 7):
            self.assertAlmostEqual(by_k[k] - by_k[0], by_k[0] - by_k[-k])

    def test_validity_window_is_the_london_session(self):
        self.assertEqual(self.ls.valid_from_ms, WIN.session_end_ms)
        self.assertEqual(self.ls.valid_to_ms, WIN.london_end_ms)


class TestFailureModes(unittest.TestCase):
    def test_no_doji_at_all_is_invalid_not_a_crash(self):
        s = session([(60_000.0 + i, 60_100.0 + i, 59_900.0 + i, 60_050.0 + i)
                     for i in range(140)])
        ls = compute_levels(anchor(60_000.0), s, WIN,
                            LevelConfig(doji_mode=DojiMode.STRICT))
        self.assertFalse(ls.valid)
        self.assertIs(ls.reason, InvalidReason.NO_DOJI_CANDIDATES)
        self.assertEqual(ls.levels, ())

    def test_single_doji_cannot_win_both_slots(self):
        """The D==0 collapse. One doji, sitting in the top half only."""
        s = session([
            (61_000.0, 61_000.0, 60_990.0, 61_000.0),  # the only doji, near the high
            (60_500.0, 60_600.0, 59_000.0, 60_400.0),  # sets the low, not a doji
        ])
        ls = compute_levels(anchor(60_000.0), s, WIN,
                            LevelConfig(doji_mode=DojiMode.STRICT))
        self.assertFalse(ls.valid)
        self.assertIs(ls.reason, InvalidReason.NO_LOWER_DOJI)

    def test_without_the_guard_the_same_candle_collapses_d_to_zero(self):
        """Reproduces the literal spec's degenerate case, to prove the guard earns its place."""
        s = session([
            (61_000.0, 61_000.0, 60_990.0, 61_000.0),
            (60_500.0, 60_600.0, 59_000.0, 60_400.0),
        ])
        ls = compute_levels(
            anchor(60_000.0), s, WIN,
            LevelConfig(doji_mode=DojiMode.STRICT, require_opposite_halves=False),
        )
        self.assertFalse(ls.valid)
        self.assertIs(ls.reason, InvalidReason.SPACING_TOO_SMALL)
        self.assertEqual(ls.spacing_d, 0.0)

    def test_incomplete_session_is_rejected(self):
        from aar.core.sessions import expected_session_candles
        need = expected_session_candles()
        s = session([(60_000.0, 60_100.0, 59_900.0, 60_000.0)])[:need - 1]
        ls = compute_levels(anchor(60_000.0), s, WIN, CFG)
        self.assertFalse(ls.valid)
        self.assertIs(ls.reason, InvalidReason.INCOMPLETE_SESSION)
        self.assertEqual(ls.session_candle_count, need - 1)

    def test_missing_anchor_is_rejected(self):
        ls = compute_levels(None, session([]), WIN, CFG)
        self.assertFalse(ls.valid)
        self.assertIs(ls.reason, InvalidReason.MISSING_ANCHOR)

    def test_candles_outside_the_window_are_ignored(self):
        s = session([(60_000.0, 60_100.0, 59_900.0, 60_000.0)])
        s.append(Candle(WIN.session_end_ms, 99_999.0, 99_999.0, 99_999.0, 99_999.0, 1.0))
        ls = compute_levels(anchor(60_000.0), s, WIN, CFG)
        self.assertNotEqual(ls.session_high, 99_999.0)
        self.assertEqual(ls.session_candle_count, 140)


class TestTolerance(unittest.TestCase):
    def _one_tick_body(self):
        # body of exactly one tick ($0.10) — invisible to STRICT, caught by TICK
        return session([
            (60_500.0, 60_600.0, 60_400.0, 60_500.1),   # upper, top half
            (60_000.0, 60_050.0, 59_900.0, 59_999.9),   # lower, bottom half
        ])

    def test_strict_misses_a_one_tick_body(self):
        ls = compute_levels(anchor(60_100.0), self._one_tick_body(), WIN,
                            LevelConfig(doji_mode=DojiMode.STRICT))
        self.assertFalse(ls.valid)

    def test_tick_mode_catches_it(self):
        ls = compute_levels(anchor(60_100.0), self._one_tick_body(), WIN,
                            LevelConfig(doji_mode=DojiMode.TICK))
        self.assertTrue(ls.valid, ls.reason)
        self.assertEqual(ls.tol_ticks_used, 1)
        self.assertLessEqual(ls.upper_doji.body_ticks, 1)

    def test_adaptive_widens_until_a_pair_exists(self):
        s = session([
            (60_500.0, 60_600.0, 60_400.0, 60_500.5),   # 5-tick body
            (60_000.0, 60_050.0, 59_900.0, 59_999.5),   # 5-tick body
        ])
        ls = compute_levels(anchor(60_100.0), s, WIN,
                            LevelConfig(doji_mode=DojiMode.ADAPTIVE))
        self.assertTrue(ls.valid, ls.reason)
        self.assertEqual(ls.tol_ticks_used, 5)

    def test_adaptive_gives_up_within_its_ceiling(self):
        s = session([(60_000.0 + i, 60_500.0 + i, 59_500.0 + i, 60_400.0 + i)
                     for i in range(140)])
        ls = compute_levels(anchor(60_000.0), s, WIN,
                            LevelConfig(doji_mode=DojiMode.ADAPTIVE, adaptive_max_ticks=5))
        self.assertFalse(ls.valid)


class TestDeterminism(unittest.TestCase):
    def test_same_input_same_output_regardless_of_order(self):
        import json
        import random
        s = session([
            (60_500.0, 60_600.0, 60_400.0, 60_500.0),
            (60_000.0, 60_050.0, 59_900.0, 60_000.0),
        ])
        a = compute_levels(anchor(60_100.0), s, WIN, CFG)
        shuffled = s[:]
        random.Random(7).shuffle(shuffled)
        b = compute_levels(anchor(60_100.0), shuffled, WIN, CFG)
        self.assertEqual(
            json.dumps(a.to_json_obj(), sort_keys=True),
            json.dumps(b.to_json_obj(), sort_keys=True),
        )

    def test_ties_broken_deterministically_by_later_candle(self):
        s = session([
            (60_500.0, 60_600.0, 60_400.0, 60_500.0),   # tie on high
            (60_500.0, 60_600.0, 60_400.0, 60_500.0),   # same high, later
            (60_000.0, 60_050.0, 59_900.0, 60_000.0),
        ])
        ls = compute_levels(anchor(60_100.0), s, WIN, CFG)
        self.assertEqual(ls.upper_doji.open_time, WIN.session_start_ms + 180_000)


if __name__ == "__main__":
    unittest.main()
