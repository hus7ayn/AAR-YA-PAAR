"""The level engine.

Pure: candles in, `LevelSet` out. No network, no clock, no config file reads.
Backtest and live execution both call this exact function, which is what
guarantees they cannot drift apart.

The five steps from the strategy spec:

  1. Anchor A = close of the 05:27-05:30 IST candle.
  2. Session high/low over 05:30-12:30 IST; find one doji near each extreme.
  3. D = |upper doji close - lower doji close|, always positive.
  4. Grid = A + k * (D/2), both directions, even k = full steps, odd k = midpoints.
  5. Grid is frozen at 12:30 IST and valid through the London session to 18:30 IST.

Two guards were added because the literal rule is undefined on most real days
(see STRATEGY.md "Why the doji rule needs a tolerance"):

  * a tick-denominated tolerance, since exact open==close occurred on only 9 of
    14 sampled days; and
  * an opposite-halves constraint, because without it a single candle can be
    nearest to BOTH extremes, collapsing D to zero and flattening the whole grid
    onto the anchor. That happened on 4 of those 14 days.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .models import (
    Candle,
    DojiMode,
    DojiRef,
    InvalidReason,
    Level,
    LevelKind,
    LevelSet,
    to_ticks,
)
from .sessions import SessionWindow, expected_session_candles


@dataclass(frozen=True, slots=True)
class LevelConfig:
    tick_size: float = 0.10
    doji_mode: DojiMode = DojiMode.TICK
    doji_tol_ticks: int = 1        # used when doji_mode is ABS
    adaptive_max_ticks: int = 50   # ceiling for ADAPTIVE widening
    steps_per_side: int = 3        # N full-D steps each way -> 4N+1 levels
    min_spacing_ticks: int = 1     # D below this means the grid is degenerate
    require_opposite_halves: bool = True
    include_midpoints: bool = True   # False -> full-D levels only, no half-D lines
    min_session_candles: int = 0   # 0 = derive from the configured interval


def _tol_ticks_for(mode: DojiMode, cfg: LevelConfig) -> int:
    if mode is DojiMode.STRICT:
        return 0
    if mode is DojiMode.TICK:
        return 1
    if mode is DojiMode.ABS:
        return max(0, cfg.doji_tol_ticks)
    raise ValueError(f"{mode} has no fixed tolerance")


def _as_ref(c: Candle, tick: float) -> DojiRef:
    return DojiRef(
        open_time=c.open_time, open=c.open, high=c.high,
        low=c.low, close=c.close,
        body_ticks=abs(to_ticks(c.open, tick) - to_ticks(c.close, tick)),
    )


def _select_pair(
    session: Sequence[Candle], mid: float, tol_ticks: int, cfg: LevelConfig
) -> tuple[DojiRef | None, DojiRef | None, int]:
    """Pick the doji nearest the session high and the one nearest the session low.

    Ranking is on the doji's **close**, not its wick. A doji's open equals its
    close, so its close *is* the price level it marks — the spec asks for two
    price levels near the extremes, and it is those levels that must be near.
    Ranking by `high`/`low` instead would let a candle with a long wick win the
    slot while the level it actually contributes sits far from the extreme.

    With `require_opposite_halves`, the upper candidate's close must sit in the
    top half of the session range and the lower's in the bottom half, and the
    two winners must be **distinct candles**. Both parts are needed: a single
    candle sitting exactly at `mid` would otherwise qualify for both pools and
    collapse D to zero.

    Ties are broken by the later candle (`open_time`), so the result is
    deterministic regardless of input ordering.

    Returns (upper, lower, candidate_count). The count lets the caller tell
    "no dojis existed at all" apart from "dojis existed but all sat in one
    half" — two very different reasons for a day to be unusable.
    """
    tick = cfg.tick_size
    candidates = [
        c for c in session
        if abs(to_ticks(c.open, tick) - to_ticks(c.close, tick)) <= tol_ticks
    ]
    if not candidates:
        return None, None, 0

    if cfg.require_opposite_halves:
        upper_pool = [c for c in candidates if c.close >= mid]
        lower_pool = [c for c in candidates if c.close <= mid]
    else:
        upper_pool = lower_pool = candidates

    upper = max(upper_pool, key=lambda c: (c.close, c.open_time)) if upper_pool else None
    lower = min(lower_pool, key=lambda c: (c.close, -c.open_time)) if lower_pool else None

    # A candle whose close sits exactly on `mid` is in both pools and could take
    # both slots, which would make D zero. Reject rather than emit a flat grid.
    if cfg.require_opposite_halves and upper is not None and lower is not None:
        if upper.open_time == lower.open_time:
            return None, None, len(candidates)

    return (
        _as_ref(upper, tick) if upper else None,
        _as_ref(lower, tick) if lower else None,
        len(candidates),
    )


def _build_grid(anchor: float, d: float, cfg: LevelConfig) -> tuple[Level, ...]:
    """The level grid around the anchor.

    With midpoints:    anchor + k*(D/2) for k in [-2N, 2N] -> 4N+1 levels,
                       even k full-D, odd k midpoints.
    Without midpoints: only the even k survive, so the grid is anchor + k*D
                       for k in [-N, N] -> 2N+1 levels. `k` keeps the same
                       meaning either way (k=2 is one full D above the anchor),
                       so saved grids stay comparable across the setting.
    """
    half = d / 2.0
    out: list[Level] = []
    for k in range(-2 * cfg.steps_per_side, 2 * cfg.steps_per_side + 1):
        if k == 0:
            kind = LevelKind.ANCHOR
        elif k % 2 == 0:
            kind = LevelKind.FULL
        else:
            if not cfg.include_midpoints:
                continue
            kind = LevelKind.MID
        out.append(Level(k=k, price=anchor + k * half, kind=kind))
    return tuple(out)


def _invalid(
    win: SessionWindow, reason: InvalidReason, *, anchor: float = 0.0,
    hi: float = 0.0, lo: float = 0.0, tol: int = 0, n: int = 0,
    upper: DojiRef | None = None, lower: DojiRef | None = None, d: float = 0.0,
) -> LevelSet:
    return LevelSet(
        date=win.date_str, valid=False, reason=reason, anchor=anchor,
        session_high=hi, session_low=lo, mid=(hi + lo) / 2 if (hi or lo) else 0.0,
        upper_doji=upper, lower_doji=lower, spacing_d=d, tol_ticks_used=tol,
        session_candle_count=n, levels=(),
        valid_from_ms=win.session_end_ms, valid_to_ms=win.london_end_ms,
    )


def compute_levels(
    anchor_candle: Candle | None,
    session_candles: Sequence[Candle],
    win: SessionWindow,
    cfg: LevelConfig | None = None,
) -> LevelSet:
    """Compute the frozen level grid for one trading day.

    `anchor_candle` is the 05:27-05:30 IST candle — the one *before* the
    observation window opens. `session_candles` are the 05:30-12:30 IST candles.
    """
    cfg = cfg or LevelConfig()

    if anchor_candle is None:
        return _invalid(win, InvalidReason.MISSING_ANCHOR)

    session = sorted(
        (c for c in session_candles if win.in_session(c.open_time)),
        key=lambda c: c.open_time,
    )
    n = len(session)
    anchor = anchor_candle.close

    required = cfg.min_session_candles or expected_session_candles(win.interval_ms or None)
    if n < required:
        return _invalid(win, InvalidReason.INCOMPLETE_SESSION, anchor=anchor, n=n)

    hi = max(c.high for c in session)
    lo = min(c.low for c in session)
    mid = (hi + lo) / 2.0

    # --- pick the two reference dojis -------------------------------------
    if cfg.doji_mode is DojiMode.ADAPTIVE:
        upper = lower = None
        n_cand = 0
        tol = 0
        # If widening never finds a usable pair, keep the best partial result so
        # the failure is reported accurately (e.g. "spacing too small" with both
        # dojis attached) rather than collapsing to a misleading "no upper doji".
        for t in range(0, cfg.adaptive_max_ticks + 1):
            u, l, nc = _select_pair(session, mid, t, cfg)
            tol, n_cand = t, nc
            if u and l:
                upper, lower = u, l
                if abs(u.close - l.close) >= cfg.min_spacing_ticks * cfg.tick_size:
                    break
            elif upper is None and lower is None:
                upper, lower = u, l   # carry whichever side exists so far
    else:
        tol = _tol_ticks_for(cfg.doji_mode, cfg)
        upper, lower, n_cand = _select_pair(session, mid, tol, cfg)

    common = dict(anchor=anchor, hi=hi, lo=lo, tol=tol, n=n)
    if upper is None and lower is None and n_cand == 0:
        return _invalid(win, InvalidReason.NO_DOJI_CANDIDATES, **common)
    if upper is None:
        return _invalid(win, InvalidReason.NO_UPPER_DOJI, lower=lower, **common)
    if lower is None:
        return _invalid(win, InvalidReason.NO_LOWER_DOJI, upper=upper, **common)

    # --- spacing and grid --------------------------------------------------
    d = abs(upper.close - lower.close)  # spec: sign discarded, always positive
    if d < cfg.min_spacing_ticks * cfg.tick_size:
        return _invalid(
            win, InvalidReason.SPACING_TOO_SMALL, upper=upper, lower=lower, d=d, **common
        )

    return LevelSet(
        date=win.date_str, valid=True, reason=None, anchor=anchor,
        session_high=hi, session_low=lo, mid=mid,
        upper_doji=upper, lower_doji=lower, spacing_d=d,
        tol_ticks_used=tol, session_candle_count=n,
        levels=_build_grid(anchor, d, cfg),
        valid_from_ms=win.session_end_ms, valid_to_ms=win.london_end_ms,
    )
