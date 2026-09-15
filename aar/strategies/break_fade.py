"""Break-fade: the user's seven-step execution plan.

    Step 1  A "break candle" opens on one side of a key price level and closes
            on the other side.
    Step 2  Colour only: the break candle must be decisively red or green, and
            the next ("opposite") candle must be the opposite colour. No body
            ratio, no wick test — a flat candle (open == close) is the only
            thing rejected.
    Step 3  The break candle also crosses the 7 EMA in the same direction it
            crossed the level (opens below / closes above, or the mirror).
    Step 4  Enter at the close of the opposite candle, IN THE DIRECTION OF THE
            OPPOSITE CANDLE. A bullish break followed by a bearish opposite
            candle is therefore a SHORT — the break is being faded.
    Step 5  Stop 100 USDT and target 750 USDT, as distances in BTC price.
    Step 6  Position size = (3% of capital) / 100 USDT stop.
    Step 7  40x leverage (set in config; enforced by the engine's margin check).

Why "price distance" for step 5: with 10,000 USDT capital the step-6 formula
gives 3 BTC, and a 100 USDT adverse price move on 3 BTC loses exactly 300 USDT
— precisely the 3% being risked. The two steps only agree under that reading,
which is what fixes the units.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ..core.models import Action, Candle, Level, LevelKind, LevelSet, Signal
from ..core.rules import BaseStrategy, Context, register


class Ema:
    """Standard exponential moving average, seeded with an SMA of the first `period`."""

    __slots__ = ("period", "k", "value", "_seed")

    def __init__(self, period: int):
        self.period = period
        self.k = 2.0 / (period + 1.0)
        self.value: float | None = None
        self._seed: list[float] = []

    def update(self, price: float) -> float | None:
        if self.value is None:
            self._seed.append(price)
            if len(self._seed) >= self.period:
                self.value = sum(self._seed) / len(self._seed)
        else:
            self.value = price * self.k + self.value * (1.0 - self.k)
        return self.value


def body_ratio(c: Candle) -> float:
    """Body as a fraction of the full high-low range. Zero-range candles score 0."""
    rng = c.high - c.low
    if rng <= 0:
        return 0.0
    return abs(c.close - c.open) / rng


def wick_ticks(c: Candle, tick: float) -> tuple[int, int]:
    """(upper wick, lower wick) in whole ticks, measured from the body."""
    upper = c.high - max(c.open, c.close)
    lower = min(c.open, c.close) - c.low
    return round(upper / tick), round(lower / tick)


def qualifies(c: Candle, tick: float, max_wick_ticks: int | None = None,
              body_min: float = 0.0) -> bool:
    """Is this candle usable as a leg of the setup?

    The rule is COLOUR ONLY: the candle must be decisively red or green. A flat
    candle (open == close) has no colour and is rejected; everything else with a
    body qualifies. No body-to-range ratio and no wick test.

    The two shape filters remain available but are OFF by default:
      * `max_wick_ticks` — None disables it; an integer caps each wick in ticks
        (0 reproduces the previous 100%-body / 0-wick marubozu rule).
      * `body_min` — an optional body/range floor, 0.0 disables it.
    """
    if c.close == c.open:
        return False                      # no colour
    if max_wick_ticks is not None:
        if c.high <= c.low:
            return False
        upper, lower = wick_ticks(c, tick)
        if upper > max_wick_ticks or lower > max_wick_ticks:
            return False
    if body_min > 0.0 and body_ratio(c) < body_min:
        return False
    return True


# Backwards-compatible alias used by the independent auditor.
def is_full_body(c: Candle, tick: float, max_wick_ticks: int | None = 0,
                 body_min: float = 0.0) -> bool:
    return qualifies(c, tick, max_wick_ticks, body_min)


def direction(c: Candle) -> int:
    """+1 bullish, -1 bearish, 0 flat."""
    if c.close > c.open:
        return 1
    if c.close < c.open:
        return -1
    return 0


@dataclass(slots=True)
class PendingBreak:
    """A qualifying break candle, waiting for its opposite candle."""
    direction: int      # +1 bullish break, -1 bearish break
    level: Level
    index: int          # London-window index of the break candle
    ema: float


@register("break-fade")
class BreakFadeStrategy(BaseStrategy):
    name = "break-fade"

    def __init__(self, cfg=None, **params):
        super().__init__(cfg, **params)
        p = dict(getattr(cfg, "strategy_params", {}) or {})
        p.update(params)

        # max_wick_ticks is the rule. At 0 ticks the body necessarily IS the
        # whole range, so body_min_pct is redundant and defaults to off; it only
        # becomes a meaningful extra floor once wick tolerance is relaxed.
        self.body_min = float(p.get("body_min_pct", 0.0)) / 100.0
        # None = colour-only (no wick test). An integer re-enables the shape filter.
        mw = p.get("max_wick_ticks", None)
        self.max_wick_ticks = None if mw is None else int(mw)
        self.tick = float(
            getattr(getattr(cfg, "market", None), "tick_size", 0.10)
        ) if cfg else 0.10
        self.ema_period = int(p.get("ema_period", 7))
        self.stop_usdt = float(p.get("stop_usdt", 100.0))
        self.target_usdt = float(p.get("target_usdt", 750.0))
        self.risk_pct = float(p.get("risk_pct", 3.0))
        self.level_kinds = str(p.get("level_kinds", "all"))
        self.opposite_within = int(p.get("opposite_within", 1))
        # Slack, in ticks, on the level-cross test. 0 reproduces the exact rule.
        self.level_tol = int(p.get("level_cross_tol_ticks", 0)) * self.tick
        # How the 7 EMA must relate to the break candle:
        #   "cross"          open one side, close the other (strict).
        #   "cross_or_touch" also accept the EMA acting as SUPPORT/RESISTANCE —
        #                    the candle wicks into the EMA and closes away from it.
        #   "touch"          loosest: the EMA lies anywhere inside the candle's
        #                    high-low range, i.e. price interacted with it at all.
        #   "off"            drop step 3 entirely — no EMA condition.
        self.ema_mode = str(p.get("ema_mode", "cross"))
        # Ignore the first N candles of the London window. The 12:30-13:00 open
        # was the worst hour in BOTH halves of the sample (0% / 7.7% win rate).
        self.skip_first = int(p.get("skip_first_candles", 0))
        self.compound = bool(p.get("compound", False))

        self.initial_capital = float(
            getattr(getattr(cfg, "backtest", None), "initial_equity", 10_000.0)
        ) if cfg else 10_000.0

        self.ema = Ema(self.ema_period)
        self.pending: PendingBreak | None = None
        self.levels: list[Level] = []
        self.taken = 0

    # ------------------------------------------------------------- lifecycle

    def on_session_start(self, levels: LevelSet, history: Sequence[Candle] = ()) -> None:
        # Warm the EMA on the observation session so it is fully converged by
        # the first London candle rather than blind for its first 7 bars.
        self.ema = Ema(self.ema_period)
        for c in history:
            self.ema.update(c.close)

        self.pending = None
        self.taken = 0
        self.levels = self._eligible_levels(levels)

    def _eligible_levels(self, ls: LevelSet) -> list[Level]:
        if self.level_kinds == "full":
            return [lv for lv in ls.levels
                    if lv.kind in (LevelKind.FULL, LevelKind.ANCHOR)]
        if self.level_kinds == "anchor":
            return [lv for lv in ls.levels if lv.kind is LevelKind.ANCHOR]
        return list(ls.levels)

    # ---------------------------------------------------------------- checks

    def _broken_level(self, c: Candle, d: int) -> Level | None:
        """The level this candle opened one side of and closed the other side of.

        The tolerance applies to the OPEN side only. The close must genuinely
        finish past the level — that is what makes it a break. Opening a tick or
        two the "wrong" side is forgiven, because price frequently opens right on
        a level; closing short of it is not, because then nothing was broken.

        With `level_tol = 0` this is the exact original rule:
        `open < level <= close` for a bull break, mirrored for a bear one.
        """
        t = self.level_tol
        for lv in self.levels:
            if d > 0 and c.open < lv.price + t and c.close >= lv.price:
                return lv
            if d < 0 and c.open > lv.price - t and c.close <= lv.price:
                return lv
        return None

    def _ema_supports(self, c: Candle, d: int, ema: float) -> bool:
        """The EMA acted as support (bull) or resistance (bear) for this candle.

        Bull: the low pierced or touched the EMA and the candle closed above it —
        price went down to the average, found it, and pushed back up. Bear is the
        mirror. This catches the case where price is already on one side of the
        EMA and uses it as a floor, which a strict cross test rejects outright.
        """
        if d > 0:
            return c.low <= ema < c.close
        if d < 0:
            return c.high >= ema > c.close
        return False

    def _ema_ok(self, c: Candle, d: int, ema: float) -> bool:
        """Step 3, honouring `ema_mode`.

        "touch" is the loosest: the EMA anywhere within the candle's range counts
        as the level "taking support from" the average. It admits the case where
        price dips through the EMA and closes back on the same side — neither a
        cross nor a clean bounce, but a genuine interaction with it.
        """
        if self.ema_mode == "off":
            return True
        if self._crosses_ema(c, d, ema):
            return True
        if self.ema_mode in ("cross_or_touch", "touch"):
            if self._ema_supports(c, d, ema):
                return True
        if self.ema_mode == "touch":
            return c.low <= ema <= c.high
        return False

    def _crosses_ema(self, c: Candle, d: int, ema: float) -> bool:
        """Step 3 (strict) — opens below / closes above the 7 EMA, or the mirror."""
        if d > 0:
            return c.open < ema <= c.close
        if d < 0:
            return c.close <= ema < c.open
        return False

    # ----------------------------------------------------------------- entry

    def _size(self, equity: float) -> float:
        capital = equity if self.compound else self.initial_capital
        return (capital * self.risk_pct / 100.0) / self.stop_usdt

    def _entry_signals(self, ctx: Context, d: int, brk: PendingBreak) -> list[Signal]:
        entry = ctx.candle.close
        qty = self._size(ctx.equity)
        if d > 0:
            stop, target = entry - self.stop_usdt, entry + self.target_usdt
            action = Action.OPEN_LONG
        else:
            stop, target = entry + self.stop_usdt, entry - self.target_usdt
            action = Action.OPEN_SHORT

        why = (f"fade {'bull' if brk.direction > 0 else 'bear'} break of "
               f"k={brk.level.k}@{brk.level.price:,.1f}")
        return [
            Signal(action, qty=qty, reason=why),
            Signal(Action.SET_STOP, price=stop, reason="stop 100"),
            Signal(Action.SET_TARGET, price=target, reason="target 750"),
        ]

    # ---------------------------------------------------------------- candle

    def on_candle(self, ctx: Context) -> Sequence[Signal]:
        c = ctx.candle
        ema = self.ema.update(c.close)
        if not self.levels:
            return ()
        if ema is None and self.ema_mode != "off":
            return ()   # indicator not warm yet and step 3 needs it

        d = direction(c)
        strong = qualifies(c, self.tick, self.max_wick_ticks, self.body_min)
        out: list[Signal] = []

        # The EMA has already been updated above, so state stays correct even on
        # candles we decline to trade.
        if ctx.index < self.skip_first:
            return ()

        # --- Step 4: is this the opposite candle for an armed break? --------
        if self.pending is not None:
            age = ctx.index - self.pending.index
            if age > self.opposite_within:
                self.pending = None                      # setup expired
            elif age >= 1:
                is_opposite = d != 0 and d == -self.pending.direction
                if is_opposite and strong:
                    if ctx.position is None:
                        out = self._entry_signals(ctx, d, self.pending)
                        self.taken += 1
                    self.pending = None
                    return out                            # never re-arm on an entry bar
                if age >= self.opposite_within:
                    self.pending = None                   # wrong shape, drop it

        # --- Steps 1-3: does this candle arm a new break? -------------------
        if ctx.position is None and d != 0 and strong:
            lv = self._broken_level(c, d)
            if lv is not None and self._ema_ok(c, d, ema):
                self.pending = PendingBreak(direction=d, level=lv,
                                            index=ctx.index, ema=ema)

        return out
