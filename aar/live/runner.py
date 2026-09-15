"""Daily scheduler.

Timeline for one IST trading day:

    05:30  observation window opens (anchor candle just closed at 05:30)
    12:30  fetch the 141 candles, freeze the level grid, persist it
    12:30-18:30  on each 3m close, drive the strategy and act on its signals
    18:30  flatten if configured, write the day's summary

The runner reuses `compute_levels` — the identical function the backtest calls —
so live and backtest cannot diverge in how levels are derived.
"""

from __future__ import annotations

import json
import time
import traceback
from datetime import date as Date, datetime, timedelta, timezone

from ..config import Config
from ..core.levels import compute_levels
from ..core import models
from ..core.models import Action, LevelSet, Position, Side
from ..core.rules import Context, get_strategy
from ..core.sessions import IST, to_ist_str, window_for
from ..data.rest import BinanceError, FuturesClient
from .broker import Broker, wait_until




class LiveRunner:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.public = FuturesClient(testnet=False)   # market data always from live
        self.broker = Broker(cfg)                    # execution honours the testnet flag
        self.strategy = get_strategy(cfg.strategy_name, cfg=cfg)
        self._pending_side: Side | None = None
        self._pending_qty: float | None = None

    # ------------------------------------------------------------- level calc

    def build_levels(self, d: Date) -> tuple[LevelSet, list]:
        win = window_for(d)
        candles = self.public.klines_range(
            self.cfg.market.symbol, self.cfg.market.interval, win.anchor_open_ms, win.session_end_ms
        )
        by_time = {c.open_time: c for c in candles}
        anchor = by_time.get(win.anchor_open_ms)
        session = [c for c in candles if win.in_session(c.open_time)]
        return compute_levels(anchor, session, win, self.cfg.levels), session

    def persist(self, ls: LevelSet) -> None:
        self.cfg.paths.ensure()
        p = self.cfg.paths.levels_dir / f"{ls.date}.json"
        p.write_text(json.dumps(ls.to_json_obj(), indent=2, sort_keys=True))
        print(f"  levels written: {p}")

    # ------------------------------------------------------------------- day

    def run_day(self, d: Date, wait: bool = True) -> int:
        win = window_for(d)
        cfg = self.cfg

        print(f"\n{'='*70}")
        print(f"  AAR-YA-PAAR  {d}  {cfg.market.symbol}")
        print(f"  mode      : {self.broker.describe_mode()}")
        print(f"  strategy  : {self.strategy.name}")
        print(f"  doji mode : {cfg.levels.doji_mode.value}")
        print(f"{'='*70}\n")

        try:
            self.broker.sync_filters()
        except BinanceError as e:
            print(f"  [warn] could not sync filters: {e}")
        self.broker.apply_account_settings()

        if wait:
            wait_until(win.session_end_ms, cfg.live.poll_seconds, "12:30 IST level freeze")

        ls, session = self.build_levels(d)
        self.persist(ls)

        if not ls.valid:
            print(f"  NO TRADING TODAY — {ls.reason.value}\n")
            return 0

        print(f"  anchor {ls.anchor:,.1f}   D {ls.spacing_d:,.1f}   "
              f"{len(ls.levels)} levels   tol {ls.tol_ticks_used} tick(s)")
        print("  levels: " + ", ".join(f"{lv.price:,.1f}" for lv in ls.levels) + "\n")

        self.strategy.on_session_start(ls, session)

        if self.strategy.name == "null":
            print("  strategy 'null' takes no trades — monitoring only.\n")

        # ------------------------------------------------------ candle loop
        position: Position | None = None
        prev = None
        index = 0
        step = models.INTERVAL_MS
        next_close = win.session_end_ms + step

        try:
            equity = self.broker.client.balance_usdt() if not cfg.live.dry_run else \
                cfg.backtest.initial_equity
        except BinanceError:
            equity = cfg.backtest.initial_equity

        # Act `entry_buffer_seconds` BEFORE the candle closes so a limit order is
        # already resting when the signal confirms, instead of chasing the market
        # after the close. At 15m a 12s buffer is 1.3% of the candle, so the
        # forming close is a close approximation of the final one.
        buffer_ms = max(0, cfg.backtest.entry_buffer_seconds) * 1000
        while next_close <= win.london_end_ms:
            if wait:
                wait_until(next_close - buffer_ms, cfg.live.poll_seconds)

            try:
                # The still-forming candle: its current close is our best estimate
                # of the final close, and it is what the limit price is based on.
                got = self.public.klines(cfg.market.symbol, cfg.market.interval,
                                         next_close - step, next_close - 1, 1)
            except BinanceError as e:
                print(f"  [warn] kline fetch failed: {e}")
                next_close += step
                continue

            if not got:
                next_close += step
                continue
            c = got[0]

            ctx = Context(candle=c, prev=prev, levels=ls, position=position,
                          window=win, index=index, equity=equity)
            signals = list(self.strategy.on_candle(ctx))
            if next_close + step > win.london_end_ms:
                signals.extend(self.strategy.on_session_end(ctx))

            for sig in signals:
                print(f"  {to_ist_str(c.open_time,'%H:%M')}  {sig.action.value:<11} "
                      f"{sig.reason}")
                self._execute(sig, c.close)

            if not cfg.live.dry_run:
                try:
                    raw = self.broker.client.position(cfg.market.symbol)
                    position = self._to_position(raw) if raw else None
                except BinanceError:
                    pass

            prev = c
            index += 1
            next_close += step
            if not wait:
                break

        if cfg.risk.flatten_at_london_close:
            print("\n  London close — flattening")
            print("  " + self.broker.flatten(prev.close if prev else 0.0).message)

        print(f"\n  day complete: {d}\n")
        return 0

    # -------------------------------------------------------------- helpers

    def _to_position(self, raw: dict) -> Position:
        amt = float(raw["positionAmt"])
        return Position(
            side=Side.LONG if amt > 0 else Side.SHORT,
            qty=abs(amt),
            entry_price=float(raw["entryPrice"]),
            entry_time=int(time.time() * 1000),
        )

    def _execute(self, sig, ref_price: float) -> None:
        cfg = self.cfg
        if sig.action in (Action.OPEN_LONG, Action.OPEN_SHORT):
            side = Side.LONG if sig.action is Action.OPEN_LONG else Side.SHORT
            qty = sig.qty or self._default_qty(ref_price)
            px = sig.price or ref_price
            # Mirror the backtest's execution assumption exactly. If the backtest
            # priced entries as maker, live must post them, or the two diverge.
            if cfg.backtest.post_only_entry:
                res = self.broker.post_only_limit(side, qty, px)
            else:
                res = self.broker.market_order(side, qty, px)
            self._pending_qty = qty
            print("    " + res.message)
            # Remember the side so the SET_STOP / SET_TARGET that arrive in the
            # same signal batch can be attached even before the exchange
            # reports the new position (and at all in dry run).
            self._pending_side = side if res.ok else None
        elif sig.action is Action.CLOSE:
            print("    " + self.broker.flatten(ref_price).message)
            self._pending_side = None
            self._pending_qty = None
        elif sig.action in (Action.SET_STOP, Action.SET_TARGET):
            side = self._position_side()
            if side is None:
                print("    [warn] no open position to attach "
                      f"{sig.action.value} to — order NOT placed")
                return
            if sig.action is Action.SET_STOP:
                # Always STOP_MARKET. A stop-limit would save 0.03% and risk not
                # filling in exactly the fast move it exists to protect against.
                print("    " + self.broker.stop_market(side, sig.price).message)
            elif cfg.backtest.limit_target:
                qty = self._pending_qty or self._default_qty(sig.price)
                print("    " + self.broker.limit_target(side, qty, sig.price).message)
            else:
                print("    " + self.broker.take_profit_market(side, sig.price).message)

    def _position_side(self) -> Side | None:
        """Side of the live position, or the pending side when dry running."""
        if self.cfg.live.dry_run and not self.broker.client.creds.present:
            return self._pending_side
        try:
            raw = self.broker.client.position(self.cfg.market.symbol)
        except BinanceError:
            return None
        if not raw:
            return None
        return Side.LONG if float(raw["positionAmt"]) > 0 else Side.SHORT

    def _default_qty(self, price: float) -> float:
        cfg = self.cfg
        notional = cfg.backtest.initial_equity * cfg.backtest.leverage
        return min(notional / price, cfg.risk.max_qty)

    # ----------------------------------------------------------------- loops

    def run_once(self) -> int:
        """Run today's session immediately, without waiting for the clock.

        Useful for validating the whole pipeline against the exchange at any
        hour: it fetches today's completed window and reports what it would do.
        """
        now_ist = datetime.now(timezone.utc).astimezone(IST)
        d = now_ist.date()
        win = window_for(d)
        if int(time.time() * 1000) < win.session_end_ms:
            d = d - timedelta(days=1)
            print(f"  before 12:30 IST — using previous session {d}")
        return self.run_day(d, wait=False)

    def run_forever(self) -> int:
        while True:
            try:
                now_ist = datetime.now(timezone.utc).astimezone(IST)
                d = now_ist.date()
                win = window_for(d)
                if int(time.time() * 1000) >= win.london_end_ms:
                    d += timedelta(days=1)
                    win = window_for(d)
                self.run_day(d, wait=True)
            except KeyboardInterrupt:
                self.broker.kill("interrupted by operator")
                return 130
            except Exception:
                traceback.print_exc()
                self.broker.kill("unhandled exception in runner")
                time.sleep(60)
