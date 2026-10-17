"""Минимальные SVG-графики без внешних библиотек (панель работает и без интернета/CDN)."""
from __future__ import annotations

from html import escape

W, H, PAD_L, PAD_B, PAD_T, PAD_R = 600, 220, 44, 24, 10, 10


def _nice_max(v: float) -> float:
    if v <= 0:
        return 1
    mag = 10 ** (len(str(int(v))) - 1)
    for m in (1, 2, 2.5, 5, 10):
        if v <= m * mag:
            return m * mag
    return v


def _axes(ymax: float, fmt=lambda v: f"{v:g}") -> str:
    out = []
    for i in range(5):
        v = ymax * i / 4
        y = H - PAD_B - (H - PAD_B - PAD_T) * i / 4
        out.append(f'<line x1="{PAD_L}" x2="{W - PAD_R}" y1="{y:.1f}" y2="{y:.1f}" stroke="var(--line)"/>'
                   f'<text x="{PAD_L - 6}" y="{y + 4:.1f}" text-anchor="end">{escape(fmt(v))}</text>')
    return "".join(out)


def _svg(body: str, legend: list[tuple[str, str]] | None = None) -> str:
    leg = ""
    if legend:
        leg = '<div class="legend">' + "".join(
            f'<span><i style="background:{c}"></i>{escape(n)}</span>' for n, c in legend) + "</div>"
    return (f'{leg}<svg viewBox="0 0 {W} {H}" class="chart" role="img" preserveAspectRatio="none" '
            f'style="width:100%;height:auto;font-size:10px;fill:var(--muted)">{body}</svg>')


def bar_line(labels: list[str], bars: list[float], line: list[float] | None = None, *, bar_name: str = "",
             line_name: str = "", every: int = 1) -> str:
    n = max(1, len(labels))
    bmax = _nice_max(max(bars or [0]))
    inner_w = W - PAD_L - PAD_R
    step = inner_w / n
    body = [_axes(bmax)]
    for i, v in enumerate(bars):
        h = (H - PAD_B - PAD_T) * (v / bmax)
        x = PAD_L + i * step + step * 0.15
        body.append(f'<rect x="{x:.1f}" y="{H - PAD_B - h:.1f}" width="{step * 0.7:.1f}" height="{h:.1f}" rx="2" '
                    f'fill="var(--accent)"><title>{escape(labels[i])}: {v:g}</title></rect>')
    for i, lab in enumerate(labels):
        if i % every == 0:
            body.append(f'<text x="{PAD_L + i * step + step / 2:.1f}" y="{H - 8}" text-anchor="middle">'
                        f'{escape(lab)}</text>')
    legend = [(bar_name, "var(--accent)")] if bar_name else []
    if line:
        lmax = _nice_max(max(line or [0]))
        pts = " ".join(f"{PAD_L + i * step + step / 2:.1f},{H - PAD_B - (H - PAD_B - PAD_T) * v / lmax:.1f}"
                       for i, v in enumerate(line))
        body.append(f'<polyline points="{pts}" fill="none" stroke="var(--blue)" stroke-width="2"/>')
        body.append(f'<text x="{W - PAD_R}" y="{PAD_T + 2}" text-anchor="end" fill="var(--blue)">'
                    f'{escape(line_name)} max {lmax:g}</text>')
        legend.append((line_name, "var(--blue)"))
    return _svg("".join(body), legend)


def lines(labels: list[str], series: list[tuple[str, list[float | None], str]], every: int | None = None) -> str:
    vals = [v for _, s, _ in series for v in s if v is not None]
    if not vals:
        return '<div class="muted">Данных пока нет</div>'
    lo, hi = min(vals), max(vals)
    if hi == lo:
        hi, lo = hi + 1, max(0, lo - 1)
    pad = (hi - lo) * 0.1
    lo, hi = lo - pad, hi + pad
    n = max(2, len(labels))
    inner_w = W - PAD_L - PAD_R
    xs = lambda i: PAD_L + inner_w * i / (n - 1)
    ys = lambda v: H - PAD_B - (H - PAD_B - PAD_T) * (v - lo) / (hi - lo)
    body = []
    for i in range(5):
        v = lo + (hi - lo) * i / 4
        y = ys(v)
        body.append(f'<line x1="{PAD_L}" x2="{W - PAD_R}" y1="{y:.1f}" y2="{y:.1f}" stroke="var(--line)"/>'
                    f'<text x="{PAD_L - 6}" y="{y + 4:.1f}" text-anchor="end">{v:.0f}</text>')
    every = every or max(1, len(labels) // 6)
    for i, lab in enumerate(labels):
        if i % every == 0:
            body.append(f'<text x="{xs(i):.1f}" y="{H - 8}" text-anchor="middle">{escape(lab)}</text>')
    for name, s, color in series:
        pts = " ".join(f"{xs(i):.1f},{ys(v):.1f}" for i, v in enumerate(s) if v is not None)
        body.append(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2"/>')
        if len([v for v in s if v is not None]) == 1:
            i, v = next((i, v) for i, v in enumerate(s) if v is not None)
            body.append(f'<circle cx="{xs(i):.1f}" cy="{ys(v):.1f}" r="3" fill="{color}"/>')
    return _svg("".join(body), [(n, c) for n, _, c in series])
