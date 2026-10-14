"""Dependency-free SVG chart renderers for the dashboard.

Server-rendered <svg> markup, no JS and no CDN: dashboard.html stays a
single static file served by serve_dashboard.py from behind the tunnel's
basic auth, with zero external requests of any kind -- nothing about when
or whether the dashboard was viewed can leak to a third party. Pure
functions, no I/O, easy to unit test (see tests/test_ramu_charts.py).
"""
from __future__ import annotations

_PAD_L, _PAD_R, _PAD_T, _PAD_B = 8, 8, 14, 22


def _esc(s):
    if s is None:
        return ""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _empty_svg(width, height, label):
    return (
        f'<svg class="chart" viewBox="0 0 {width} {height}" width="100%" height="{height}">'
        f'<text x="{width/2}" y="{height/2}" text-anchor="middle" class="chart-empty">{_esc(label)}</text>'
        f"</svg>"
    )


def svg_line_chart(points, *, width=560, height=140, color="#0ca30c", target=None, empty_label="No data yet"):
    """points: [{"label": str, "value": float|None}, ...] oldest first.
    A None value is a gap in the data (no log that day) and breaks the
    line rather than being drawn as zero."""
    plot_w = width - _PAD_L - _PAD_R
    plot_h = height - _PAD_T - _PAD_B

    known = [p["value"] for p in points if p["value"] is not None]
    if not known:
        return _empty_svg(width, height, empty_label)

    bounds = known + ([target] if target is not None else [])
    lo, hi = min(bounds), max(bounds)
    if lo == hi:
        lo, hi = lo - 1, hi + 1
    span = hi - lo

    n = len(points)
    step = plot_w / max(n - 1, 1)

    def xy(i, v):
        x = _PAD_L + i * step
        y = _PAD_T + plot_h - ((v - lo) / span) * plot_h
        return x, y

    coords = [xy(i, p["value"]) if p["value"] is not None else None for i, p in enumerate(points)]

    segments, cur = [], []
    for c in coords:
        if c is None:
            if cur:
                segments.append(cur)
                cur = []
            continue
        cur.append(c)
    if cur:
        segments.append(cur)

    parts = [f'<svg class="chart" viewBox="0 0 {width} {height}" width="100%" height="{height}">']
    if target is not None:
        ty = xy(0, target)[1]
        parts.append(
            f'<line x1="{_PAD_L}" y1="{ty:.1f}" x2="{width - _PAD_R}" y2="{ty:.1f}" '
            f'class="chart-target" stroke-dasharray="4,3"/>'
        )
    for seg in segments:
        pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in seg)
        parts.append(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2"/>')
    for p, c in zip(points, coords):
        if c is None:
            continue
        x, y = c
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{color}"><title>{_esc(p["label"])}: {_esc(p["value"])}</title></circle>')

    for i in sorted({0, n // 2, n - 1}):
        x, _y = xy(i, points[i]["value"] if points[i]["value"] is not None else lo)
        parts.append(f'<text x="{x:.1f}" y="{height - 4}" class="chart-axis" text-anchor="middle">{_esc(points[i]["label"])}</text>')
    parts.append("</svg>")
    return "".join(parts)


def svg_bar_chart(points, *, width=560, height=140, color="#0ca30c", cap=None, empty_label="No data yet"):
    """points: [{"label": str, "value": float}, ...] oldest first."""
    if not points:
        return _empty_svg(width, height, empty_label)

    plot_w = width - _PAD_L - _PAD_R
    plot_h = height - _PAD_T - _PAD_B
    n = len(points)

    hi = max([p["value"] or 0 for p in points] + ([cap] if cap is not None else [0])) or 1
    gap = 4
    bar_w = max((plot_w - gap * (n - 1)) / n, 2)

    parts = [f'<svg class="chart" viewBox="0 0 {width} {height}" width="100%" height="{height}">']
    if cap is not None:
        cy = _PAD_T + plot_h - (cap / hi) * plot_h
        parts.append(
            f'<line x1="{_PAD_L}" y1="{cy:.1f}" x2="{width - _PAD_R}" y2="{cy:.1f}" '
            f'class="chart-target" stroke-dasharray="4,3"/>'
        )
    for i, p in enumerate(points):
        v = p["value"] or 0
        bh = (v / hi) * plot_h
        x = _PAD_L + i * (bar_w + gap)
        y = _PAD_T + plot_h - bh
        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{max(bh, 0):.1f}" fill="{color}">'
            f'<title>{_esc(p["label"])}: {_esc(p["value"])}</title></rect>'
        )
    for i in sorted({0, n // 2, n - 1}):
        x = _PAD_L + i * (bar_w + gap) + bar_w / 2
        parts.append(f'<text x="{x:.1f}" y="{height - 4}" class="chart-axis" text-anchor="middle">{_esc(points[i]["label"])}</text>')
    parts.append("</svg>")
    return "".join(parts)
