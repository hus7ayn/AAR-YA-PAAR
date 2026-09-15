import unittest

from aar.core import models
from datetime import date, datetime, timezone

from aar.core.sessions import (
    expected_session_candles,
    funding_times_between,
    ist_date_of,
    window_for,
)
from aar.core import models as _m


def utc(y, mo, d, h, mi=0):
    return int(datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp() * 1000)


class TestSessionWindow(unittest.TestCase):
    def test_ist_boundaries_land_on_utc_hours(self):
        w = window_for(date(2026, 8, 1))
        self.assertEqual(w.session_start_ms, utc(2026, 8, 1, 0, 0))   # 05:30 IST
        self.assertEqual(w.session_end_ms, utc(2026, 8, 1, 7, 0))     # 12:30 IST
        self.assertEqual(w.london_end_ms, utc(2026, 8, 1, 13, 0))     # 18:30 IST

    def test_anchor_candle_precedes_session_on_previous_utc_day(self):
        _m.set_interval_ms(_m.interval_to_ms("15m"))
        w = window_for(date(2026, 8, 1))
        self.assertEqual(w.anchor_open_ms, utc(2026, 7, 31, 23, 45))
        # and it closes exactly when the session opens
        self.assertEqual(w.anchor_close_ms, w.session_start_ms)
        self.assertFalse(w.in_session(w.anchor_open_ms))

    def test_session_length_matches_the_configured_interval(self):
        w = window_for(date(2026, 8, 1))
        span = w.session_end_ms - w.session_start_ms
        self.assertEqual(span // _m.INTERVAL_MS, expected_session_candles())

    def test_candle_counts_for_each_supported_interval(self):
        w = window_for(date(2026, 8, 1))
        span = w.session_end_ms - w.session_start_ms
        for name, expect in (("3m", 140), ("15m", 28), ("5m", 84), ("1h", 7)):
            _m.set_interval_ms(_m.interval_to_ms(name))
            self.assertEqual(expected_session_candles(), expect, name)
        _m.set_interval_ms(_m.interval_to_ms("15m"))

    def test_window_membership_is_half_open(self):
        w = window_for(date(2026, 8, 1))
        self.assertTrue(w.in_session(w.session_start_ms))
        self.assertFalse(w.in_session(w.session_end_ms))       # exclusive
        self.assertTrue(w.in_london(w.session_end_ms))         # London starts here
        self.assertFalse(w.in_london(w.london_end_ms))

    def test_no_dst_shift_across_the_year(self):
        # IST has no DST; a January day must have the same UTC offsets as August.
        for d in (date(2026, 1, 15), date(2026, 8, 15)):
            w = window_for(d)
            start = datetime.fromtimestamp(w.session_start_ms / 1000, timezone.utc)
            self.assertEqual((start.hour, start.minute), (0, 0))

    def test_ist_date_of_maps_back(self):
        w = window_for(date(2026, 8, 1))
        self.assertEqual(ist_date_of(w.session_start_ms), date(2026, 8, 1))
        # 23:57 UTC on Jul 31 is already Aug 1 in IST
        self.assertEqual(ist_date_of(w.anchor_open_ms), date(2026, 8, 1))


class TestFunding(unittest.TestCase):
    def test_0800_utc_funding_falls_inside_london_window(self):
        w = window_for(date(2026, 8, 1))
        times = funding_times_between(w.session_end_ms, w.london_end_ms)
        self.assertEqual(times, [utc(2026, 8, 1, 8, 0)])

    def test_boundary_is_half_open(self):
        # a funding stamp exactly at start counts, exactly at end does not
        self.assertEqual(
            funding_times_between(utc(2026, 8, 1, 8, 0), utc(2026, 8, 1, 16, 0)),
            [utc(2026, 8, 1, 8, 0)],
        )

    def test_full_day_has_three(self):
        self.assertEqual(
            len(funding_times_between(utc(2026, 8, 1, 0), utc(2026, 8, 2, 0))), 3
        )


if __name__ == "__main__":
    unittest.main()
