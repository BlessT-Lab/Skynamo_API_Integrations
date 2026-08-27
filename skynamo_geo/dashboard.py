"""Build a self-contained HTML dashboard from the local extract store.

No dependencies and no external requests: charts are hand-built inline SVG and
the CSS is inline, so the file works offline, opens in any browser, survives
being emailed, and adds nothing to the packaged .exe.

Every panel is one SQL aggregate over report_store, and every panel degrades to
an explicit "no data" state rather than an empty box - a dashboard that silently
shows nothing is worse than one that says why.

All text from the instance (customer/product/user names) is HTML-escaped.
"""

import html
import os
from datetime import datetime

# Palette chosen to stay legible printed and in both light and dark viewers.
_INK = "#1f2933"
_MUTED = "#6b7280"
_ACCENT = "#2f6fd0"
_ACCENT_SOFT = "#dbe7fa"
_GRID = "#e5e7eb"
_WARN = "#b45309"

_SERIES_COLOURS = ["#2f6fd0", "#2ea36b", "#b45309", "#8b5cf6", "#d64545",
                   "#0e7490", "#a16207", "#be185d"]


# ---------------------------------------------------------------------------
# Small formatting helpers
# ---------------------------------------------------------------------------

def _esc(value):
    return html.escape("" if value is None else str(value))


def _num(value, places=0):
    """Thousands-separated number, blank-safe."""
    try:
        value = float(value or 0)
    except (TypeError, ValueError):
        return "0"
    if places:
        return f"{value:,.{places}f}"
    return f"{value:,.0f}"


def _money(value):
    return _num(value, 2)


def _nice_max(value):
    """Round a maximum up to something a human would label an axis with."""
    if value <= 0:
        return 1.0
    magnitude = 10 ** (len(str(int(value))) - 1)
    for step in (1, 2, 2.5, 5, 10):
        candidate = magnitude * step
        if candidate >= value:
            return float(candidate)
    return float(magnitude * 10)


# ---------------------------------------------------------------------------
# Charts (inline SVG)
# ---------------------------------------------------------------------------

def _no_data(message="No data for this period."):
    return f'<p class="nodata">{_esc(message)}</p>'


def _bar_chart(pairs, value_format=_num, width=680, bar_height=26):
    """Horizontal bar chart from [(label, value)] - good for rankings.

    Horizontal because the labels are customer/product names, which do not fit
    under vertical bars.
    """
    pairs = [(l, float(v or 0)) for l, v in pairs if v is not None]
    if not pairs:
        return _no_data()
    top = max(v for _l, v in pairs) or 1.0
    label_w = 190
    track_w = width - label_w - 90
    height = len(pairs) * (bar_height + 8) + 8

    parts = [f'<svg class="chart" viewBox="0 0 {width} {height}" '
             f'role="img" width="100%" height="{height}">']
    for i, (label, value) in enumerate(pairs):
        y = 8 + i * (bar_height + 8)
        bar_w = max(1.0, (value / top) * track_w)
        parts.append(
            f'<text x="0" y="{y + bar_height * 0.7:.0f}" class="lbl">'
            f'{_esc(_truncate(label, 26))}</text>')
        parts.append(
            f'<rect x="{label_w}" y="{y}" width="{track_w}" '
            f'height="{bar_height}" fill="{_GRID}" rx="3"/>')
        parts.append(
            f'<rect x="{label_w}" y="{y}" width="{bar_w:.1f}" '
            f'height="{bar_height}" fill="{_ACCENT}" rx="3"/>')
        parts.append(
            f'<text x="{label_w + track_w + 8}" '
            f'y="{y + bar_height * 0.7:.0f}" class="val">'
            f'{_esc(value_format(value))}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _line_chart(pairs, value_format=_num, width=680, height=220):
    """Time series from [(label, value)], labels already in order."""
    pairs = [(l, float(v or 0)) for l, v in pairs if v is not None]
    if not pairs:
        return _no_data()
    if len(pairs) == 1:
        # A single point is not a line - show it as a bar so it is still visible.
        return _bar_chart(pairs, value_format=value_format, width=width)

    pad_l, pad_r, pad_t, pad_b = 64, 16, 16, 40
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    top = _nice_max(max(v for _l, v in pairs))
    step = plot_w / (len(pairs) - 1)

    points = []
    for i, (_label, value) in enumerate(pairs):
        x = pad_l + i * step
        y = pad_t + plot_h - (value / top) * plot_h
        points.append((x, y))

    parts = [f'<svg class="chart" viewBox="0 0 {width} {height}" '
             f'role="img" width="100%" height="{height}">']
    # Horizontal gridlines + y labels
    for frac in (0, 0.25, 0.5, 0.75, 1.0):
        y = pad_t + plot_h - frac * plot_h
        parts.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{pad_l + plot_w}" '
                     f'y2="{y:.1f}" stroke="{_GRID}" stroke-width="1"/>')
        parts.append(f'<text x="{pad_l - 8}" y="{y + 4:.1f}" '
                     f'class="axis" text-anchor="end">'
                     f'{_esc(value_format(top * frac))}</text>')
    # Area + line
    area = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
    parts.append(
        f'<polygon points="{pad_l},{pad_t + plot_h} {area} '
        f'{pad_l + plot_w},{pad_t + plot_h}" fill="{_ACCENT_SOFT}"/>')
    parts.append(f'<polyline points="{area}" fill="none" stroke="{_ACCENT}" '
                 f'stroke-width="2"/>')
    for x, y in points:
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" '
                     f'fill="{_ACCENT}"/>')
    # X labels: thin them out so they never overlap
    stride = max(1, len(pairs) // 8)
    for i, (label, _value) in enumerate(pairs):
        if i % stride and i != len(pairs) - 1:
            continue
        x = pad_l + i * step
        parts.append(f'<text x="{x:.1f}" y="{height - 14}" class="axis" '
                     f'text-anchor="middle">{_esc(_truncate(label, 10))}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _truncate(text, limit):
    text = "" if text is None else str(text)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _kpi_tile(label, value, sub=""):
    sub_html = f'<div class="kpi-sub">{_esc(sub)}</div>' if sub else ""
    return (f'<div class="kpi"><div class="kpi-label">{_esc(label)}</div>'
            f'<div class="kpi-value">{_esc(value)}</div>{sub_html}</div>')


def _table(headers, rows, empty="No data for this period."):
    if not rows:
        return _no_data(empty)
    head = "".join(f"<th>{_esc(h)}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{_esc(c)}</td>" for c in row) + "</tr>"
        for row in rows)
    return (f'<div class="tablewrap"><table><thead><tr>{head}</tr></thead>'
            f'<tbody>{body}</tbody></table></div>')


def _panel(title, body, note=""):
    note_html = f'<p class="note">{_esc(note)}</p>' if note else ""
    return (f'<section class="panel"><h2>{_esc(title)}</h2>'
            f'{note_html}{body}</section>')


# ---------------------------------------------------------------------------
# Panel data (one aggregate each)
# ---------------------------------------------------------------------------
# Customer attribution goes through activities rather than trusting the
# denormalised customer_name on order_totals, which can come back empty.

_LIVE = "is_deleted=0"


def _invoice_totals(store):
    """(invoiced, outstanding) from whichever invoice table holds data.

    Invoice lines can arrive either as the /v2/invoices root entity or nested
    under customers, depending on what was selected for extract, so both are
    checked. outstanding_balance is per invoice but repeated on every line, so
    it is summed over DISTINCT sale_id rather than over lines.
    """
    for table in ("customer_invoices", "invoices"):
        invoiced = store.scalar(
            f"SELECT SUM(value) FROM {table} WHERE {_LIVE}", default=0)
        outstanding = store.scalar(
            "SELECT SUM(outstanding_balance) FROM ("
            f"  SELECT DISTINCT sale_id, outstanding_balance FROM {table} "
            f"  WHERE {_LIVE})", default=0)
        if invoiced or outstanding:
            return invoiced, outstanding
    return 0, 0


def _kpis(store):
    orders = store.scalar(f"SELECT COUNT(*) FROM order_totals WHERE {_LIVE}")
    order_value = store.scalar(
        f"SELECT SUM(subtotal_value) FROM order_totals WHERE {_LIVE}", default=0)
    invoiced, outstanding = _invoice_totals(store)
    visits = store.scalar(f"SELECT COUNT(*) FROM activity_visits WHERE {_LIVE}")
    visit_secs = store.scalar(
        f"SELECT SUM(duration_sec) FROM activity_visits WHERE {_LIVE}", default=0)
    customers = store.scalar(
        f"SELECT COUNT(*) FROM customers WHERE {_LIVE} AND is_active=1")
    activities = store.scalar(f"SELECT COUNT(*) FROM activities WHERE {_LIVE}")

    tiles = [
        _kpi_tile("Orders", _num(orders), f"{_money(order_value)} total value"),
        _kpi_tile("Invoiced", _money(invoiced),
                  f"{_money(outstanding)} outstanding"),
        _kpi_tile("Visits", _num(visits),
                  f"{_num((visit_secs or 0) / 3600, 1)} hours on visits"),
        _kpi_tile("Activities", _num(activities)),
        _kpi_tile("Active customers", _num(customers)),
    ]
    return f'<div class="kpis">{"".join(tiles)}</div>'


def _orders_over_time(store):
    rows = store.query(
        "SELECT substr(date, 1, 10) AS d, SUM(subtotal_value) "
        f"FROM order_totals WHERE {_LIVE} AND date IS NOT NULL AND date != '' "
        "GROUP BY d ORDER BY d")
    return _line_chart([(r[0], r[1]) for r in rows], value_format=_num)


def _top_customers(store, limit=8):
    rows = store.query(
        "SELECT COALESCE(NULLIF(a.customer_name, ''), "
        "                NULLIF(ot.customer_name, ''), "
        "                'Unattributed') AS who, "
        "       SUM(ot.subtotal_value) AS v "
        "FROM order_totals ot "
        "LEFT JOIN activities a ON a.activity_id = ot.activity_id "
        f"WHERE ot.{_LIVE} "
        "GROUP BY who ORDER BY v DESC LIMIT ?", (limit,))
    return _bar_chart([(r[0], r[1]) for r in rows], value_format=_money)


def _top_products(store, limit=8):
    rows = store.query(
        "SELECT COALESCE(NULLIF(p.name, ''), NULLIF(oi.product_code, ''), "
        "                'Unknown') AS what, "
        "       SUM(oi.item_subtotal_value) AS v "
        "FROM order_items oi "
        "LEFT JOIN products p ON p.product_id = oi.product_id "
        f"WHERE oi.{_LIVE} "
        "GROUP BY what ORDER BY v DESC LIMIT ?", (limit,))
    return _bar_chart([(r[0], r[1]) for r in rows], value_format=_money)


def _visits_by_user(store, limit=10):
    rows = store.query(
        "SELECT COALESCE(NULLIF(a.display_name, ''), 'Unassigned') AS who, "
        "       COUNT(*) AS n "
        "FROM activity_visits v "
        "JOIN activities a ON a.activity_id = v.activity_id "
        f"WHERE v.{_LIVE} "
        "GROUP BY who ORDER BY n DESC LIMIT ?", (limit,))
    return _bar_chart([(r[0], r[1]) for r in rows], value_format=_num)


def _onsite_split(store):
    onsite = store.scalar(
        f"SELECT COUNT(*) FROM activity_visits WHERE {_LIVE} AND is_onsite=1")
    offsite = store.scalar(
        f"SELECT COUNT(*) FROM activity_visits WHERE {_LIVE} AND "
        "(is_onsite=0 OR is_onsite IS NULL)")
    scheduled = store.scalar(
        f"SELECT COUNT(*) FROM activity_visits WHERE {_LIVE} AND is_scheduled=1")
    if not (onsite or offsite):
        return _no_data()
    return _bar_chart(
        [("On-site", onsite), ("Off-site", offsite), ("Scheduled", scheduled)],
        value_format=_num)


def _targets(store, limit=12):
    """User targets against actual order value for the same month."""
    rows = store.query(
        "SELECT COALESCE(NULLIF(u.display_name, ''), ut.user_id) AS who, "
        "       ut.month AS m, ut.target AS t "
        "FROM user_targets ut "
        "LEFT JOIN users u ON u.user_id = ut.user_id "
        f"WHERE ut.{_LIVE} ORDER BY ut.month DESC, who LIMIT ?", (limit,))
    if not rows:
        return _no_data("No user targets extracted for this period.")
    out = []
    for who, month, target in rows:
        actual = store.scalar(
            "SELECT SUM(ot.subtotal_value) FROM order_totals ot "
            "LEFT JOIN activities a ON a.activity_id = ot.activity_id "
            f"WHERE ot.{_LIVE} AND substr(ot.date, 1, 7) = substr(?, 1, 7) "
            "AND COALESCE(NULLIF(a.display_name,''), "
            "             NULLIF(ot.display_name,'')) = ?",
            (month or "", who), default=0)
        target = float(target or 0)
        pct = f"{(actual / target * 100):.0f}%" if target else "-"
        out.append([who, month or "-", _money(target), _money(actual), pct])
    return _table(["User", "Month", "Target", "Actual (orders)", "Achieved"], out)


def _freshness(store):
    """When each entity was last extracted, and the server window it used."""
    runs = store.last_runs()
    counts = store.counts()
    if not runs:
        return _no_data("Nothing has been extracted yet - run an extract on the "
                        "Reporting tab first.")
    rows = []
    for entity, run in sorted(runs.items()):
        rows.append([
            entity,
            run["started_at"] or "-",
            run["reporting_period"] or "(n/a)",
            run["mode"] or "-",
            _num(run["rows"]),
            run["date_range"] or "-",
            run["status"] or "-",
        ])
    table = _table(["Entity", "Last extract", "Period", "Mode", "Rows",
                    "Server window", "Status"], rows)
    live = ", ".join(f"{t}={_num(n)}" for t, n in sorted(counts.items()) if n)
    total = f'<p class="note">Stored rows: {_esc(live or "none")}</p>'
    return table + total


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

_CSS = """
*{box-sizing:border-box}
body{margin:0;padding:32px;background:#f7f8fa;color:%(ink)s;
 font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
header{max-width:1180px;margin:0 auto 24px}
h1{margin:0 0 4px;font-size:26px;letter-spacing:-.01em}
.sub{color:%(muted)s;font-size:14px;margin:0}
main{max-width:1180px;margin:0 auto;display:grid;gap:20px}
.panel{background:#fff;border:1px solid %(grid)s;border-radius:12px;padding:20px 22px}
.panel h2{margin:0 0 14px;font-size:16px;font-weight:600}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:20px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px}
.kpi{background:#fff;border:1px solid %(grid)s;border-radius:12px;padding:16px 18px}
.kpi-label{color:%(muted)s;font-size:12px;text-transform:uppercase;
 letter-spacing:.06em;margin-bottom:6px}
.kpi-value{font-size:26px;font-weight:600;letter-spacing:-.02em}
.kpi-sub{color:%(muted)s;font-size:12px;margin-top:4px}
.chart{display:block;overflow:visible}
.chart .lbl{font-size:12px;fill:%(ink)s}
.chart .val{font-size:12px;fill:%(muted)s}
.chart .axis{font-size:11px;fill:%(muted)s}
.nodata{color:%(muted)s;font-style:italic;margin:8px 0 0;font-size:14px}
.note{color:%(muted)s;font-size:12px;margin:0 0 12px}
.tablewrap{overflow-x:auto}
table{border-collapse:collapse;width:100%%;font-size:14px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid %(grid)s;
 white-space:nowrap}
th{color:%(muted)s;font-weight:600;font-size:12px;text-transform:uppercase;
 letter-spacing:.05em}
tbody tr:last-child td{border-bottom:none}
footer{max-width:1180px;margin:24px auto 0;color:%(muted)s;font-size:12px}
@media print{body{background:#fff;padding:0}.panel,.kpi{break-inside:avoid}}
""" % {"ink": _INK, "muted": _MUTED, "grid": _GRID}


def build_dashboard(store, out_path, title="Skynamo Dashboard",
                    period_label="", generated_at=None):
    """Render the store into one self-contained HTML file. Returns the path."""
    generated = generated_at or datetime.now().strftime("%Y-%m-%d %H:%M")

    panels = [
        _panel("Overview", _kpis(store)),
        f'<div class="grid2">'
        + _panel("Order value over time", _orders_over_time(store))
        + _panel("Top customers by order value", _top_customers(store),
                 note="Attributed via the activity that created the order.")
        + '</div>',
        f'<div class="grid2">'
        + _panel("Top products by order value", _top_products(store))
        + _panel("Visits by user", _visits_by_user(store))
        + '</div>',
        f'<div class="grid2">'
        + _panel("Visit type", _onsite_split(store))
        + _panel("Targets vs actuals", _targets(store),
                 note="Actuals are order value in the target's month.")
        + '</div>',
        _panel("Data freshness", _freshness(store),
               note="What is in the local store, and the window the server "
                    "actually computed for each extract."),
    ]

    subtitle = f"{period_label} · generated {generated}" if period_label \
        else f"Generated {generated}"

    return _write(out_path, f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(title)}</title><style>{_CSS}</style></head>
<body>
<header><h1>{_esc(title)}</h1><p class="sub">{_esc(subtitle)}</p></header>
<main>{"".join(panels)}</main>
<footer>Built from the local Skynamo reporting extract. Figures reflect what
has been extracted, not necessarily the live instance - see Data freshness.</footer>
</body></html>
""")


def _write(out_path, content):
    directory = os.path.dirname(out_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(content)
    return out_path
