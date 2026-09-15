"""Order placement with exchange-filter compliance and hard safety locks.

Every path defaults to testnet + dry_run. Placing a real order on the live
exchange requires all three of:
    config: live.testnet = false
    config: live.dry_run = false
    env:    AAR_ALLOW_LIVE=1
"""

from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass

from ..config import Config
from ..core.models import Side
from ..data.rest import BinanceError, FuturesClient


@dataclass(slots=True)
class OrderResult:
    ok: bool
    dry_run: bool
    detail: dict
    message: str = ""


class Broker:
    def __init__(self, cfg: Config, client: FuturesClient | None = None):
        self.cfg = cfg
        self.client = client or FuturesClient(testnet=cfg.live.testnet)
        self.symbol = cfg.market.symbol
        self._filters_synced = False

    # ------------------------------------------------------------ guardrails

    @property
    def is_live(self) -> bool:
        return self.cfg.live_trading_enabled and not self.cfg.live.dry_run

    def describe_mode(self) -> str:
        if self.cfg.live.dry_run:
            return "DRY RUN (no orders sent)"
        if self.cfg.live.testnet:
            return "TESTNET (virtual funds)"
        if not self.cfg.live_trading_enabled:
            return "BLOCKED (set AAR_ALLOW_LIVE=1 to enable live trading)"
        return "*** LIVE — REAL FUNDS ***"

    # --------------------------------------------------------------- filters

    def sync_filters(self) -> dict[str, float]:
        """Pull tick/step/notional from the venue we will actually trade on.

        Testnet and live publish different filters (testnet's step size is finer),
        so these are always read from `self.client`, not assumed from config.
        """
        venue = "testnet" if self.cfg.live.testnet else "live"
        f = self.client.filters(self.symbol)
        m = self.cfg.market
        for key in ("tick_size", "step_size", "min_notional"):
            if key in f and abs(getattr(m, key) - f[key]) > 1e-12:
                print(f"  [filters] {key}: config {getattr(m,key)} -> {venue} {f[key]}")
                setattr(m, key, f[key])
        self._filters_synced = True
        return f

    def apply_account_settings(self) -> None:
        """Set margin type and leverage on the exchange to match config.

        Isolated margin caps the loss on a position at its own posted margin;
        crossed puts the whole wallet behind it. The backtest's liquidation
        model assumes isolated, so live must be isolated or the two disagree
        about what a losing trade can cost.
        """
        if not self.client.creds.present:
            print(f"  [account] no credentials — margin type and leverage NOT set "
                  f"(want {self.cfg.market.margin_type}, "
                  f"{self.cfg.backtest.leverage:g}x)")
            return
        want = self.cfg.market.margin_type.upper()
        try:
            self.client.set_margin_type(self.symbol, want)
            print(f"  [account] margin type -> {want}")
        except BinanceError as e:
            # -4046 "No need to change margin type" is success, not failure.
            if "-4046" in e.payload or "No need to change" in e.payload:
                print(f"  [account] margin type already {want}")
            else:
                print(f"  [warn] could not set margin type: {e}")
        try:
            self.client.set_leverage(self.symbol, int(self.cfg.backtest.leverage))
            print(f"  [account] leverage -> {int(self.cfg.backtest.leverage)}x")
        except BinanceError as e:
            print(f"  [warn] could not set leverage: {e}")

    def round_price(self, price: float) -> float:
        t = self.cfg.market.tick_size
        return round(round(price / t) * t, 8)

    def round_qty(self, qty: float) -> float:
        s = self.cfg.market.step_size
        # floor, never round up past a limit the account can afford
        return round(math.floor(qty / s) * s, 8)

    def validate(self, qty: float, price: float) -> tuple[bool, str]:
        q, p = self.round_qty(qty), self.round_price(price)
        if q <= 0:
            return False, "quantity rounds to zero at the exchange step size"
        if q * p < self.cfg.market.min_notional:
            return False, (f"notional {q*p:.2f} below minimum "
                           f"{self.cfg.market.min_notional}")
        return True, ""

    # ---------------------------------------------------------------- orders

    def _client_id(self, tag: str) -> str:
        """Idempotent, human-traceable, within Binance's 36-char limit."""
        return f"aar{tag}{uuid.uuid4().hex[:12]}"[:36]

    def market_order(self, side: Side, qty: float, ref_price: float,
                     reduce_only: bool = False, tag: str = "e") -> OrderResult:
        q, p = self.round_qty(qty), self.round_price(ref_price)
        ok, why = self.validate(q, p)
        if not ok:
            return OrderResult(False, True, {}, why)

        params = {
            "symbol": self.symbol,
            "side": "BUY" if side is Side.LONG else "SELL",
            "type": "MARKET",
            "quantity": f"{q:.8f}".rstrip("0").rstrip("."),
            "newClientOrderId": self._client_id(tag),
        }
        if reduce_only:
            params["reduceOnly"] = "true"

        if not self.is_live:
            return OrderResult(True, True, params,
                               f"{self.describe_mode()} — would send {params['side']} "
                               f"{params['quantity']} @~{p}")
        try:
            return OrderResult(True, False, self.client.new_order(**params), "sent")
        except BinanceError as e:
            return OrderResult(False, False, {}, str(e))

    def stop_market(self, side: Side, stop_price: float, tag: str = "s") -> OrderResult:
        """Reduce-only stop for an open position. `side` is the CLOSING side."""
        sp = self.round_price(stop_price)
        params = {
            "symbol": self.symbol,
            "side": "SELL" if side is Side.LONG else "BUY",
            "type": "STOP_MARKET",
            "stopPrice": f"{sp}",
            "closePosition": "true",
            "newClientOrderId": self._client_id(tag),
        }
        if not self.is_live:
            return OrderResult(True, True, params,
                               f"{self.describe_mode()} — would set stop @{sp}")
        try:
            return OrderResult(True, False, self.client.new_order(**params), "sent")
        except BinanceError as e:
            return OrderResult(False, False, {}, str(e))

    def post_only_limit(self, side: Side, qty: float, price: float,
                        tag: str = "p") -> OrderResult:
        """Resting entry that earns the maker rate.

        `GTX` is Binance's post-only time-in-force: the order is rejected
        outright rather than filled if it would cross the book and pay taker.
        That rejection is the desired behaviour — it guarantees we never pay
        taker on an entry the backtest priced as maker.
        """
        q, p = self.round_qty(qty), self.round_price(price)
        ok, why = self.validate(q, p)
        if not ok:
            return OrderResult(False, True, {}, why)

        params = {
            "symbol": self.symbol,
            "side": "BUY" if side is Side.LONG else "SELL",
            "type": "LIMIT",
            "timeInForce": "GTX",
            "price": f"{p}",
            "quantity": f"{q:.8f}".rstrip("0").rstrip("."),
            "newClientOrderId": self._client_id(tag),
        }
        if not self.is_live:
            return OrderResult(True, True, params,
                               f"{self.describe_mode()} — would post-only "
                               f"{params['side']} {params['quantity']} @{p}")
        try:
            return OrderResult(True, False, self.client.new_order(**params), "sent")
        except BinanceError as e:
            return OrderResult(False, False, {}, str(e))

    def limit_target(self, side: Side, qty: float, target_price: float,
                     tag: str = "l") -> OrderResult:
        """Reduce-only resting LIMIT at the target — maker, and no slippage.

        Preferred over `take_profit_market`: it only ever fills at the target
        price or better, and pays the maker rate rather than taker.
        `side` is the POSITION side.
        """
        q, p = self.round_qty(qty), self.round_price(target_price)
        params = {
            "symbol": self.symbol,
            "side": "SELL" if side is Side.LONG else "BUY",
            "type": "LIMIT",
            "timeInForce": "GTC",
            "price": f"{p}",
            "quantity": f"{q:.8f}".rstrip("0").rstrip("."),
            "reduceOnly": "true",
            "newClientOrderId": self._client_id(tag),
        }
        if not self.is_live:
            return OrderResult(True, True, params,
                               f"{self.describe_mode()} — would rest LIMIT target @{p}")
        try:
            return OrderResult(True, False, self.client.new_order(**params), "sent")
        except BinanceError as e:
            return OrderResult(False, False, {}, str(e))

    def take_profit_market(self, side: Side, target_price: float,
                           tag: str = "t") -> OrderResult:
        """Reduce-only take-profit for an open position. `side` is the POSITION side.

        Without this the strategy's 750 USDT target exists only in the backtest:
        live positions would carry a stop and no target, so every 7.5R winner
        would instead exit at the stop or at the London-close flatten.
        """
        tp = self.round_price(target_price)
        params = {
            "symbol": self.symbol,
            "side": "SELL" if side is Side.LONG else "BUY",
            "type": "TAKE_PROFIT_MARKET",
            "stopPrice": f"{tp}",
            "closePosition": "true",
            "newClientOrderId": self._client_id(tag),
        }
        if not self.is_live:
            return OrderResult(True, True, params,
                               f"{self.describe_mode()} — would set target @{tp}")
        try:
            return OrderResult(True, False, self.client.new_order(**params), "sent")
        except BinanceError as e:
            return OrderResult(False, False, {}, str(e))

    def flatten(self, ref_price: float) -> OrderResult:
        """Close any open position and cancel resting orders."""
        # In dry run with no keys there is no remote position to query, and the
        # 401 would be pure noise rather than a real failure.
        if self.cfg.live.dry_run and not self.client.creds.present:
            return OrderResult(True, True, {}, "dry run, no credentials — nothing to flatten")
        try:
            pos = self.client.position(self.symbol)
        except BinanceError as e:
            return OrderResult(False, self.cfg.live.dry_run, {}, f"position query failed: {e}")

        if not pos:
            return OrderResult(True, not self.is_live, {}, "no open position")

        amt = float(pos["positionAmt"])
        side = Side.SHORT if amt > 0 else Side.LONG  # closing side
        res = self.market_order(side, abs(amt), ref_price, reduce_only=True, tag="x")
        if self.is_live:
            try:
                self.client.cancel_all(self.symbol)
            except BinanceError:
                pass
        return res

    # ------------------------------------------------------------ kill switch

    def kill(self, reason: str) -> None:
        print(f"\n  !! KILL SWITCH: {reason}")
        try:
            mark = self.client.mark_price(self.symbol)
        except Exception:
            mark = 0.0
        print("  " + self.flatten(mark).message)
        if self.is_live:
            try:
                self.client.cancel_all(self.symbol)
                print("  all open orders cancelled")
            except BinanceError as e:
                print(f"  [warn] cancel_all failed: {e}")


def wait_until(target_ms: int, poll: int = 5, label: str = "") -> None:
    """Block until wall clock passes target_ms."""
    while True:
        now = int(time.time() * 1000)
        if now >= target_ms:
            return
        remaining = (target_ms - now) / 1000
        if label and remaining % 300 < poll:
            print(f"  waiting for {label}: {remaining/60:.1f} min")
        time.sleep(min(poll, max(1, remaining)))
