"""The seam where trading rules plug in.

The level engine deliberately knows nothing about entries and exits. A strategy
receives the frozen `LevelSet`, the current candle, and the open position (if
any), and returns zero or more `Signal`s. The backtest engine and the live
runner both drive strategies through this identical interface, so a rule set
validated in backtest runs unchanged against the exchange.

To add your own rules, subclass `Strategy`, implement `on_candle`, and register
it with `@register("my-name")`. Select it via `strategy.name` in config.toml.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol, Sequence, runtime_checkable

from .models import Candle, Level, LevelSet, Position, Signal
from .sessions import SessionWindow


@dataclass(frozen=True, slots=True)
class Context:
    """Everything a rule is allowed to see at one 3m candle boundary."""

    candle: Candle              # the just-CLOSED 3m candle
    prev: Candle | None         # the candle before it
    levels: LevelSet            # frozen at 12:30 IST
    position: Position | None
    window: SessionWindow
    index: int                  # 0-based index within the London trading window
    equity: float               # account equity in USDT

    # -- conveniences so rules stay declarative ---------------------------

    @property
    def price(self) -> float:
        return self.candle.close

    @property
    def spacing(self) -> float:
        """D — the full level spacing for the day. Useful for normalising risk."""
        return self.levels.spacing_d

    def nearest_level(self, price: float | None = None) -> Level | None:
        p = self.price if price is None else price
        if not self.levels.levels:
            return None
        return min(self.levels.levels, key=lambda lv: abs(lv.price - p))

    def levels_crossed(self) -> list[Level]:
        """Levels strictly inside the just-closed candle's low..high range."""
        c = self.candle
        return [lv for lv in self.levels.levels if c.low <= lv.price <= c.high]

    def crossed_up(self) -> list[Level]:
        """Levels this candle closed above having opened at or below."""
        c = self.candle
        return [lv for lv in self.levels.levels if c.open <= lv.price < c.close]

    def crossed_down(self) -> list[Level]:
        """Levels this candle closed below having opened at or above."""
        c = self.candle
        return [lv for lv in self.levels.levels if c.close < lv.price <= c.open]

    def level_above(self, price: float | None = None) -> Level | None:
        p = self.price if price is None else price
        above = [lv for lv in self.levels.levels if lv.price > p]
        return min(above, key=lambda lv: lv.price) if above else None

    def level_below(self, price: float | None = None) -> Level | None:
        p = self.price if price is None else price
        below = [lv for lv in self.levels.levels if lv.price < p]
        return max(below, key=lambda lv: lv.price) if below else None

    @property
    def is_last_candle(self) -> bool:
        return self.candle.close_time >= self.window.london_end_ms


@runtime_checkable
class Strategy(Protocol):
    """Implement this to define trading rules."""

    name: str

    def on_session_start(self, levels: LevelSet, history: Sequence[Candle]) -> None:
        """Called once at 12:30 IST when the grid is frozen.

        `history` is the 140 observation-session candles. Indicators that need
        warmup (moving averages and the like) should be primed here so they are
        already converged on the first London candle.
        """

    def on_candle(self, ctx: Context) -> Sequence[Signal]:
        """Called for each closed 3m candle in the London window."""
        ...

    def on_session_end(self, ctx: Context) -> Sequence[Signal]:
        """Called after the final candle. Return a CLOSE to flatten. Optional."""


class BaseStrategy:
    """Convenience base with no-op defaults for the optional hooks."""

    name = "base"

    def __init__(self, cfg=None, **params):
        self.cfg = cfg
        self.params = params

    def on_session_start(self, levels: LevelSet, history: Sequence[Candle] = ()) -> None:
        return None

    def on_candle(self, ctx: Context) -> Sequence[Signal]:
        return ()

    def on_session_end(self, ctx: Context) -> Sequence[Signal]:
        return ()


# ------------------------------------------------------------------ registry

_REGISTRY: dict[str, Callable[..., Strategy]] = {}


def register(name: str):
    def deco(cls):
        cls.name = name
        _REGISTRY[name] = cls
        return cls
    return deco


def get_strategy(name: str, **kwargs) -> Strategy:
    if name not in _REGISTRY:
        known = ", ".join(sorted(_REGISTRY)) or "(none)"
        raise KeyError(f"unknown strategy {name!r}; registered: {known}")
    return _REGISTRY[name](**kwargs)


def available() -> list[str]:
    return sorted(_REGISTRY)


@register("null")
class NullStrategy(BaseStrategy):
    """Emits no signals.

    This is the default. It exists so the whole pipeline — data, levels,
    scheduling, order plumbing, reporting — can be exercised end to end,
    including against the exchange testnet, before any real trading rule is
    written. Replace it by registering your own strategy and pointing
    `strategy.name` at it in config.toml.
    """

    def on_candle(self, ctx: Context) -> Sequence[Signal]:
        return ()
