"""Config loading. stdlib `tomllib` — no external dependency."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .core.levels import LevelConfig
from .core import models
from .core.models import DojiMode, interval_to_ms

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config.toml"


@dataclass(slots=True)
class MarketCfg:
    symbol: str = "BTCUSDT"
    market_type: str = "futures"
    interval: str = "15m"        # execution timeframe
    level_interval: str = "3m"   # timeframe the doji search runs on
    anchor_interval: str = "15m" # timeframe the ANCHOR candle is taken from
    # Which candle on that timeframe is the anchor:
    #   "first_session" -> the candle that OPENS at 05:30 (15m: 05:30-05:45).
    #                      Its CLOSE is the price at 05:45.
    #   "pre_session"   -> the candle that CLOSES at 05:30 (15m: 05:15-05:30).
    #                      Its close is the price AT 05:30.
    anchor_position: str = "first_session"
    # ISOLATED caps loss on a position at its own margin; CROSSED puts the whole
    # wallet behind it. The backtest's liquidation model assumes ISOLATED.
    margin_type: str = "ISOLATED"
    tick_size: float = 0.10
    step_size: float = 0.001
    min_notional: float = 50.0


@dataclass(slots=True)
class BacktestCfg:
    start: str = "2025-08-01"
    end: str = "2026-08-01"
    initial_equity: float = 10_000.0
    taker_fee: float = 0.0005
    maker_fee: float = 0.0002
    slippage_ticks: int = 1
    slippage_bps: float = 0.0
    # Fee optimisation. fee_discount multiplies every rate (0.9 = pay fees in BNB).
    fee_discount: float = 1.0
    # Post the entry as a resting post-only LIMIT instead of crossing the spread.
    # Earns the maker rate but may never fill — see entry_ttl_candles.
    post_only_entry: bool = False
    entry_ttl_candles: int = 2
    # Place the entry limit this many seconds BEFORE the candle closes, so the
    # order is already resting when the signal confirms instead of chasing price.
    entry_buffer_seconds: int = 12
    # Queue-position proxy. Touching our limit price does not guarantee a fill —
    # there is a queue at that price. Require the candle to trade THROUGH the
    # limit by this many ticks before we count ourselves filled.
    post_only_through_ticks: int = 0
    # Exit the target with a resting LIMIT (maker, no slippage) rather than a
    # TAKE_PROFIT_MARKET. Safe: it only fills if price reaches the target anyway.
    limit_target: bool = True
    leverage: float = 3.0
    funding_rate: float = 0.0001
    use_real_funding: bool = True


@dataclass(slots=True)
class RiskCfg:
    risk_per_trade_pct: float = 0.5
    max_qty: float = 1.0
    max_trades_per_day: int = 10
    flatten_at_london_close: bool = True


@dataclass(slots=True)
class LiveCfg:
    testnet: bool = True
    dry_run: bool = True
    poll_seconds: int = 5


@dataclass(slots=True)
class Paths:
    cache_dir: Path = ROOT / "out/cache"
    levels_dir: Path = ROOT / "out/levels"
    reports_dir: Path = ROOT / "out/reports"
    charts_dir: Path = ROOT / "out/charts"

    def ensure(self) -> None:
        for p in (self.cache_dir, self.levels_dir, self.reports_dir, self.charts_dir):
            p.mkdir(parents=True, exist_ok=True)


@dataclass(slots=True)
class Config:
    market: MarketCfg = field(default_factory=MarketCfg)
    levels: LevelConfig = field(default_factory=LevelConfig)
    backtest: BacktestCfg = field(default_factory=BacktestCfg)
    strategy_name: str = "null"
    strategy_params: dict = field(default_factory=dict)
    risk: RiskCfg = field(default_factory=RiskCfg)
    live: LiveCfg = field(default_factory=LiveCfg)
    paths: Paths = field(default_factory=Paths)

    @property
    def live_trading_enabled(self) -> bool:
        """Live orders need config AND an environment variable. Two locks, on purpose."""
        return (not self.live.testnet) and os.environ.get("AAR_ALLOW_LIVE") == "1"


def _pick(d: dict, cls, **overrides):
    fields = {f for f in cls.__slots__} if hasattr(cls, "__slots__") else set()
    kw = {k: v for k, v in d.items() if k in fields}
    kw.update(overrides)
    return cls(**kw)


def load(path: str | Path | None = None) -> Config:
    p = Path(path) if path else DEFAULT_CONFIG
    raw = tomllib.loads(p.read_text()) if p.exists() else {}

    market = _pick(raw.get("market", {}), MarketCfg)
    # Single-timeframe system: set the global candle interval once, here, before
    # anything derives session sizes or candle boundaries from it.
    models.set_interval_ms(interval_to_ms(market.interval))

    lv = raw.get("levels", {})
    levels = LevelConfig(
        tick_size=market.tick_size,
        doji_mode=DojiMode(lv.get("doji_mode", "tick")),
        doji_tol_ticks=lv.get("doji_tol_ticks", 1),
        adaptive_max_ticks=lv.get("adaptive_max_ticks", 50),
        steps_per_side=lv.get("steps_per_side", 3),
        min_spacing_ticks=lv.get("min_spacing_ticks", 1),
        require_opposite_halves=lv.get("require_opposite_halves", True),
        include_midpoints=lv.get("include_midpoints", True),
        min_session_candles=lv.get("min_session_candles", 0),
    )

    pth = raw.get("paths", {})
    paths = Paths(
        cache_dir=ROOT / pth.get("cache_dir", "out/cache"),
        levels_dir=ROOT / pth.get("levels_dir", "out/levels"),
        reports_dir=ROOT / pth.get("reports_dir", "out/reports"),
        charts_dir=ROOT / pth.get("charts_dir", "out/charts"),
    )

    strat = dict(raw.get("strategy", {}))
    strat_name = strat.pop("name", "null")
    # Per-strategy params live in their own table, e.g. [strategy.break_fade].
    # Fall back to the strategy table itself so flat keys also work.
    params = strat.pop(strat_name.replace("-", "_"), None)
    if params is None:
        params = {k: v for k, v in strat.items() if not isinstance(v, dict)}

    return Config(
        market=market,
        levels=levels,
        backtest=_pick(raw.get("backtest", {}), BacktestCfg),
        strategy_name=strat_name,
        strategy_params=params,
        risk=_pick(raw.get("risk", {}), RiskCfg),
        live=_pick(raw.get("live", {}), LiveCfg),
        paths=paths,
    )
