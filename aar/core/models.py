"""Core value types.

Prices are floats parsed from Binance's decimal strings. All price comparisons
that need to be exact go through integer tick counts (see `to_ticks`) rather
than float equality — the exchange quantises every price to `tickSize`, so an
integer tick index is the only lossless representation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

# Candle interval, in milliseconds. Single-timeframe system: this is set once
# from config at startup via `set_interval_ms` and then read everywhere.
INTERVAL_MS = 900_000  # 15 minutes

INTERVAL_NAMES = {
    60_000: "1m", 180_000: "3m", 300_000: "5m", 900_000: "15m",
    1_800_000: "30m", 3_600_000: "1h", 14_400_000: "4h",
}


def interval_to_ms(name: str) -> int:
    for ms, n in INTERVAL_NAMES.items():
        if n == name:
            return ms
    raise ValueError(f"unsupported interval {name!r}; known: {sorted(INTERVAL_NAMES.values())}")


def set_interval_ms(ms: int) -> None:
    """Set the global candle interval. Called once by config.load()."""
    global INTERVAL_MS
    INTERVAL_MS = ms


@dataclass(frozen=True, slots=True)
class Candle:
    open_time: int  # ms since epoch, UTC, inclusive
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def close_time(self) -> int:
        """Exclusive end of the candle."""
        return self.open_time + INTERVAL_MS

    @property
    def body(self) -> float:
        return abs(self.open - self.close)


def to_ticks(price: float, tick_size: float) -> int:
    """Exact tick index for a price. Binance quantises to tickSize, so this is lossless."""
    return round(price / tick_size)


class DojiMode(str, Enum):
    STRICT = "strict"      # open == close exactly (0 ticks)
    TICK = "tick"          # within 1 tick
    ABS = "abs"            # within config's doji_tol_ticks
    ADAPTIVE = "adaptive"  # widen from 0 until a valid opposite-half pair exists


class InvalidReason(str, Enum):
    MISSING_ANCHOR = "missing_anchor"
    INCOMPLETE_SESSION = "incomplete_session"
    NO_DOJI_CANDIDATES = "no_doji_candidates"  # none in the whole session
    NO_UPPER_DOJI = "no_upper_doji"            # candidates exist, none in the top half
    NO_LOWER_DOJI = "no_lower_doji"            # candidates exist, none in the bottom half
    SPACING_TOO_SMALL = "spacing_too_small"


class LevelKind(str, Enum):
    ANCHOR = "anchor"
    FULL = "full"
    MID = "mid"


@dataclass(frozen=True, slots=True)
class DojiRef:
    """A session candle selected as one of the two reference price levels."""
    open_time: int
    open: float
    high: float
    low: float
    close: float
    body_ticks: int


@dataclass(frozen=True, slots=True)
class Level:
    k: int  # level index; price = anchor + k * (D / 2)
    price: float
    kind: LevelKind


@dataclass(frozen=True, slots=True)
class LevelSet:
    """The frozen output of the level engine for one trading day."""
    date: str  # IST calendar date, YYYY-MM-DD
    valid: bool
    reason: InvalidReason | None
    anchor: float
    session_high: float
    session_low: float
    mid: float
    upper_doji: DojiRef | None
    lower_doji: DojiRef | None
    spacing_d: float
    tol_ticks_used: int
    session_candle_count: int
    levels: tuple[Level, ...] = ()
    # UTC ms bounds of the London trading window these levels are valid for
    valid_from_ms: int = 0
    valid_to_ms: int = 0

    def prices(self) -> list[float]:
        return [lv.price for lv in self.levels]

    def to_json_obj(self) -> dict:
        def doji(d: DojiRef | None) -> dict | None:
            if d is None:
                return None
            return {
                "open_time": d.open_time, "open": d.open, "high": d.high,
                "low": d.low, "close": d.close, "body_ticks": d.body_ticks,
            }

        return {
            "date": self.date,
            "valid": self.valid,
            "reason": self.reason.value if self.reason else None,
            "anchor": self.anchor,
            "session_high": self.session_high,
            "session_low": self.session_low,
            "mid": self.mid,
            "upper_doji": doji(self.upper_doji),
            "lower_doji": doji(self.lower_doji),
            "spacing_d": self.spacing_d,
            "tol_ticks_used": self.tol_ticks_used,
            "session_candle_count": self.session_candle_count,
            "valid_from_ms": self.valid_from_ms,
            "valid_to_ms": self.valid_to_ms,
            "levels": [{"k": lv.k, "price": lv.price, "kind": lv.kind.value} for lv in self.levels],
        }


# ---------------------------------------------------------------- trading side

class Side(str, Enum):
    LONG = "long"
    SHORT = "short"


class Action(str, Enum):
    OPEN_LONG = "open_long"
    OPEN_SHORT = "open_short"
    CLOSE = "close"
    SET_STOP = "set_stop"
    SET_TARGET = "set_target"


@dataclass(frozen=True, slots=True)
class Signal:
    action: Action
    qty: float | None = None      # base units (BTC); None = use config default sizing
    price: float | None = None    # for SET_STOP / SET_TARGET, or a limit price
    reason: str = ""


@dataclass(slots=True)
class Position:
    side: Side
    qty: float
    entry_price: float
    entry_time: int
    stop: float | None = None
    target: float | None = None
    fees_paid: float = 0.0
    funding_paid: float = 0.0

    def unrealised(self, mark: float) -> float:
        d = mark - self.entry_price
        return d * self.qty if self.side is Side.LONG else -d * self.qty


@dataclass(slots=True)
class Fill:
    time: int
    side: Side
    qty: float
    price: float
    fee: float
    kind: str  # "entry" | "exit"
    reason: str = ""


@dataclass(slots=True)
class Trade:
    date: str
    side: Side
    qty: float
    entry_time: int
    entry_price: float
    exit_time: int
    exit_price: float
    pnl_gross: float
    fees: float
    funding: float
    exit_reason: str
    entry_reason: str = ""
    # open_time of the candle that GENERATED the signal. With a post-only entry
    # the fill lands on a later candle, so this is the only reliable way to point
    # back at the setup rather than inferring it from the fill price.
    signal_time: int = 0

    @property
    def pnl_net(self) -> float:
        return self.pnl_gross - self.fees - self.funding
