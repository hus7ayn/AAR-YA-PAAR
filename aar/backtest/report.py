"""Metrics and diagnostics."""

from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from ..core.models import LevelSet
from .engine import BacktestResult


def _pct(n: int, d: int) -> float:
    return 100.0 * n / d if d else 0.0


def quantiles(xs: list[float], qs=(0.1, 0.25, 0.5, 0.75, 0.9)) -> dict[str, float]:
    if not xs:
        return {f"p{int(q*100)}": 0.0 for q in qs}
    s = sorted(xs)
    out = {}
    for q in qs:
        i = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
        out[f"p{int(q*100)}"] = s[i]
    return out


# ------------------------------------------------------------ level diagnostics

@dataclass(slots=True)
class LevelDiagnostics:
    mode: str
    total_days: int
    valid_days: int
    reasons: dict[str, int]
    d_values: list[float]
    grid_vs_range: list[float]  # full grid width / realised session range

    @property
    def valid_pct(self) -> float:
        return _pct(self.valid_days, self.total_days)

    def summary_row(self) -> dict:
        q = quantiles(self.d_values)
        return {
            "mode": self.mode,
            "valid_pct": round(self.valid_pct, 1),
            "valid_days": self.valid_days,
            "total_days": self.total_days,
            "D_p10": round(q["p10"], 1),
            "D_median": round(q["p50"], 1),
            "D_p90": round(q["p90"], 1),
            "D_max_over_min": round(max(self.d_values) / min(self.d_values), 1)
            if self.d_values and min(self.d_values) > 0 else 0.0,
            "grid_vs_range_median": round(quantiles(self.grid_vs_range)["p50"], 2),
            "top_failure": max(self.reasons, key=self.reasons.get) if self.reasons else "-",
        }


def diagnose(mode: str, level_sets: list[LevelSet]) -> LevelDiagnostics:
    reasons = Counter()
    d_values, ratios = [], []
    valid = 0
    for ls in level_sets:
        if ls.valid:
            valid += 1
            d_values.append(ls.spacing_d)
            rng = ls.session_high - ls.session_low
            if rng > 0 and ls.levels:
                width = max(ls.prices()) - min(ls.prices())
                ratios.append(width / rng)
        elif ls.reason:
            reasons[ls.reason.value] += 1
    return LevelDiagnostics(mode, len(level_sets), valid, dict(reasons), d_values, ratios)


def render_diagnostics_table(diags: list[LevelDiagnostics]) -> str:
    rows = [d.summary_row() for d in diags]
    cols = ["mode", "valid_pct", "valid_days", "total_days", "D_p10", "D_median",
            "D_p90", "D_max_over_min", "grid_vs_range_median", "top_failure"]
    head = {
        "mode": "doji_mode", "valid_pct": "valid%", "valid_days": "valid",
        "total_days": "days", "D_p10": "D p10", "D_median": "D med",
        "D_p90": "D p90", "D_max_over_min": "D max/min",
        "grid_vs_range_median": "grid/range", "top_failure": "top failure",
    }
    widths = {c: max(len(head[c]), *(len(str(r[c])) for r in rows)) for c in cols}
    line = "  ".join(head[c].rjust(widths[c]) for c in cols)
    sep = "  ".join("-" * widths[c] for c in cols)
    body = "\n".join("  ".join(str(r[c]).rjust(widths[c]) for c in cols) for r in rows)
    return f"{line}\n{sep}\n{body}"


# --------------------------------------------------------------- backtest stats

@dataclass(slots=True)
class Metrics:
    trades: int
    wins: int
    losses: int
    win_rate: float
    gross_pnl: float
    fees: float
    funding: float
    net_pnl: float
    return_pct: float
    avg_win: float
    avg_loss: float
    profit_factor: float
    expectancy: float
    max_drawdown: float
    max_drawdown_pct: float
    sharpe_daily: float
    best_day: float
    worst_day: float
    traded_days: int
    valid_days: int
    total_days: int
    margin_rejects: int = 0
    blown_days: int = 0
    entries_unfilled: int = 0
    size_rejects: int = 0

    def render(self) -> str:
        rows = [
            ("Days examined", f"{self.total_days}"),
            ("Days with valid levels", f"{self.valid_days} ({_pct(self.valid_days, self.total_days):.1f}%)"),
            ("Days with trades", f"{self.traded_days}"),
            ("Entries refused (margin)", f"{self.margin_rejects}"),
            ("Entries refused (min size)", f"{self.size_rejects}"),
            ("Post-only limits unfilled", f"{self.entries_unfilled}"),
            ("Days account was blown", f"{self.blown_days}"),
            ("", ""),
            ("Trades", f"{self.trades}"),
            ("Win rate", f"{self.win_rate:.1f}%  ({self.wins}W / {self.losses}L)"),
            ("Avg win / avg loss", f"{self.avg_win:,.2f} / {self.avg_loss:,.2f}"),
            ("Profit factor", f"{self.profit_factor:.2f}"),
            ("Expectancy / trade", f"{self.expectancy:,.2f}"),
            ("", ""),
            # Signed as impact on equity: negative = paid out, positive = received.
            # Funding can genuinely be a credit, so the sign is always explicit.
            ("Gross PnL", f"{self.gross_pnl:+,.2f}"),
            ("Fees", f"{-self.fees:+,.2f}"),
            ("Funding", f"{-self.funding:+,.2f}"),
            ("Net PnL", f"{self.net_pnl:,.2f}"),
            ("Return", f"{self.return_pct:.2f}%"),
            ("", ""),
            ("Max drawdown", f"{self.max_drawdown:,.2f} ({self.max_drawdown_pct:.2f}%)"),
            ("Sharpe (daily, ann.)", f"{self.sharpe_daily:.2f}"),
            ("Best / worst day", f"{self.best_day:,.2f} / {self.worst_day:,.2f}"),
        ]
        w = max(len(k) for k, _ in rows)
        return "\n".join(f"  {k.ljust(w)}  {v}" if k else "" for k, v in rows)


def compute_metrics(res: BacktestResult) -> Metrics:
    trades = res.trades
    pnls = [t.pnl_net for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    gross = sum(t.pnl_gross for t in trades)
    fees = sum(t.fees for t in trades)
    funding = sum(t.funding for t in trades)
    net = sum(pnls)

    # equity curve by day
    eq, curve = res.initial_equity, [res.initial_equity]
    daily = []
    for d in res.days:
        daily.append(d.pnl)
        eq += d.pnl
        curve.append(eq)

    peak, mdd = curve[0], 0.0
    mdd_pct = 0.0
    for v in curve:
        peak = max(peak, v)
        dd = peak - v
        if dd > mdd:
            mdd, mdd_pct = dd, (100.0 * dd / peak if peak else 0.0)

    if len(daily) > 1:
        mean = sum(daily) / len(daily)
        var = sum((x - mean) ** 2 for x in daily) / (len(daily) - 1)
        sd = math.sqrt(var)
        sharpe = (mean / sd) * math.sqrt(252) if sd > 0 else 0.0
    else:
        sharpe = 0.0

    gross_win = sum(wins)
    gross_loss = abs(sum(losses))

    return Metrics(
        trades=len(trades), wins=len(wins), losses=len(losses),
        win_rate=_pct(len(wins), len(trades)),
        gross_pnl=gross, fees=fees, funding=funding, net_pnl=net,
        return_pct=100.0 * net / res.initial_equity if res.initial_equity else 0.0,
        avg_win=sum(wins) / len(wins) if wins else 0.0,
        avg_loss=sum(losses) / len(losses) if losses else 0.0,
        profit_factor=gross_win / gross_loss if gross_loss > 0 else (
            float("inf") if gross_win > 0 else 0.0),
        expectancy=net / len(trades) if trades else 0.0,
        max_drawdown=mdd, max_drawdown_pct=mdd_pct, sharpe_daily=sharpe,
        best_day=max(daily) if daily else 0.0,
        worst_day=min(daily) if daily else 0.0,
        traded_days=len(res.traded_days), valid_days=len(res.valid_days),
        total_days=len(res.days),
        margin_rejects=sum(d.margin_rejects for d in res.days),
        blown_days=sum(1 for d in res.days if d.blown),
        entries_unfilled=sum(d.entries_unfilled for d in res.days),
        size_rejects=sum(d.size_rejects for d in res.days),
    )


def write_trades_csv(res: BacktestResult, path: Path) -> None:
    import csv
    with Path(path).open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "side", "qty", "entry_time", "entry_price", "exit_time",
                    "exit_price", "pnl_gross", "fees", "funding", "pnl_net",
                    "signal_time", "entry_reason", "exit_reason"])
        for t in res.trades:
            w.writerow([t.date, t.side.value, t.qty, t.entry_time, t.entry_price,
                        t.exit_time, t.exit_price, round(t.pnl_gross, 4),
                        round(t.fees, 4), round(t.funding, 4), round(t.pnl_net, 4),
                        t.signal_time, t.entry_reason, t.exit_reason])


def write_levels_json(level_sets: list[LevelSet], path: Path) -> None:
    Path(path).write_text(json.dumps(
        [ls.to_json_obj() for ls in level_sets], indent=2, sort_keys=True))
