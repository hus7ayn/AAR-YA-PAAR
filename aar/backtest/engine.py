"""Candle-replay backtest for USD-M perpetual futures.

Conventions, chosen to avoid flattering the strategy:

* Signals are produced from a **closed** candle and filled at that candle's
  close plus slippage — matching live behaviour, where the runner acts within
  seconds of the close.
* Stops and targets are checked **intrabar** against each subsequent candle's
  high/low. When a single candle contains both, the **stop is assumed to hit
  first**. That is the pessimistic ordering; 3m candle data cannot tell us which
  came first, so we take the worse branch rather than guess.
* Funding is charged at every 00:00/08:00/16:00 UTC stamp a position is held
  across. The 08:00 UTC stamp is 13:30 IST, inside the trading window.
* Liquidation is checked against the position's maintenance margin each candle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as Date

from ..config import Config
from ..core.levels import compute_levels
from ..core.models import Candle, Fill, LevelSet, Position, Side, Signal, Trade
from ..core.models import Action
from ..core.rules import Context, Strategy
from ..core.sessions import (SessionWindow, anchor_open_ms_for,
                             funding_times_between, window_for)

# Binance BTCUSDT tier-1 maintenance margin rate.
MAINT_MARGIN_RATE = 0.004


@dataclass(slots=True)
class DayResult:
    date: str
    levels: LevelSet
    trades: list[Trade] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    equity_start: float = 0.0
    equity_end: float = 0.0
    skipped: str | None = None  # invalid-day reason, if any
    blown: bool = False          # equity hit zero; further entries refused
    margin_rejects: int = 0      # entries refused for insufficient initial margin
    entries_unfilled: int = 0    # post-only limits that expired without filling
    size_rejects: int = 0        # entries refused: qty rounded to 0 or below min notional

    @property
    def pnl(self) -> float:
        return self.equity_end - self.equity_start


@dataclass(slots=True)
class PendingEntry:
    """A post-only limit order resting on the book, not yet filled."""
    side: Side
    qty: float
    limit: float
    index: int          # London index of the candle it was posted on
    signal_time: int    # open_time of the signal candle
    reason: str
    posted_ms: int = 0  # when the limit was posted (continuous engine)
    stop: float | None = None
    target: float | None = None


@dataclass(slots=True)
class BacktestResult:
    days: list[DayResult] = field(default_factory=list)
    initial_equity: float = 0.0
    final_equity: float = 0.0

    @property
    def trades(self) -> list[Trade]:
        return [t for d in self.days for t in d.trades]

    @property
    def traded_days(self) -> list[DayResult]:
        return [d for d in self.days if d.trades]

    @property
    def valid_days(self) -> list[DayResult]:
        return [d for d in self.days if d.levels.valid]


class BacktestEngine:
    def __init__(self, cfg: Config, funding: dict[int, float] | None = None):
        self.cfg = cfg
        self.funding = funding or {}

    # ------------------------------------------------------------- helpers

    def _slip(self, price: float, side: Side, entering: bool) -> float:
        """Slippage always works against us."""
        bt, m = self.cfg.backtest, self.cfg.market
        adverse_up = (side is Side.LONG) == entering
        delta = bt.slippage_ticks * m.tick_size + price * bt.slippage_bps / 10_000
        return price + delta if adverse_up else price - delta

    def _round_qty(self, qty: float) -> float:
        step = self.cfg.market.step_size
        return max(0.0, round(qty / step) * step)

    def _size(self, ctx_equity: float, price: float, stop: float | None) -> float:
        """Risk-based sizing when a stop is known, else leverage-capped notional."""
        r, bt, m = self.cfg.risk, self.cfg.backtest, self.cfg.market
        if stop is not None and abs(price - stop) > 0:
            risk_usdt = ctx_equity * (r.risk_per_trade_pct / 100.0)
            qty = risk_usdt / abs(price - stop)
        else:
            qty = (ctx_equity * bt.leverage) / price
        qty = min(qty, r.max_qty, (ctx_equity * bt.leverage) / price)
        qty = self._round_qty(qty)
        return qty if qty * price >= m.min_notional else 0.0

    def _fee(self, qty: float, price: float, maker: bool = False) -> float:
        bt = self.cfg.backtest
        rate = (bt.maker_fee if maker else bt.taker_fee) * bt.fee_discount
        return qty * price * rate

    def _liquidation_price(self, pos: Position) -> float:
        """Approximate isolated-margin liquidation for a single position."""
        margin = (pos.qty * pos.entry_price) / self.cfg.backtest.leverage
        move = (margin - pos.qty * pos.entry_price * MAINT_MARGIN_RATE) / pos.qty
        return pos.entry_price - move if pos.side is Side.LONG else pos.entry_price + move

    # ----------------------------------------------------------------- day

    def run_day(
        self,
        win: SessionWindow,
        anchor: Candle | None,
        session: list[Candle],
        london: list[Candle],
        strategy: Strategy,
        equity: float,
        warmup: list[Candle] | None = None,
    ) -> DayResult:
        levels = compute_levels(anchor, session, win, self.cfg.levels)
        res = DayResult(date=win.date_str, levels=levels,
                        equity_start=equity, equity_end=equity)

        if not levels.valid:
            res.skipped = levels.reason.value if levels.reason else "invalid"
            return res
        if not london:
            res.skipped = "no_london_candles"
            return res

        # The EMA lives on the EXECUTION timeframe, so warm it on those candles.
        # `session` here is the level-timeframe series used to build the grid.
        strategy.on_session_start(levels, warmup if warmup is not None else session)

        pos: Position | None = None
        entry_reason = ""
        signal_time = 0
        trades_today = 0
        prev: Candle | None = None
        funding_stamps = funding_times_between(win.session_end_ms, win.london_end_ms)

        def close_position(c: Candle, price: float, reason: str) -> None:
            nonlocal pos, equity
            assert pos is not None
            # A target exit is a resting LIMIT: it earns the maker rate and fills
            # at exactly its price. Stops and the London-close flatten are market
            # orders — taker, and they slip.
            maker = reason == "target" and self.cfg.backtest.limit_target
            fill_px = price if maker else self._slip(price, pos.side, entering=False)
            fee = self._fee(pos.qty, fill_px, maker=maker)
            gross = pos.unrealised(fill_px)
            equity += gross - fee - pos.funding_paid
            res.trades.append(Trade(
                date=win.date_str, side=pos.side, qty=pos.qty,
                entry_time=pos.entry_time, entry_price=pos.entry_price,
                exit_time=c.open_time, exit_price=fill_px,
                pnl_gross=gross, fees=pos.fees_paid + fee,
                funding=pos.funding_paid, exit_reason=reason,
                entry_reason=entry_reason, signal_time=signal_time,
            ))
            res.fills.append(Fill(c.open_time, pos.side, pos.qty, fill_px, fee, "exit", reason))
            pos = None

        pending_entry: PendingEntry | None = None

        for i, c in enumerate(london):
            # --- post-only entry: did our resting limit get hit? -------------
            # Conservative rule: a BUY limit fills only if price actually traded
            # DOWN to it, a SELL limit only if price traded UP to it. That is
            # what produces adverse selection — we get filled on the setups that
            # immediately go against us, and miss the ones that run away.
            if pending_entry is not None and pos is None:
                pe = pending_entry
                thru = (self.cfg.backtest.post_only_through_ticks
                        * self.cfg.market.tick_size)
                hit = ((pe.side is Side.LONG and c.low <= pe.limit - thru) or
                       (pe.side is Side.SHORT and c.high >= pe.limit + thru))
                if hit:
                    fee = self._fee(pe.qty, pe.limit, maker=True)
                    equity -= fee
                    pos = Position(side=pe.side, qty=pe.qty, entry_price=pe.limit,
                                   entry_time=c.open_time, stop=pe.stop,
                                   target=pe.target, fees_paid=fee)
                    entry_reason = pe.reason
                    signal_time = pe.signal_time
                    trades_today += 1
                    res.fills.append(Fill(c.open_time, pe.side, pe.qty, pe.limit,
                                          fee, "entry", pe.reason))
                    pending_entry = None
                elif i - pe.index >= self.cfg.backtest.entry_ttl_candles:
                    res.entries_unfilled += 1
                    pending_entry = None

            # --- funding on positions held across a stamp -------------------
            if pos is not None:
                for ts in funding_stamps:
                    if c.open_time <= ts < c.close_time:
                        rate = self.funding.get(ts, self.cfg.backtest.funding_rate)
                        amt = pos.qty * c.close * rate
                        pos.funding_paid += amt if pos.side is Side.LONG else -amt

            # --- stop / target, pessimistic ordering --------------------------
            # Checked BEFORE liquidation: the liquidation price is always
            # farther from entry than the stop, so on any continuous intrabar
            # path the stop is necessarily touched first. Testing liquidation
            # first would book a full-margin wipe in place of a bounded stop.
            if pos is not None:
                stop_hit = pos.stop is not None and (
                    (pos.side is Side.LONG and c.low <= pos.stop) or
                    (pos.side is Side.SHORT and c.high >= pos.stop))
                tgt_hit = pos.target is not None and (
                    (pos.side is Side.LONG and c.high >= pos.target) or
                    (pos.side is Side.SHORT and c.low <= pos.target))
                if stop_hit:
                    close_position(c, pos.stop, "stop")
                elif tgt_hit:
                    close_position(c, pos.target, "target")

            # --- liquidation, only on the path the stop did not already end ---
            if pos is not None:
                liq = self._liquidation_price(pos)
                hit = (pos.side is Side.LONG and c.low <= liq) or \
                      (pos.side is Side.SHORT and c.high >= liq)
                if hit:
                    close_position(c, liq, "liquidation")

            # --- strategy -----------------------------------------------------
            ctx = Context(candle=c, prev=prev, levels=levels, position=pos,
                          window=win, index=i, equity=equity)
            signals = list(strategy.on_candle(ctx))
            if i == len(london) - 1:
                signals.extend(strategy.on_session_end(ctx))

            for sig in signals:
                if sig.action is Action.CLOSE and pos is not None:
                    close_position(c, c.close, sig.reason or "signal")

                elif (sig.action in (Action.OPEN_LONG, Action.OPEN_SHORT)
                      and pos is None and pending_entry is None):
                    if trades_today >= self.cfg.risk.max_trades_per_day:
                        continue
                    # A blown account cannot keep trading. Without this the
                    # backtest happily runs equity deeply negative and reports
                    # losses no real account could have sustained.
                    if equity <= 0:
                        res.blown = True
                        continue
                    side = Side.LONG if sig.action is Action.OPEN_LONG else Side.SHORT
                    post_only = self.cfg.backtest.post_only_entry
                    # A post-only limit sets its own price, so it does not slip.
                    px = (sig.price or c.close) if post_only else \
                        self._slip(sig.price or c.close, side, entering=True)
                    qty = sig.qty if sig.qty else self._size(equity, px, None)
                    qty = self._round_qty(qty)
                    if qty <= 0 or qty * px < self.cfg.market.min_notional:
                        # Counted, not silent. When equity decays far enough the
                        # size rounds below the exchange step and every remaining
                        # signal disappears here — which truncates the sample
                        # endogenously. Un-counted, that looks like "no signals".
                        res.size_rejects += 1
                        continue
                    # Initial margin must be available at the configured leverage.
                    if qty * px > equity * self.cfg.backtest.leverage:
                        res.margin_rejects += 1
                        continue

                    if post_only:
                        pending_entry = PendingEntry(side=side, qty=qty, limit=px,
                                                     index=i, signal_time=c.open_time,
                                                     reason=sig.reason)
                    else:
                        fee = self._fee(qty, px)
                        equity -= fee
                        pos = Position(side=side, qty=qty, entry_price=px,
                                       entry_time=c.open_time, fees_paid=fee)
                        entry_reason = sig.reason
                        signal_time = c.open_time
                        trades_today += 1
                        res.fills.append(Fill(c.open_time, side, qty, px, fee,
                                              "entry", sig.reason))

                # Stop/target arrive in the same batch as the entry, so they must
                # attach to the resting order when the position does not exist yet.
                elif sig.action is Action.SET_STOP:
                    if pos is not None:
                        pos.stop = sig.price
                    elif pending_entry is not None:
                        pending_entry.stop = sig.price
                elif sig.action is Action.SET_TARGET:
                    if pos is not None:
                        pos.target = sig.price
                    elif pending_entry is not None:
                        pending_entry.target = sig.price

            prev = c

        # A post-only limit still resting when the window closes never fills.
        # Without this it vanished from the accounting entirely — neither a
        # trade nor an unfilled entry.
        if pending_entry is not None and pos is None:
            res.entries_unfilled += 1
            pending_entry = None

        if pos is not None:
            # The position is always booked, even when flattening is disabled.
            # Levels expire at 18:30 and there is no overnight carry, so simply
            # dropping `pos` would strand an already-debited entry fee and hide
            # the trade from the results entirely.
            reason = ("london_close" if self.cfg.risk.flatten_at_london_close
                      else "day_end_unflattened")
            close_position(london[-1], london[-1].close, reason)

        res.equity_end = equity
        return res

    # ----------------------------------------------------------------- run

    def run_held(self, candles: list[Candle], days: list[Date],
                 strategy_factory, level_candles: list[Candle] | None = None) -> BacktestResult:
        """Walk the whole candle stream once, carrying positions across days.

        Entries may only open inside a London window on a day with a valid grid,
        but once open a position is monitored on EVERY candle — overnight, over
        weekends — until its stop or target is hit. Nothing is cut at 18:30.
        """
        import bisect
        from ..core.models import interval_to_ms
        from ..core.sessions import anchor_open_ms_for, ist_date_of

        lvl = level_candles if level_candles is not None else candles
        lvl_ms = interval_to_ms(self.cfg.market.level_interval)
        exec_ms = interval_to_ms(self.cfg.market.interval)
        anc_ms = interval_to_ms(self.cfg.market.anchor_interval)

        ltimes = [c.open_time for c in lvl]
        lby = {c.open_time: c for c in lvl}
        eby = {c.open_time: c for c in candles}
        etimes = [c.open_time for c in candles]
        anchor_by = eby if anc_ms == exec_ms else lby

        # Precompute the frozen grid for each day.
        info = {}
        for d in days:
            lwin = window_for(d, lvl_ms)
            twin = window_for(d, exec_ms)
            awin = window_for(d, anc_ms)
            sess = lvl[bisect.bisect_left(ltimes, lwin.session_start_ms):
                       bisect.bisect_left(ltimes, lwin.session_end_ms)]
            ls = compute_levels(anchor_by.get(anchor_open_ms_for(awin, self.cfg.market.anchor_position)),
                                sess, lwin, self.cfg.levels)
            warm = candles[bisect.bisect_left(etimes, twin.session_start_ms):
                           bisect.bisect_left(etimes, twin.session_end_ms)]
            info[d] = (ls, twin, warm)
            res_day = DayResult(date=lwin.date_str, levels=ls)
            if not ls.valid:
                res_day.skipped = ls.reason.value if ls.reason else "invalid"

        out = BacktestResult(initial_equity=self.cfg.backtest.initial_equity)
        equity = self.cfg.backtest.initial_equity
        by_date = {}
        for d in days:
            ls, twin, _ = info[d]
            dr = DayResult(date=d.isoformat(), levels=ls, equity_start=equity, equity_end=equity)
            if not ls.valid:
                dr.skipped = ls.reason.value if ls.reason else "invalid"
            by_date[d.isoformat()] = dr
            out.days.append(dr)

        pos: Position | None = None
        pending: PendingEntry | None = None
        entry_reason = ""; signal_time = 0; owner = None
        strat = None; cur_day = None; idx = 0
        traded_on = set()
        funding_all = sorted(self.funding)

        def book(c, price, reason, maker=False):
            nonlocal pos, equity, owner
            fill = price if maker else self._slip(price, pos.side, entering=False)
            fee = self._fee(pos.qty, fill, maker=maker)
            gross = pos.unrealised(fill)
            equity += gross - fee - pos.funding_paid
            dr = by_date[owner]
            dr.trades.append(Trade(
                date=owner, side=pos.side, qty=pos.qty, entry_time=pos.entry_time,
                entry_price=pos.entry_price, exit_time=c.open_time, exit_price=fill,
                pnl_gross=gross, fees=pos.fees_paid + fee, funding=pos.funding_paid,
                exit_reason=reason, entry_reason=entry_reason, signal_time=signal_time))
            pos = None

        for c in candles:
            d = ist_date_of(c.open_time)
            entry = info.get(d)

            # --- new London window: reset the strategy for that day ----------
            if entry is not None and c.open_time == entry[1].session_end_ms:
                ls, twin, warm = entry
                cur_day = d; idx = 0
                strat = strategy_factory()
                if ls.valid:
                    strat.on_session_start(ls, warm)

            # --- resting limit ------------------------------------------------
            if pending is not None and pos is None:
                thru = self.cfg.backtest.post_only_through_ticks * self.cfg.market.tick_size
                hit = ((pending.side is Side.LONG and c.low <= pending.limit - thru) or
                       (pending.side is Side.SHORT and c.high >= pending.limit + thru))
                if hit:
                    fee = self._fee(pending.qty, pending.limit, maker=True)
                    equity -= fee
                    pos = Position(side=pending.side, qty=pending.qty, entry_price=pending.limit,
                                   entry_time=c.open_time, stop=pending.stop,
                                   target=pending.target, fees_paid=fee)
                    entry_reason = pending.reason; signal_time = pending.signal_time
                    by_date[owner].fills.append(Fill(c.open_time, pending.side, pending.qty,
                                                     pending.limit, fee, "entry", pending.reason))
                    pending = None
                elif c.open_time - pending.posted_ms >= self.cfg.backtest.entry_ttl_candles * exec_ms:
                    by_date[owner].entries_unfilled += 1
                    pending = None

            # --- an open position is managed on EVERY candle ------------------
            if pos is not None:
                for ts in funding_all:
                    if c.open_time <= ts < c.open_time + exec_ms:
                        rate = self.funding.get(ts, self.cfg.backtest.funding_rate)
                        amt = pos.qty * c.close * rate
                        pos.funding_paid += amt if pos.side is Side.LONG else -amt
                stop_hit = pos.stop is not None and (
                    (pos.side is Side.LONG and c.low <= pos.stop) or
                    (pos.side is Side.SHORT and c.high >= pos.stop))
                tgt_hit = pos.target is not None and (
                    (pos.side is Side.LONG and c.high >= pos.target) or
                    (pos.side is Side.SHORT and c.low <= pos.target))
                if stop_hit:
                    book(c, pos.stop, "stop")
                elif tgt_hit:
                    book(c, pos.target, "target", maker=self.cfg.backtest.limit_target)
                if pos is not None:
                    liq = self._liquidation_price(pos)
                    if ((pos.side is Side.LONG and c.low <= liq) or
                            (pos.side is Side.SHORT and c.high >= liq)):
                        book(c, liq, "liquidation")

            # --- drive the strategy on EVERY London candle ---------------------
            # It must see every close even while a position is open, or its EMA
            # (and any other stateful indicator) goes stale and the live and
            # backtest paths diverge. Entry signals are gated separately below.
            if (entry is not None and strat is not None and cur_day == d
                    and entry[0].valid and entry[1].in_london(c.open_time)):
                ls, twin, _ = entry
                can_open = (pos is None and pending is None
                            and d.isoformat() not in traded_on)
                ctx = Context(candle=c, prev=None, levels=ls, position=pos,
                              window=twin, index=idx, equity=equity)
                for sig in strat.on_candle(ctx):
                    if not can_open and sig.action in (Action.OPEN_LONG, Action.OPEN_SHORT):
                        continue
                    if sig.action in (Action.OPEN_LONG, Action.OPEN_SHORT):
                        if equity <= 0:
                            by_date[d.isoformat()].blown = True; continue
                        side = Side.LONG if sig.action is Action.OPEN_LONG else Side.SHORT
                        po = self.cfg.backtest.post_only_entry
                        px = (sig.price or c.close) if po else self._slip(sig.price or c.close, side, True)
                        qty = self._round_qty(sig.qty if sig.qty else self._size(equity, px, None))
                        if qty <= 0 or qty * px < self.cfg.market.min_notional:
                            by_date[d.isoformat()].size_rejects += 1; continue
                        if qty * px > equity * self.cfg.backtest.leverage:
                            by_date[d.isoformat()].margin_rejects += 1; continue
                        owner = d.isoformat(); traded_on.add(owner)
                        if po:
                            pending = PendingEntry(side=side, qty=qty, limit=px, index=idx,
                                                   signal_time=c.open_time, reason=sig.reason)
                            pending.posted_ms = c.open_time
                        else:
                            fee = self._fee(qty, px); equity -= fee
                            pos = Position(side=side, qty=qty, entry_price=px,
                                           entry_time=c.open_time, fees_paid=fee)
                            entry_reason = sig.reason; signal_time = c.open_time
                    elif sig.action is Action.SET_STOP:
                        if pos is not None: pos.stop = sig.price
                        elif pending is not None: pending.stop = sig.price
                    elif sig.action is Action.SET_TARGET:
                        if pos is not None: pos.target = sig.price
                        elif pending is not None: pending.target = sig.price
                idx += 1

        if pos is not None:
            book(candles[-1], candles[-1].close, "end_of_data")

        eq = self.cfg.backtest.initial_equity
        for dr in out.days:
            dr.equity_start = eq
            eq += sum(t.pnl_net for t in dr.trades)
            dr.equity_end = eq
        out.final_equity = eq
        return out

    def run(self, candles: list[Candle], days: list[Date],
            strategy_factory, level_candles: list[Candle] | None = None) -> BacktestResult:
        """`candles` drive execution; `level_candles` build the grid.

        They are usually different timeframes — the grid is a daily structure
        that needs small candles for the doji rule, while the setup trades on a
        larger one. Passing None uses the execution candles for both.
        """
        if not self.cfg.risk.flatten_at_london_close:
            return self.run_held(candles, days, strategy_factory, level_candles)

        # Candles are sorted, so slice each window by bisect rather than
        # rescanning the whole series per day (that would be O(days x candles)).
        import bisect

        from ..core.models import interval_to_ms

        lvl = level_candles if level_candles is not None else candles
        lvl_ms = interval_to_ms(self.cfg.market.level_interval)
        exec_ms = interval_to_ms(self.cfg.market.interval)
        anc_ms = interval_to_ms(self.cfg.market.anchor_interval)
        # The anchor comes from its own timeframe. Pick whichever loaded series
        # matches it; the two available are execution and level candles.
        anchor_by_time = ({c.open_time: c for c in candles} if anc_ms == exec_ms
                          else lby_time)

        times = [c.open_time for c in candles]
        ltimes = [c.open_time for c in lvl]
        lby_time = {c.open_time: c for c in lvl}

        def window_slice(lo: int, hi: int) -> list[Candle]:
            return candles[bisect.bisect_left(times, lo):bisect.bisect_left(times, hi)]

        def level_slice(lo: int, hi: int) -> list[Candle]:
            return lvl[bisect.bisect_left(ltimes, lo):bisect.bisect_left(ltimes, hi)]

        equity = self.cfg.backtest.initial_equity
        out = BacktestResult(initial_equity=equity)

        for d in days:
            lwin = window_for(d, lvl_ms)      # doji search: its own timeframe
            twin = window_for(d, exec_ms)     # execution: the trading timeframe
            awin = window_for(d, anc_ms)      # anchor: its own timeframe
            day = self.run_day(
                lwin,
                anchor_by_time.get(anchor_open_ms_for(awin, self.cfg.market.anchor_position)),
                level_slice(lwin.session_start_ms, lwin.session_end_ms),
                window_slice(twin.session_end_ms, twin.london_end_ms),
                strategy_factory(),
                equity,
                warmup=window_slice(twin.session_start_ms, twin.session_end_ms),
            )
            equity = day.equity_end
            out.days.append(day)

        out.final_equity = equity
        return out
