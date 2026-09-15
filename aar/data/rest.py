"""Binance USD-M futures REST client — stdlib only.

The endpoint surface this system needs is small (klines, exchangeInfo, funding,
account, order), so we sign requests directly rather than take on a wrapper
library. That also sidesteps the recurring breakage where wrapper libraries lag
behind Binance's testnet base-URL changes.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from ..core.models import Candle

FAPI_LIVE = "https://fapi.binance.com"
FAPI_TESTNET = "https://testnet.binancefuture.com"

MAX_KLINES = 1000
RECV_WINDOW = 5000


class BinanceError(RuntimeError):
    def __init__(self, status: int, payload: str):
        self.status = status
        self.payload = payload
        super().__init__(f"HTTP {status}: {payload}")


@dataclass(slots=True)
class Credentials:
    api_key: str = ""
    api_secret: str = ""

    @classmethod
    def from_env(cls, testnet: bool = True) -> "Credentials":
        pre = "AAR_TESTNET" if testnet else "AAR_LIVE"
        return cls(
            api_key=os.environ.get(f"{pre}_API_KEY", ""),
            api_secret=os.environ.get(f"{pre}_API_SECRET", ""),
        )

    @property
    def present(self) -> bool:
        return bool(self.api_key and self.api_secret)


class FuturesClient:
    def __init__(
        self,
        testnet: bool = True,
        creds: Credentials | None = None,
        timeout: int = 20,
        max_retries: int = 4,
    ):
        self.base = FAPI_TESTNET if testnet else FAPI_LIVE
        self.testnet = testnet
        self.creds = creds or Credentials.from_env(testnet)
        self.timeout = timeout
        self.max_retries = max_retries
        self.used_weight = 0

    # ------------------------------------------------------------- transport

    def _request(self, method: str, path: str, params: dict | None = None,
                 signed: bool = False) -> object:
        params = dict(params or {})
        headers = {"User-Agent": "aar-ya-paar/1.0"}

        if signed:
            if not self.creds.present:
                raise BinanceError(401, "no API credentials in environment")
            params["timestamp"] = int(time.time() * 1000)
            params.setdefault("recvWindow", RECV_WINDOW)
            query = urllib.parse.urlencode(params, doseq=True)
            sig = hmac.new(
                self.creds.api_secret.encode(), query.encode(), hashlib.sha256
            ).hexdigest()
            query = f"{query}&signature={sig}"
            headers["X-MBX-APIKEY"] = self.creds.api_key
        else:
            query = urllib.parse.urlencode(params, doseq=True)

        url = f"{self.base}{path}"
        body = None
        if method in ("POST", "PUT", "DELETE"):
            body = query.encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif query:
            url = f"{url}?{query}"

        last: Exception | None = None
        for attempt in range(self.max_retries):
            req = urllib.request.Request(url, data=body, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    w = resp.headers.get("X-MBX-USED-WEIGHT-1M")
                    if w:
                        self.used_weight = int(w)
                    return json.loads(resp.read().decode())
            except urllib.error.HTTPError as e:
                payload = e.read().decode(errors="replace")
                # 429/418 = rate limited, 5xx = transient. Anything else is ours to fix.
                if e.code in (429, 418) or 500 <= e.code < 600:
                    last = BinanceError(e.code, payload)
                    time.sleep(min(2 ** attempt, 30))
                    continue
                raise BinanceError(e.code, payload) from None
            except (urllib.error.URLError, TimeoutError) as e:
                last = e
                time.sleep(min(2 ** attempt, 30))

        raise BinanceError(0, f"exhausted retries: {last}")

    # ---------------------------------------------------------- market data

    def exchange_info(self, symbol: str) -> dict:
        info = self._request("GET", "/fapi/v1/exchangeInfo")
        for s in info["symbols"]:
            if s["symbol"] == symbol:
                return s
        raise KeyError(f"symbol {symbol} not listed")

    def filters(self, symbol: str) -> dict[str, float]:
        """tick_size / step_size / min_notional straight from the exchange."""
        s = self.exchange_info(symbol)
        out: dict[str, float] = {}
        for f in s["filters"]:
            t = f["filterType"]
            if t == "PRICE_FILTER":
                out["tick_size"] = float(f["tickSize"])
            elif t == "LOT_SIZE":
                out["step_size"] = float(f["stepSize"])
            elif t == "MIN_NOTIONAL":
                out["min_notional"] = float(f["notional"])
        return out

    def klines(self, symbol: str, interval: str, start_ms: int | None = None,
               end_ms: int | None = None, limit: int = MAX_KLINES) -> list[Candle]:
        params = {"symbol": symbol, "interval": interval, "limit": min(limit, MAX_KLINES)}
        if start_ms is not None:
            params["startTime"] = start_ms
        if end_ms is not None:
            params["endTime"] = end_ms
        rows = self._request("GET", "/fapi/v1/klines", params)
        return [
            Candle(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]))
            for r in rows
        ]

    def klines_range(self, symbol: str, interval: str, start_ms: int,
                     end_ms: int) -> list[Candle]:
        """Page through an arbitrary range. Deduplicated and sorted."""
        out: list[Candle] = []
        cursor = start_ms
        seen: set[int] = set()
        while cursor < end_ms:
            batch = self.klines(symbol, interval, cursor, end_ms, MAX_KLINES)
            if not batch:
                break
            fresh = [c for c in batch if c.open_time not in seen]
            if not fresh:
                break
            for c in fresh:
                seen.add(c.open_time)
            out.extend(fresh)
            cursor = fresh[-1].open_time + 1
            if len(batch) < MAX_KLINES:
                break
        out.sort(key=lambda c: c.open_time)
        return [c for c in out if start_ms <= c.open_time < end_ms]

    def funding_history(self, symbol: str, start_ms: int, end_ms: int) -> list[tuple[int, float]]:
        """(fundingTime, fundingRate) pairs."""
        out: list[tuple[int, float]] = []
        cursor = start_ms
        while cursor < end_ms:
            rows = self._request("GET", "/fapi/v1/fundingRate", {
                "symbol": symbol, "startTime": cursor, "endTime": end_ms, "limit": 1000,
            })
            if not rows:
                break
            out.extend((int(r["fundingTime"]), float(r["fundingRate"])) for r in rows)
            nxt = int(rows[-1]["fundingTime"]) + 1
            if nxt <= cursor:
                break
            cursor = nxt
            if len(rows) < 1000:
                break
        return out

    def mark_price(self, symbol: str) -> float:
        r = self._request("GET", "/fapi/v1/premiumIndex", {"symbol": symbol})
        return float(r["markPrice"])

    def server_time(self) -> int:
        return int(self._request("GET", "/fapi/v1/time")["serverTime"])

    # -------------------------------------------------------------- signed

    def account(self) -> dict:
        return self._request("GET", "/fapi/v2/account", signed=True)

    def balance_usdt(self) -> float:
        for a in self._request("GET", "/fapi/v2/balance", signed=True):
            if a["asset"] == "USDT":
                return float(a["balance"])
        return 0.0

    def position(self, symbol: str) -> dict | None:
        rows = self._request("GET", "/fapi/v2/positionRisk", {"symbol": symbol}, signed=True)
        for r in rows:
            if float(r["positionAmt"]) != 0:
                return r
        return None

    def set_margin_type(self, symbol: str, margin_type: str) -> dict:
        """ISOLATED or CROSSED. Binance errors with -4046 if already set."""
        return self._request("POST", "/fapi/v1/marginType",
                             {"symbol": symbol, "marginType": margin_type}, signed=True)

    def set_leverage(self, symbol: str, leverage: int) -> dict:
        return self._request("POST", "/fapi/v1/leverage",
                             {"symbol": symbol, "leverage": leverage}, signed=True)

    def new_order(self, **params) -> dict:
        return self._request("POST", "/fapi/v1/order", params, signed=True)

    def cancel_order(self, symbol: str, order_id: int | None = None,
                     client_order_id: str | None = None) -> dict:
        p: dict = {"symbol": symbol}
        if order_id:
            p["orderId"] = order_id
        if client_order_id:
            p["origClientOrderId"] = client_order_id
        return self._request("DELETE", "/fapi/v1/order", p, signed=True)

    def cancel_all(self, symbol: str) -> dict:
        return self._request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol}, signed=True)

    def open_orders(self, symbol: str) -> list:
        return self._request("GET", "/fapi/v1/openOrders", {"symbol": symbol}, signed=True)
