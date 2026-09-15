"""Historical candle loading with a local cache.

Bulk monthly archives from data.binance.vision are ~660 KB per month of 3m
candles, versus hundreds of paged REST calls for the same span — so archives are
the primary source and REST is the fallback for the current (incomplete) month
and for live top-ups.

Cached as plain CSV, one file per symbol-interval-month. No parquet, no pandas.
"""

from __future__ import annotations

import csv
import io
import urllib.error
import urllib.request
import zipfile
from datetime import date as Date, timedelta
from pathlib import Path

from ..core import models
from ..core.models import Candle
from .rest import FuturesClient

VISION = "https://data.binance.vision/data/futures/um/monthly/klines"
VISION_DAILY = "https://data.binance.vision/data/futures/um/daily/klines"


def _month_key(d: Date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _months_between(start: Date, end: Date) -> list[str]:
    out, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _parse_rows(rows) -> list[Candle]:
    out: list[Candle] = []
    for r in rows:
        if not r or not r[0]:
            continue
        try:
            ts = int(r[0])
        except ValueError:
            continue  # header row present in newer archives
        # Binance switched some archives from ms to microsecond timestamps.
        if ts > 10_000_000_000_000:
            ts //= 1000
        out.append(Candle(ts, float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])))
    return out


class HistoryStore:
    def __init__(self, symbol: str, interval: str, cache_dir: Path,
                 client: FuturesClient | None = None, verbose: bool = True):
        self.symbol = symbol
        self.interval = interval
        self.cache = Path(cache_dir)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.client = client or FuturesClient(testnet=False)
        self.verbose = verbose

    # ------------------------------------------------------------- caching

    def _cache_path(self, month: str) -> Path:
        return self.cache / f"{self.symbol}-{self.interval}-{month}.csv"

    def _write_cache(self, path: Path, candles: list[Candle]) -> None:
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["open_time", "open", "high", "low", "close", "volume"])
            for c in candles:
                w.writerow([c.open_time, c.open, c.high, c.low, c.close, c.volume])
        tmp.replace(path)

    def _read_cache(self, path: Path) -> list[Candle]:
        with path.open(newline="") as f:
            r = csv.reader(f)
            next(r, None)
            return _parse_rows(r)

    # -------------------------------------------------------------- source

    def _fetch_archive(self, month: str) -> list[Candle] | None:
        url = f"{VISION}/{self.symbol}/{self.interval}/{self.symbol}-{self.interval}-{month}.zip"
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                blob = resp.read()
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
            return None
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            name = z.namelist()[0]
            text = z.read(name).decode()
        return _parse_rows(csv.reader(io.StringIO(text)))

    def _fetch_rest_month(self, month: str) -> list[Candle]:
        y, m = (int(x) for x in month.split("-"))
        start = Date(y, m, 1)
        end = Date(y + 1, 1, 1) if m == 12 else Date(y, m + 1, 1)
        from datetime import datetime, timezone
        s = int(datetime(start.year, start.month, 1, tzinfo=timezone.utc).timestamp() * 1000)
        e = int(datetime(end.year, end.month, 1, tzinfo=timezone.utc).timestamp() * 1000)
        return self.client.klines_range(self.symbol, self.interval, s, e)

    def load_month(self, month: str, allow_rest: bool = True) -> list[Candle]:
        path = self._cache_path(month)
        if path.exists():
            return self._read_cache(path)

        candles = self._fetch_archive(month)
        source = "archive"
        if candles is None and allow_rest:
            candles = self._fetch_rest_month(month)
            source = "rest"
        if not candles:
            if self.verbose:
                print(f"  [warn] no data for {month}")
            return []

        # Only cache complete months. The current month keeps filling in.
        today_month = _month_key(Date.today())
        if month != today_month:
            self._write_cache(path, candles)
        if self.verbose:
            print(f"  {month}: {len(candles):>6} candles ({source})")
        return candles

    # --------------------------------------------------------------- range

    def load_range(self, start: Date, end: Date, pad_days: int = 1) -> list[Candle]:
        """All candles covering [start, end] IST days.

        `pad_days` widens the fetch so the 05:27 IST anchor candle — which sits
        on the *previous* UTC day — is always present for the first day.
        """
        lo = start - timedelta(days=pad_days)
        hi = end + timedelta(days=pad_days)
        out: list[Candle] = []
        for month in _months_between(lo, hi):
            out.extend(self.load_month(month))
        out.sort(key=lambda c: c.open_time)

        deduped: list[Candle] = []
        last = -1
        for c in out:
            if c.open_time != last:
                deduped.append(c)
                last = c.open_time
        return deduped


def index_by_open_time(candles: list[Candle]) -> dict[int, Candle]:
    return {c.open_time: c for c in candles}


def find_gaps(candles: list[Candle]) -> list[tuple[int, int]]:
    """(after_open_time, missing_count) for each discontinuity."""
    gaps = []
    for a, b in zip(candles, candles[1:]):
        step = b.open_time - a.open_time
        if step > models.INTERVAL_MS:
            gaps.append((a.open_time, step // models.INTERVAL_MS - 1))
    return gaps
