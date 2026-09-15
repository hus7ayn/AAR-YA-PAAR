"""Hand-rolled SVG candle chart with the level grid overlaid.

No matplotlib. The output is a standalone .svg viewable in any browser, which is
also easier to eyeball than a raster plot when checking whether levels land
sensibly against price.
"""

from __future__ import annotations

from pathlib import Path

from ..core.models import Candle, LevelKind, LevelSet
from ..core.sessions import SessionWindow, to_ist_str

W, H = 1480, 780
PAD_L, PAD_R, PAD_T, PAD_B = 78, 132, 56, 48
PLOT_W = W - PAD_L - PAD_R
PLOT_H = H - PAD_T - PAD_B

BG = "#0f1117"
GRID = "#232733"
TEXT = "#c9d1d9"
DIM = "#7d8590"
UP = "#3fb950"
DOWN = "#f85149"
SESSION_BG = "#161b26"
LONDON_BG = "#1a1f2e"
ANCHOR_C = "#e3b341"
FULL_C = "#58a6ff"
MID_C = "#456"
DOJI_C = "#d2a8ff"


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_day(
    candles: list[Candle],
    ls: LevelSet,
    win: SessionWindow,
    path: Path,
    title: str | None = None,
) -> Path:
    """Render one IST trading day: observation session + London session + levels."""
    candles = sorted(candles, key=lambda c: c.open_time)
    if not candles:
        raise ValueError("no candles to chart")

    lo = min(c.low for c in candles)
    hi = max(c.high for c in candles)
    if ls.valid and ls.levels:
        prices = ls.prices()
        # only stretch to levels that are plausibly near price, so one far
        # outer level cannot squash the candles into a flat line
        span = hi - lo
        near = [p for p in prices if lo - span <= p <= hi + span]
        if near:
            lo, hi = min(lo, min(near)), max(hi, max(near))
    pad = (hi - lo) * 0.06 or 1.0
    lo, hi = lo - pad, hi + pad

    t0 = candles[0].open_time
    t1 = candles[-1].close_time

    def x(ms: int) -> float:
        return PAD_L + PLOT_W * (ms - t0) / max(1, t1 - t0)

    def y(p: float) -> float:
        return PAD_T + PLOT_H * (hi - p) / max(1e-9, hi - lo)

    cw = max(1.4, PLOT_W / max(1, len(candles)) * 0.66)
    out: list[str] = []
    add = out.append

    add(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
        f'viewBox="0 0 {W} {H}" font-family="ui-monospace,SFMono-Regular,Menlo,monospace">')
    add(f'<rect width="{W}" height="{H}" fill="{BG}"/>')

    # session shading
    sx0, sx1 = x(win.session_start_ms), x(win.session_end_ms)
    lx1 = x(min(win.london_end_ms, t1))
    add(f'<rect x="{sx0:.1f}" y="{PAD_T}" width="{max(0,sx1-sx0):.1f}" height="{PLOT_H}" fill="{SESSION_BG}"/>')
    add(f'<rect x="{sx1:.1f}" y="{PAD_T}" width="{max(0,lx1-sx1):.1f}" height="{PLOT_H}" fill="{LONDON_BG}"/>')
    add(f'<line x1="{sx1:.1f}" y1="{PAD_T}" x2="{sx1:.1f}" y2="{PAD_T+PLOT_H}" '
        f'stroke="{ANCHOR_C}" stroke-width="1.2" stroke-dasharray="5 4" opacity="0.8"/>')
    add(f'<text x="{sx0+6:.1f}" y="{PAD_T+16}" fill="{DIM}" font-size="11">'
        f'observation 05:30-12:30 IST</text>')
    add(f'<text x="{sx1+6:.1f}" y="{PAD_T+16}" fill="{DIM}" font-size="11">'
        f'London 12:30-18:30 IST (levels frozen)</text>')

    # horizontal price gridlines
    for i in range(6):
        p = lo + (hi - lo) * i / 5
        yy = y(p)
        add(f'<line x1="{PAD_L}" y1="{yy:.1f}" x2="{PAD_L+PLOT_W}" y2="{yy:.1f}" '
            f'stroke="{GRID}" stroke-width="1"/>')
        add(f'<text x="{PAD_L-8}" y="{yy+4:.1f}" fill="{DIM}" font-size="10" '
            f'text-anchor="end">{p:,.0f}</text>')

    # session hi/lo
    if ls.session_high:
        for p, lab in ((ls.session_high, "session high"), (ls.session_low, "session low")):
            yy = y(p)
            add(f'<line x1="{sx0:.1f}" y1="{yy:.1f}" x2="{PAD_L+PLOT_W}" y2="{yy:.1f}" '
                f'stroke="{DIM}" stroke-width="1" stroke-dasharray="2 5" opacity="0.7"/>')
            add(f'<text x="{PAD_L+PLOT_W+6}" y="{yy+3:.1f}" fill="{DIM}" font-size="9">{lab}</text>')

    # levels
    for lv in ls.levels:
        yy = y(lv.price)
        if not (PAD_T - 2 <= yy <= PAD_T + PLOT_H + 2):
            continue
        if lv.kind is LevelKind.ANCHOR:
            col, sw, dash, op = ANCHOR_C, 2.0, "", 1.0
        elif lv.kind is LevelKind.FULL:
            col, sw, dash, op = FULL_C, 1.5, "", 0.95
        else:
            col, sw, dash, op = MID_C, 1.0, "4 4", 0.85
        add(f'<line x1="{PAD_L}" y1="{yy:.1f}" x2="{PAD_L+PLOT_W}" y2="{yy:.1f}" '
            f'stroke="{col}" stroke-width="{sw}" stroke-dasharray="{dash}" opacity="{op}"/>')
        tag = "A" if lv.kind is LevelKind.ANCHOR else f"{lv.k:+d}"
        add(f'<text x="{PAD_L+PLOT_W+6}" y="{yy+3:.1f}" fill="{col}" font-size="10">'
            f'{tag} {lv.price:,.1f}</text>')

    # candles
    for c in candles:
        cx = x(c.open_time + 90_000)
        col = UP if c.close >= c.open else DOWN
        add(f'<line x1="{cx:.1f}" y1="{y(c.high):.1f}" x2="{cx:.1f}" y2="{y(c.low):.1f}" '
            f'stroke="{col}" stroke-width="1"/>')
        yo, yc = y(c.open), y(c.close)
        top, hgt = min(yo, yc), max(0.8, abs(yc - yo))
        add(f'<rect x="{cx-cw/2:.1f}" y="{top:.1f}" width="{cw:.1f}" height="{hgt:.1f}" fill="{col}"/>')

    # doji markers
    for ref, lab in ((ls.upper_doji, "upper doji"), (ls.lower_doji, "lower doji")):
        if not ref:
            continue
        cx, cy = x(ref.open_time + 90_000), y(ref.close)
        add(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="5" fill="none" '
            f'stroke="{DOJI_C}" stroke-width="1.8"/>')
        add(f'<text x="{cx+8:.1f}" y="{cy-7:.1f}" fill="{DOJI_C}" font-size="10">'
            f'{lab} {ref.close:,.1f}</text>')

    # time axis
    step = max(1, len(candles) // 10)
    for c in candles[::step]:
        cx = x(c.open_time)
        add(f'<text x="{cx:.1f}" y="{PAD_T+PLOT_H+18}" fill="{DIM}" font-size="10" '
            f'text-anchor="middle">{to_ist_str(c.open_time, "%H:%M")}</text>')
    add(f'<text x="{W/2}" y="{H-12}" fill="{DIM}" font-size="10" text-anchor="middle">'
        f'time (IST)</text>')

    # header
    head = title or f"{ls.date}  BTCUSDT perp  3m"
    add(f'<text x="{PAD_L}" y="30" fill="{TEXT}" font-size="15" font-weight="600">{_esc(head)}</text>')
    if ls.valid:
        sub = (f"anchor {ls.anchor:,.1f}   D {ls.spacing_d:,.1f}   "
               f"half-D {ls.spacing_d/2:,.1f}   tol {ls.tol_ticks_used} tick(s)   "
               f"{len(ls.levels)} levels")
        col = TEXT
    else:
        sub = f"NO LEVELS — {ls.reason.value if ls.reason else 'invalid'}"
        col = DOWN
    add(f'<text x="{PAD_L}" y="47" fill="{col}" font-size="11">{_esc(sub)}</text>')

    add("</svg>")

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(out))
    return p
