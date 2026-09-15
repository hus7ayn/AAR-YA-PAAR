"""Regressions for the nine defects confirmed by the adversarial audit.

Each test fails against the pre-fix code. Named for the defect, not the fix, so
a future change that reintroduces one is obvious from the failure output.
"""

import unittest

from aar.core import models
from dataclasses import replace
from datetime import date

from aar.backtest.engine import BacktestEngine
from aar.backtest.report import compute_metrics
from aar.config import Config
from aar.core.levels import LevelConfig, compute_levels
from aar.core.models import (
    Action, Candle, DojiMode, InvalidReason, Position, Side, Signal,
)
from aar.core.rules import BaseStrategy
from aar.core.sessions import window_for

models.set_interval_ms(900000)   # tests are written against 15m candles

WIN = window_for(date(2026, 8, 1))
ANCHOR = Candle(WIN.anchor_open_ms, 60_000.0, 60_000.0, 60_000.0, 60_000.0, 1.0)


def pad(session, o=60_200.0, h=60_260.0, l=60_150.0, c=60_250.0, n=140):
    t = WIN.session_start_ms + len(session) * 180_000
    while len(session) < n:
        session.append(Candle(t, o, h, l, c, 1.0))
        t += 180_000
    return session


def sess(specs, **kw):
    out, t = [], WIN.session_start_ms
    for o, h, l, c in specs:
        out.append(Candle(t, o, h, l, c, 1.0))
        t += 180_000
    return pad(out, **kw)


# ---------------------------------------------------------------- levels.py

class TestDojiRankedByCloseNotWick(unittest.TestCase):
    """Ranking by high/low let a long-wicked candle win the slot while the level
    it actually contributes (its close) sat far from the extreme."""

    def test_long_wick_does_not_beat_a_closer_level(self):
        s = sess([
            # A: huge upper wick, high is the session high, but close is low
            (60_300.0, 60_800.0, 60_290.0, 60_300.0),
            # B: no wick, close sits much nearer the session high
            (60_700.0, 60_720.0, 60_680.0, 60_700.0),
            (60_000.0, 60_050.0, 59_900.0, 60_000.0),   # lower doji
        ])
        ls = compute_levels(ANCHOR, s, WIN, LevelConfig())
        self.assertTrue(ls.valid, ls.reason)
        # B (close 60,700) must win, not A (high 60,800 but close 60,300)
        self.assertEqual(ls.upper_doji.close, 60_700.0)


class TestOppositeHalvesActuallySeparates(unittest.TestCase):
    """The pools overlapped: a doji straddling `mid` satisfied both
    `high >= mid` and `low <= mid` and could take both slots, giving D = 0."""

    def test_single_straddling_doji_cannot_take_both_slots(self):
        # one doji whose wicks span mid; under the old wick-based pools it was
        # simultaneously the highest high and the lowest low
        s = sess([(60_250.0, 61_000.0, 59_000.0, 60_250.0)],
                 o=60_240.0, h=60_260.0, l=60_230.0, c=60_255.0)
        ls = compute_levels(ANCHOR, s, WIN, LevelConfig(doji_mode=DojiMode.STRICT))
        self.assertFalse(ls.valid)
        self.assertNotEqual(ls.spacing_d, 0.0 if ls.valid else None)
        self.assertIn(ls.reason, (InvalidReason.NO_DOJI_CANDIDATES,
                                  InvalidReason.NO_UPPER_DOJI,
                                  InvalidReason.NO_LOWER_DOJI))

    def test_a_valid_day_always_uses_two_distinct_candles(self):
        s = sess([
            (60_500.0, 60_550.0, 60_450.0, 60_500.0),
            (60_000.0, 60_050.0, 59_900.0, 60_000.0),
        ])
        ls = compute_levels(ANCHOR, s, WIN, LevelConfig())
        self.assertTrue(ls.valid, ls.reason)
        self.assertNotEqual(ls.upper_doji.open_time, ls.lower_doji.open_time)
        self.assertGreater(ls.spacing_d, 0.0)


class TestAdaptiveReportsAccurateFailure(unittest.TestCase):
    """ADAPTIVE assigned upper/lower only on the successful break, so an
    exhausted search reported `no_upper_doji` and dropped both DojiRefs even
    when a pair had been found whose spacing was merely too small."""

    def test_exhaustion_keeps_the_pair_and_names_spacing(self):
        # two dojis one tick apart: a pair always exists, spacing never grows
        s = sess([
            (60_250.1, 60_600.0, 60_250.0, 60_250.1),
            (60_250.0, 60_250.1, 59_900.0, 60_250.0),
        ], o=60_240.0, h=60_260.0, l=60_230.0, c=60_245.0)
        ls = compute_levels(ANCHOR, s, WIN, LevelConfig(
            doji_mode=DojiMode.ADAPTIVE, adaptive_max_ticks=5, min_spacing_ticks=50))
        self.assertFalse(ls.valid)
        self.assertIs(ls.reason, InvalidReason.SPACING_TOO_SMALL)
        self.assertIsNotNone(ls.upper_doji)
        self.assertIsNotNone(ls.lower_doji)


# ---------------------------------------------------------------- engine.py

def _cfg(**over):
    c = Config()
    c.backtest.initial_equity = 10_000.0
    c.backtest.taker_fee = 0.0
    c.backtest.slippage_ticks = 0
    c.backtest.leverage = 40.0
    c.risk.max_qty = 100.0
    for k, v in over.items():
        obj, _, attr = k.rpartition(".")
        t = c
        for p in obj.split("."):
            t = getattr(t, p)
        setattr(t, attr, v)
    return c


def _session():
    return sess([
        (60_500.0, 60_550.0, 60_450.0, 60_500.0),
        (60_000.0, 60_050.0, 59_900.0, 60_000.0),
    ])


class StopAndTarget(BaseStrategy):
    name = "t-stop-target"

    def __init__(self, stop, target, qty=3.0):
        super().__init__(None)
        self.stop, self.target, self.qty = stop, target, qty
        self.done = False

    def on_candle(self, ctx):
        if self.done:
            return ()
        self.done = True
        return [Signal(Action.OPEN_LONG, qty=self.qty, reason="t"),
                Signal(Action.SET_STOP, price=self.stop),
                Signal(Action.SET_TARGET, price=self.target)]


class TestStopBeatsLiquidation(unittest.TestCase):
    """Liquidation was tested before the stop. The liquidation price is always
    farther from entry than the stop, so a fast candle booked a full-margin
    wipe in place of a bounded loss — an ~12x overstatement."""

    def test_fast_candle_books_the_stop_not_a_liquidation(self):
        entry = 60_000.0
        london = [Candle(WIN.session_end_ms, entry, entry, entry, entry, 1.0),
                  # collapses far past both the stop and the liquidation price
                  Candle(WIN.session_end_ms + 180_000, entry, entry, 50_000.0, 51_000.0, 1.0)]
        cfg = _cfg()
        r = BacktestEngine(cfg, {}).run_day(
            WIN, ANCHOR, _session(), london,
            StopAndTarget(stop=entry - 100.0, target=entry + 750.0), 10_000.0)
        self.assertEqual(len(r.trades), 1)
        t = r.trades[0]
        self.assertEqual(t.exit_reason, "stop")
        self.assertAlmostEqual(t.exit_price, entry - 100.0, places=2)
        self.assertAlmostEqual(t.pnl_gross, -300.0, delta=1.0)   # not ~-3,800


class TestMarginAndBlownAreReported(unittest.TestCase):
    """`blown` and `margin_rejects` were recorded on DayResult and read by
    nothing, so silently-refused entries looked like an absence of signals."""

    def test_metrics_surface_the_counters(self):
        cfg = _cfg()
        # 3 BTC at 60k = 180k notional; at 40x that needs 4.5k equity
        london = [Candle(WIN.session_end_ms + i * 180_000,
                         60_000.0, 60_010.0, 59_990.0, 60_000.0, 1.0) for i in range(4)]
        eng = BacktestEngine(cfg, {})
        r = eng.run_day(WIN, ANCHOR, _session(), london,
                        StopAndTarget(59_900.0, 60_750.0, qty=3.0), 1_000.0)
        self.assertEqual(r.trades, [])
        self.assertGreaterEqual(r.margin_rejects, 1)

        res = eng.run([], [], lambda: StopAndTarget(0, 0))
        res.days.append(r)
        res.initial_equity = 1_000.0
        m = compute_metrics(res)
        self.assertGreaterEqual(m.margin_rejects, 1)


# ------------------------------------------------------------ break_fade.py

class TestNoDeadConfigKnob(unittest.TestCase):
    def test_require_level_side_is_gone(self):
        from aar.strategies.break_fade import BreakFadeStrategy
        s = BreakFadeStrategy(cfg=None)
        self.assertFalse(hasattr(s, "require_level_side"))


# ---------------------------------------------------------------- runner.py

class TestLiveTargetIsPlaced(unittest.TestCase):
    """SET_TARGET fell through the runner's if/elif chain, so live positions
    carried a stop and no take-profit — every 7.5R winner was lost."""

    def test_runner_places_a_take_profit_order(self):
        from aar.live.runner import LiveRunner

        cfg = _cfg()
        cfg.live.dry_run = False
        cfg.live.testnet = True
        cfg.strategy_name = "null"

        sent = []

        class FakeClient:
            class creds:
                present = True
            def new_order(self, **p):
                sent.append(p)
                return {"orderId": len(sent)}
            def position(self, symbol):
                return {"positionAmt": "3.0", "entryPrice": "60000"}
            def filters(self, symbol):
                return {}
            def cancel_all(self, symbol):
                return {}
            def mark_price(self, symbol):
                return 60_000.0
            def balance_usdt(self):
                return 10_000.0

        r = LiveRunner.__new__(LiveRunner)
        r.cfg = cfg
        from aar.live.broker import Broker
        r.broker = Broker(cfg, client=FakeClient())
        r.broker.client.creds = FakeClient.creds()
        r._pending_side = None
        r.public = None
        r.strategy = None

        # force the live path
        cfg.live.testnet = False
        import contextlib
        import io
        import os
        os.environ["AAR_ALLOW_LIVE"] = "1"
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                r._execute(Signal(Action.OPEN_SHORT, qty=3.0, reason="x"), 60_000.0)
                r._execute(Signal(Action.SET_STOP, price=60_100.0), 60_000.0)
                r._execute(Signal(Action.SET_TARGET, price=59_250.0), 60_000.0)
        finally:
            os.environ.pop("AAR_ALLOW_LIVE", None)

        types = [p["type"] for p in sent]
        self.assertIn("STOP_MARKET", types)
        # The target must reach the exchange by SOME route — it was previously
        # dropped entirely. With limit_target on it is a reduce-only LIMIT;
        # with it off, a TAKE_PROFIT_MARKET.
        tp = next((p for p in sent
                   if p["type"] in ("TAKE_PROFIT_MARKET", "LIMIT")
                   and p.get("reduceOnly") == "true" or p["type"] == "TAKE_PROFIT_MARKET"),
                  None)
        self.assertIsNotNone(tp, f"no target order was placed; sent={types}")
        price = float(tp.get("price") or tp["stopPrice"])
        self.assertEqual(price, 59_250.0)

    def test_target_route_follows_the_limit_target_setting(self):
        """Live must place the same order type the backtest priced."""
        from aar.live.broker import Broker
        sent = []

        class FakeClient:
            class creds:
                present = True
            def new_order(self, **p):
                sent.append(p)
                return {"orderId": 1}

        for limit_target, expected in ((True, "LIMIT"), (False, "TAKE_PROFIT_MARKET")):
            sent.clear()
            cfg = _cfg(**{"backtest.limit_target": limit_target})
            cfg.live.dry_run = False
            cfg.live.testnet = False
            b = Broker(cfg, client=FakeClient())
            b.client.creds = FakeClient.creds()
            import os
            os.environ["AAR_ALLOW_LIVE"] = "1"
            try:
                if limit_target:
                    b.limit_target(Side.LONG, 3.0, 59_250.0)
                else:
                    b.take_profit_market(Side.LONG, 59_250.0)
            finally:
                os.environ.pop("AAR_ALLOW_LIVE", None)
            self.assertEqual(sent[0]["type"], expected)
            self.assertEqual(sent[0].get("reduceOnly") or sent[0].get("closePosition"),
                             "true")


if __name__ == "__main__":
    unittest.main()


# ------------------------------------------------------- fee optimisation

class TestMakerFeesAndPostOnly(unittest.TestCase):
    """Fee-optimisation paths: maker target exits, BNB discount, and post-only
    entries with a conservative fill model."""

    def _run(self, london, **over):
        cfg = _cfg(**over)
        return BacktestEngine(cfg, {}).run_day(
            WIN, ANCHOR, _session(), london,
            StopAndTarget(stop=59_900.0, target=60_750.0), 10_000.0), cfg

    def _flat_then(self, moves):
        out = [Candle(WIN.session_end_ms, 60_000.0, 60_000.0, 60_000.0, 60_000.0, 1.0)]
        t = WIN.session_end_ms + 180_000
        for (o, h, l, c) in moves:
            out.append(Candle(t, o, h, l, c, 1.0))
            t += 180_000
        return out

    def test_target_exit_pays_maker_and_does_not_slip(self):
        rise = self._flat_then([(60_000.0, 60_800.0, 59_990.0, 60_760.0)])
        r, cfg = self._run(rise, **{"backtest.taker_fee": 0.0005,
                                    "backtest.maker_fee": 0.0002,
                                    "backtest.slippage_ticks": 1})
        t = r.trades[0]
        self.assertEqual(t.exit_reason, "target")
        self.assertAlmostEqual(t.exit_price, 60_750.0, places=4)  # exact, no slip
        entry_fee = 3.0 * t.entry_price * 0.0005
        exit_fee = 3.0 * 60_750.0 * 0.0002
        self.assertAlmostEqual(t.fees, entry_fee + exit_fee, delta=0.5)

    def test_stop_exit_still_pays_taker(self):
        drop = self._flat_then([(60_000.0, 60_010.0, 59_800.0, 59_850.0)])
        r, _ = self._run(drop, **{"backtest.taker_fee": 0.0005,
                                  "backtest.maker_fee": 0.0002})
        t = r.trades[0]
        self.assertEqual(t.exit_reason, "stop")
        self.assertAlmostEqual(t.fees, 3.0 * (t.entry_price + 59_900.0) * 0.0005, delta=0.5)

    def test_fee_discount_scales_every_rate(self):
        rise = self._flat_then([(60_000.0, 60_800.0, 59_990.0, 60_760.0)])
        full, _ = self._run(rise, **{"backtest.taker_fee": 0.0005})
        disc, _ = self._run(rise, **{"backtest.taker_fee": 0.0005,
                                     "backtest.fee_discount": 0.9})
        self.assertAlmostEqual(disc.trades[0].fees, full.trades[0].fees * 0.9, delta=0.01)

    def test_post_only_fills_only_when_price_comes_back(self):
        """A BUY limit must not fill on a candle that never trades down to it."""
        away = self._flat_then([
            (60_100.0, 60_200.0, 60_050.0, 60_150.0),   # never touches 60,000
            (60_200.0, 60_300.0, 60_150.0, 60_250.0),
            (60_300.0, 60_400.0, 60_250.0, 60_350.0),
        ])
        r, _ = self._run(away, **{"backtest.post_only_entry": True,
                                  "backtest.entry_ttl_candles": 2})
        self.assertEqual(r.trades, [])
        self.assertEqual(r.entries_unfilled, 1)

    def test_post_only_fills_at_exactly_the_posted_price(self):
        back = self._flat_then([
            (60_050.0, 60_100.0, 59_950.0, 60_060.0),   # trades down through 60,000
            (60_060.0, 60_900.0, 60_050.0, 60_800.0),
        ])
        r, _ = self._run(back, **{"backtest.post_only_entry": True,
                                  "backtest.slippage_ticks": 5})
        self.assertEqual(len(r.trades), 1)
        t = r.trades[0]
        self.assertAlmostEqual(t.entry_price, 60_000.0, places=4)  # no slippage
        self.assertAlmostEqual(3.0 * 60_000.0 * 0.0002,
                               t.fees - 3.0 * t.exit_price * 0.0002, delta=1.0)

    def test_post_only_carries_stop_and_target_onto_the_fill(self):
        back = self._flat_then([
            (60_050.0, 60_100.0, 59_950.0, 60_060.0),
            (60_060.0, 60_900.0, 60_050.0, 60_800.0),
        ])
        r, _ = self._run(back, **{"backtest.post_only_entry": True})
        self.assertEqual(r.trades[0].exit_reason, "target")
        self.assertAlmostEqual(r.trades[0].exit_price, 60_750.0, places=4)


# ------------------------------------------- 15m migration / timeframe split

class TestTimeframeIsConfigurable(unittest.TestCase):
    def test_session_size_follows_the_interval(self):
        from aar.core.sessions import expected_session_candles
        for name, expect in (("3m", 140), ("15m", 28), ("1h", 7)):
            models.set_interval_ms(models.interval_to_ms(name))
            self.assertEqual(expected_session_candles(), expect, name)
        models.set_interval_ms(models.interval_to_ms("15m"))

    def test_anchor_opens_one_interval_before_the_session(self):
        from aar.core.sessions import window_for as wf
        for name in ("3m", "15m", "1h"):
            ms = models.interval_to_ms(name)
            w = wf(date(2026, 8, 1), ms)
            self.assertEqual(w.anchor_open_ms, w.session_start_ms - ms, name)
            self.assertEqual(w.anchor_close_ms, w.session_start_ms, name)

    def test_grid_and_execution_can_use_different_timeframes(self):
        """window_for(interval) must not disturb the global setting."""
        from aar.core.sessions import window_for as wf
        before = models.INTERVAL_MS
        lw = wf(date(2026, 8, 1), models.interval_to_ms("3m"))
        tw = wf(date(2026, 8, 1), models.interval_to_ms("15m"))
        self.assertEqual(models.INTERVAL_MS, before)          # unchanged
        self.assertNotEqual(lw.anchor_open_ms, tw.anchor_open_ms)
        # wall-clock boundaries are identical at every timeframe
        self.assertEqual(lw.session_end_ms, tw.session_end_ms)
        self.assertEqual(lw.london_end_ms, tw.london_end_ms)


class TestSignalTimeIsRecorded(unittest.TestCase):
    """The auditor used to infer the setup candle from the fill price, which
    picks the wrong candle whenever another close happens to match."""

    def test_trade_records_the_signal_candle(self):
        cfg = _cfg(**{"backtest.post_only_entry": True})
        london = [Candle(WIN.session_end_ms, 60_000.0, 60_000.0, 60_000.0, 60_000.0, 1.0),
                  Candle(WIN.session_end_ms + 900_000, 60_000.0, 60_010.0, 59_950.0, 59_990.0, 1.0),
                  Candle(WIN.session_end_ms + 1_800_000, 59_990.0, 60_800.0, 59_980.0, 60_760.0, 1.0)]
        r = BacktestEngine(cfg, {}).run_day(
            WIN, ANCHOR, _session(), london,
            StopAndTarget(stop=59_900.0, target=60_750.0), 10_000.0)
        self.assertEqual(len(r.trades), 1)
        t = r.trades[0]
        # signal on the first candle, fill on a later one
        self.assertEqual(t.signal_time, london[0].open_time)
        self.assertGreater(t.entry_time, t.signal_time)


class TestColourOnlyPattern(unittest.TestCase):
    def test_wicky_candles_qualify_but_flat_ones_do_not(self):
        from aar.strategies.break_fade import qualifies
        wicky_green = Candle(0, 100.0, 200.0, 50.0, 101.0, 1.0)
        wicky_red = Candle(0, 101.0, 200.0, 50.0, 100.0, 1.0)
        flat = Candle(0, 100.0, 110.0, 90.0, 100.0, 1.0)
        self.assertTrue(qualifies(wicky_green, 0.10))
        self.assertTrue(qualifies(wicky_red, 0.10))
        self.assertFalse(qualifies(flat, 0.10))

    def test_shape_filter_still_available(self):
        from aar.strategies.break_fade import qualifies
        wicky = Candle(0, 100.0, 200.0, 50.0, 101.0, 1.0)
        marubozu = Candle(0, 100.0, 101.0, 100.0, 101.0, 1.0)
        self.assertFalse(qualifies(wicky, 0.10, max_wick_ticks=0))
        self.assertTrue(qualifies(marubozu, 0.10, max_wick_ticks=0))


class TestIsolatedMargin(unittest.TestCase):
    def test_broker_sets_margin_type_from_config(self):
        from aar.live.broker import Broker
        calls = []

        class FakeClient:
            class creds:
                present = True
            def set_margin_type(self, symbol, margin_type):
                calls.append(("margin", margin_type)); return {}
            def set_leverage(self, symbol, lev):
                calls.append(("lev", lev)); return {}

        cfg = _cfg(**{"backtest.leverage": 40.0})
        cfg.market.margin_type = "ISOLATED"
        b = Broker(cfg, client=FakeClient())
        b.client.creds = FakeClient.creds()
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()):
            b.apply_account_settings()
        self.assertIn(("margin", "ISOLATED"), calls)
        self.assertIn(("lev", 40), calls)
