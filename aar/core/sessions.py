"""IST session window arithmetic.

IST (Asia/Kolkata) is UTC+5:30 year-round with no DST, so every boundary in this
strategy lands on an exact UTC hour:

    05:30 IST -> 00:00 UTC   (session start / anchor boundary)
    12:30 IST -> 07:00 UTC   (session end, levels computed)
    18:30 IST -> 13:00 UTC   (London close, levels expire)

That makes the observation session exactly 140 three-minute candles. We still go
through `zoneinfo` rather than hardcoding the offset, so the intent stays legible
and a future tz-database change would be picked up rather than silently wrong.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as Date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from . import models

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc

SESSION_START_IST = time(5, 30)   # observation window opens
SESSION_END_IST = time(12, 30)    # observation window closes; levels computed here
LONDON_END_IST = time(18, 30)     # levels expire

def expected_session_candles(interval_ms: int | None = None) -> int:
    """Candles in the 7h observation window at the configured interval.

    7h / 3m = 140.  7h / 15m = 28.  Every session boundary lands on an exact
    UTC hour, so this divides cleanly for any interval that divides an hour.
    """
    span = int((datetime.combine(Date(2000, 1, 1), SESSION_END_IST, tzinfo=IST)
                - datetime.combine(Date(2000, 1, 1), SESSION_START_IST, tzinfo=IST))
               .total_seconds() * 1000)
    return span // (interval_ms or models.INTERVAL_MS)


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def ist_datetime(d: Date, t: time) -> datetime:
    return datetime.combine(d, t, tzinfo=IST)


@dataclass(frozen=True, slots=True)
class SessionWindow:
    """All UTC-ms boundaries for one IST trading day."""
    date: Date

    anchor_open_ms: int   # open time of the 05:27-05:30 IST candle
    session_start_ms: int  # 05:30 IST — first observation candle opens
    session_end_ms: int    # 12:30 IST — exclusive end of observation window
    london_end_ms: int     # 18:30 IST — exclusive end of trading window
    interval_ms: int = 0   # candle interval this window was built for

    @property
    def date_str(self) -> str:
        return self.date.isoformat()

    @property
    def anchor_close_ms(self) -> int:
        return self.anchor_open_ms + self.interval_ms

    def in_session(self, open_time_ms: int) -> bool:
        """Is this candle part of the 05:30-12:30 IST observation window?"""
        return self.session_start_ms <= open_time_ms < self.session_end_ms

    def in_london(self, open_time_ms: int) -> bool:
        """Is this candle part of the 12:30-18:30 IST trading window?"""
        return self.session_end_ms <= open_time_ms < self.london_end_ms


def window_for(d: Date, interval_ms: int | None = None) -> SessionWindow:
    """Boundaries for one IST day.

    `interval_ms` only affects where the ANCHOR candle opens — the session and
    London boundaries are wall-clock and identical at every timeframe. Pass it
    to build a window for a timeframe other than the configured one (the level
    grid and the execution loop can run on different candles).
    """
    step = interval_ms or models.INTERVAL_MS
    session_start = _ms(ist_datetime(d, SESSION_START_IST))
    return SessionWindow(
        date=d,
        # The anchor candle CLOSES at 05:30, so it OPENS one interval earlier —
        # it is the last candle before the observation window, not the first in it.
        anchor_open_ms=session_start - step,
        interval_ms=step,
        session_start_ms=session_start,
        session_end_ms=_ms(ist_datetime(d, SESSION_END_IST)),
        london_end_ms=_ms(ist_datetime(d, LONDON_END_IST)),
    )


def anchor_open_ms_for(win: SessionWindow, position: str) -> int:
    """Open time of the anchor candle.

    "pre_session"   the candle CLOSING at 05:30 — its close is the price at 05:30.
    "first_session" the candle OPENING at 05:30 — its close is one interval later
                    (05:45 at 15m), which is the first *completed* candle of the
                    observation window.
    """
    if position == "pre_session":
        return win.anchor_open_ms
    if position == "first_session":
        return win.session_start_ms
    raise ValueError(f"unknown anchor_position {position!r}")


def ist_date_of(ms: int) -> Date:
    return datetime.fromtimestamp(ms / 1000, UTC).astimezone(IST).date()


def to_ist_str(ms: int, fmt: str = "%Y-%m-%d %H:%M") -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).astimezone(IST).strftime(fmt)


def daterange(start: Date, end: Date):
    """Inclusive on both ends."""
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


# Binance USD-M funding is charged at 00:00 / 08:00 / 16:00 UTC.
# Note 08:00 UTC == 13:30 IST, which falls INSIDE the 12:30-18:30 trading window,
# so any position held across it pays (or receives) funding.
FUNDING_HOURS_UTC = (0, 8, 16)


def funding_times_between(start_ms: int, end_ms: int) -> list[int]:
    """Funding timestamps in [start_ms, end_ms)."""
    out: list[int] = []
    d = datetime.fromtimestamp(start_ms / 1000, UTC).replace(
        minute=0, second=0, microsecond=0
    )
    # step back one hour so a funding time exactly at start_ms is considered
    d -= timedelta(hours=1)
    while _ms(d) < end_ms:
        if d.hour in FUNDING_HOURS_UTC and start_ms <= _ms(d) < end_ms:
            out.append(_ms(d))
        d += timedelta(hours=1)
    return out
