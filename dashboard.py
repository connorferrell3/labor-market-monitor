"""
Job market dashboard - display layer (professional restyle).

Collect -> Store -> Analyze -> DISPLAY

Reads jobs.db, computes analytics, and serves an editorial-terminal style
page: masthead, KPI tiles with threshold colors, and one framed "exhibit"
per metric on a shared monthly timeline. Light/dark theme toggle.

Still decoupled: this file knows only the standard row (date, source,
metric, value) plus display config in PANELS below. Nothing about BLS/Adzuna.

Usage:
    python dashboard.py
Open the printed URL (usually http://localhost:8000). Ctrl+C to stop.
"""

import http.server
import json
import socketserver
import sqlite3
import webbrowser
import datetime as dt

import analytics

DB_PATH = "jobs.db"
PORT = 8000

# Each panel carries display config AND honest threshold bounds used to color
# its KPI tile. Thresholds mirror the reference dashboard's published bounds
# where they exist (sentiment, CPI); the two job-count series have no standard
# "healthy/stress" bounds, so their tiles stay neutral (policy-style) rather
# than inventing thresholds. "higher_is_better" flips the color logic.
#
# To add a metric later: add one dict here. Nothing else changes.
PANELS = [
    {
        "source": "BLS_JOLTS", "metric": "total_openings",
        "title": "Job openings", "kpi_label": "JOLTS OPENINGS",
        "subtitle": "Total nonfarm job openings, monthly.",
        "exhibit": "01", "src_tag": "BLS JOLTS",
        "source_line": "Source: BLS Job Openings & Labor Turnover Survey "
                       "(series JTS000000000000000JOL, seasonally adjusted).",
        "color": "#336BCC", "unit": "count",
        "thresholds": None,   # no standard bound -> neutral tile
        "tab": "Jobs",
    },
    {
        "source": "ADZUNA", "metric": "entry_level_index",
        "title": "Entry-level postings", "kpi_label": "ENTRY-LEVEL",
        "subtitle": "Keyword-matched posting proxy. Trend matters, not the "
                    "absolute level. Collecting since Aug 2026.",
        "exhibit": "02", "src_tag": "ADZUNA",
        "source_line": "Source: Adzuna US search API, keyword-matched entry-level "
                       "terms. Proxy index (terms overlap); read the trend.",
        "color": "#7A5CD0", "unit": "count",
        "thresholds": None,
        "tab": "Jobs",
    },
    {
        "source": "CPI", "metric": "inflation_yoy_pct",
        "title": "Inflation (CPI, YoY)", "kpi_label": "CPI YoY",
        "subtitle": "% change vs the same month a year earlier.",
        "exhibit": "03", "src_tag": "BLS CPI-U",
        "source_line": "Source: BLS CPI-U, all items, U.S. city average "
                       "(series CUUR0000SA0). Fed target ~2%.",
        "color": "#C0632D", "unit": "percent",
        # reference bounds: <2.5 healthy, 2.5-3.5 watch, >=3.5 stress
        "thresholds": {"healthy_max": 2.5, "watch_max": 3.5,
                       "higher_is_better": False},
        "tab": "Inflation",
    },
    {
        "source": "UNEMPLOYMENT", "metric": "u3_rate_pct",
        "title": "Unemployment rate (U-3)", "kpi_label": "UNEMPLOYMENT",
        "subtitle": "Headline unemployment rate, 16+, seasonally adjusted.",
        "exhibit": "04", "src_tag": "BLS CPS",
        "source_line": "Source: BLS Current Population Survey, U-3 unemployment "
                       "rate (series LNS14000000, seasonally adjusted).",
        "color": "#B23B1E", "unit": "percent",
        # reference bounds: <4.0 healthy, 4.0-4.9 watch, >=5.0 stress
        "thresholds": {"healthy_max": 4.0, "watch_max": 4.9,
                       "higher_is_better": False},
        "tab": "Jobs",
    },
    {
        "source": "EARNINGS", "metric": "earnings_yoy_pct",
        "title": "Wage growth (YoY)", "kpi_label": "WAGE GROWTH",
        "subtitle": "Year-over-year change in average hourly earnings. Compare "
                    "against CPI above \u2014 higher than inflation means real gains.",
        "exhibit": "06", "src_tag": "BLS CES",
        "source_line": "Source: BLS CES avg hourly earnings YoY (CES0500000003). "
                       "Read alongside CPI: wage growth minus inflation = real wage change.",
        "color": "#2E8B6F", "unit": "percent",
        # reference "goldilocks" band: 3.0-4.5 healthy, outside = watch,
        # <2 or >6 = stress. This one is a RANGE, handled specially below.
        "thresholds": {"band_low": 3.0, "band_high": 4.5,
                       "stress_low": 2.0, "stress_high": 6.0, "is_band": True},
        "tab": "Wages",
    },
    {
        # DERIVED series - no source table; computed as wage growth minus CPI.
        "source": "REAL_WAGE", "metric": "real_wage_pct",
        "title": "Real wage growth", "kpi_label": "REAL WAGES",
        "subtitle": "Wage growth minus inflation. Positive = pay outpacing "
                    "prices (buying power rising); negative = falling behind.",
        "exhibit": "07", "src_tag": "BLS CES \u2212 CPI",
        "source_line": "Derived: BLS avg hourly earnings YoY minus BLS CPI-U YoY. "
                       "The zero line is the break-even between gaining and losing ground.",
        "color": "#1D9E75", "unit": "percent",
        # positive is healthy, near-zero is watch, negative is stress
        "thresholds": {"healthy_min": 0.5, "watch_min": 0.0,
                       "higher_is_better": True},
        "tab": "Wages",
    },
    {
        "source": "SENTIMENT", "metric": "umich_ics",
        "title": "Consumer sentiment", "kpi_label": "UMICH SENTIMENT",
        "subtitle": "U. Michigan Index of Consumer Sentiment. Higher = more "
                    "optimistic. Shown from 2015.",
        "exhibit": "07", "src_tag": "U. MICHIGAN",
        "source_line": "Source: University of Michigan Survey of Consumers, "
                       "Index of Consumer Sentiment. Long-run avg ~85.",
        "color": "#7A5CD0", "unit": "index",
        # reference bounds: >=70 healthy, 55-70 watch, <55 stress
        "thresholds": {"healthy_min": 70, "watch_min": 55,
                       "higher_is_better": True},
        "tab": "Sentiment",
    },
]


def classify(value, thresholds):
    """Return 'healthy' | 'watch' | 'stress' | 'neutral' for a KPI tile."""
    if thresholds is None or value is None:
        return "neutral"
    # A "band" metric is healthy inside a range, stress at the extremes,
    # watch in between. Wage growth works this way: too low = stagnation,
    # too high = overheating.
    if thresholds.get("is_band"):
        if value < thresholds["stress_low"] or value > thresholds["stress_high"]:
            return "stress"
        if thresholds["band_low"] <= value <= thresholds["band_high"]:
            return "healthy"
        return "watch"
    if thresholds.get("higher_is_better"):
        if value >= thresholds["healthy_min"]:
            return "healthy"
        if value >= thresholds["watch_min"]:
            return "watch"
        return "stress"
    else:
        if value <= thresholds["healthy_max"]:
            return "healthy"
        if value <= thresholds["watch_max"]:
            return "watch"
        return "stress"


def build_sector_table(conn):
    """
    Build the JOLTS-by-sector table: for each sector, the openings level at the
    end of each year in the data, plus a trend arrow from first year to last.

    Returns a dict with 'years' (column headers) and 'rows' (one per sector).
    We take each year's LATEST available month as that year's figure, so the
    current (partial) year is represented by its most recent print.
    """
    # Pull every sector metric present.
    metrics = [r[0] for r in conn.execute(
        "SELECT DISTINCT metric FROM series WHERE source = 'JOLTS_SECTOR' "
        "ORDER BY metric"
    ).fetchall()]
    if not metrics:
        return None

    # Collect all (year -> latest value) per sector.
    sector_year = {}          # {sector_name: {year: value}}
    all_years = set()
    for m in metrics:
        name = m.replace("openings_", "")
        rows = conn.execute(
            "SELECT date, value FROM series "
            "WHERE source = 'JOLTS_SECTOR' AND metric = ? ORDER BY date ASC",
            (m,),
        ).fetchall()
        yearly = {}
        for date, value in rows:
            yearly[date[:4]] = value      # later months overwrite -> latest wins
            all_years.add(date[:4])
        sector_year[name] = yearly

    years = sorted(all_years)
    table_rows = []
    for name, yearly in sector_year.items():
        values = [yearly.get(y) for y in years]
        # Trend: compare first and last available values for this sector.
        present = [(y, yearly[y]) for y in years if y in yearly]
        trend = "flat"
        pct = None
        if len(present) >= 2:
            first_v = present[0][1]
            last_v = present[-1][1]
            if first_v:
                pct = (last_v - first_v) / first_v * 100
                if pct > 5:
                    trend = "up"
                elif pct < -5:
                    trend = "down"
        table_rows.append({
            "sector": name,
            "values": values,
            "trend": trend,
            "trend_pct": round(pct, 1) if pct is not None else None,
        })

    # Sort by latest value, biggest sector first.
    table_rows.sort(key=lambda r: (r["values"][-1] or 0), reverse=True)
    return {"years": years, "rows": table_rows}


def build_sector_netchange(conn):
    """
    Build the CES year-over-year net employment change per sector, for a bar
    chart. For each sector: (this year's latest level) - (same month last year).

    This is the "+687K / -113K" net-adds figure. We compare the latest month to
    the SAME month 12 months earlier so seasonality cancels out (comparing, say,
    June to June rather than June to December).

    Returns {'period': 'Jun 2025 -> Jun 2026', 'rows': [{sector, change}, ...]}
    sorted most-positive first, or None if no CES data.
    """
    metrics = [r[0] for r in conn.execute(
        "SELECT DISTINCT metric FROM series WHERE source = 'CES_SECTOR' "
        "ORDER BY metric"
    ).fetchall()]
    if not metrics:
        return None

    rows_out = []
    latest_label = prior_label = None
    for m in metrics:
        name = m.replace("employment_", "")
        pts = conn.execute(
            "SELECT date, value FROM series "
            "WHERE source = 'CES_SECTOR' AND metric = ? ORDER BY date ASC",
            (m,),
        ).fetchall()
        if len(pts) < 13:          # need 12+ months apart to compare YoY
            continue
        latest_date, latest_val = pts[-1]
        # find the point ~12 months before the latest (same month prior year)
        target_year = str(int(latest_date[:4]) - 1)
        target = target_year + latest_date[4:]   # same MM-DD, prior year
        prior_val = None
        for d, v in pts:
            if d == target:
                prior_val = v
                break
        if prior_val is None:
            continue
        rows_out.append({"sector": name,
                         "change": round(latest_val - prior_val)})
        latest_label = latest_date[:7]
        prior_label = target[:7]

    if not rows_out:
        return None
    rows_out.sort(key=lambda r: r["change"], reverse=True)
    return {"period": f"{prior_label} \u2192 {latest_label}", "rows": rows_out}


def build_unemployment_views(conn):
    """
    Build everything the Unemployment tab needs:
      - yearly_avg: [(year, avg_rate)]           annual mean of U-3
      - last12:     [(month, rate)]              trailing 12 months of U-3
      - sector_latest: [{sector, rate}]          latest month, per sector
      - sector_yoy:    [{sector, change}]        same-month YoY change, per sector
      - sector_table:  {years, rows}             per-sector across years + trend
    Returns None if there's no unemployment data at all.
    """
    u3 = conn.execute(
        "SELECT date, value FROM series "
        "WHERE source='UNEMPLOYMENT' AND metric='u3_rate_pct' ORDER BY date ASC"
    ).fetchall()
    if not u3:
        return None

    # --- yearly average of U-3 ---
    from collections import defaultdict
    by_year = defaultdict(list)
    for date, val in u3:
        by_year[date[:4]].append(val)
    yearly_avg = [{"year": y, "rate": round(sum(v) / len(v), 2)}
                  for y, v in sorted(by_year.items())]

    # --- trailing 12 months ---
    last12 = [{"month": d[:7], "rate": v} for d, v in u3[-12:]]

    # --- sector views ---
    metrics = [r[0] for r in conn.execute(
        "SELECT DISTINCT metric FROM series WHERE source='UNEMP_SECTOR' ORDER BY metric"
    ).fetchall()]

    sector_latest, sector_yoy = [], []
    sector_year = {}
    all_years = set()
    for m in metrics:
        name = m.replace("unemp_", "")
        pts = conn.execute(
            "SELECT date, value FROM series "
            "WHERE source='UNEMP_SECTOR' AND metric=? ORDER BY date ASC", (m,)
        ).fetchall()
        if not pts:
            continue
        latest_date, latest_val = pts[-1]
        sector_latest.append({"sector": name, "rate": latest_val})

        # YoY: same month one year earlier
        target = str(int(latest_date[:4]) - 1) + latest_date[4:]
        prior = next((v for d, v in pts if d == target), None)
        if prior is not None:
            sector_yoy.append({"sector": name,
                               "change": round(latest_val - prior, 2)})

        # table: latest value per year
        yearly = {}
        for d, v in pts:
            yearly[d[:4]] = v
            all_years.add(d[:4])
        sector_year[name] = yearly

    sector_latest.sort(key=lambda r: r["rate"], reverse=True)
    sector_yoy.sort(key=lambda r: r["change"], reverse=True)

    # sector table across years with first-vs-last trend
    years = sorted(all_years)
    table_rows = []
    for name, yearly in sector_year.items():
        values = [yearly.get(y) for y in years]
        present = [(y, yearly[y]) for y in years if y in yearly]
        trend, pct = "flat", None
        if len(present) >= 2 and present[0][1]:
            pct = (present[-1][1] - present[0][1]) / present[0][1] * 100
            trend = "up" if pct > 5 else "down" if pct < -5 else "flat"
        table_rows.append({"sector": name, "values": values,
                           "trend": trend, "trend_pct": round(pct, 1) if pct is not None else None})
    table_rows.sort(key=lambda r: (r["values"][-1] or 0), reverse=True)

    return {
        "yearly_avg": yearly_avg,
        "last12": last12,
        "sector_latest": sector_latest,
        "sector_yoy": sector_yoy,
        "sector_table": {"years": years, "rows": table_rows} if table_rows else None,
    }


def build_wage_views(conn):
    """
    Wages tab extras:
      - nominal_vs_infl: aligned monthly [{month, wage, cpi}] for a two-line chart
      - sector_table: per-sector wage growth (YoY) across years + trend
    Wage growth per sector is derived from CES earnings dollar levels: this
    month's $ vs the same month a year earlier.
    """
    # --- nominal wage growth vs inflation, aligned by month ---
    wage = dict(conn.execute(
        "SELECT date, value FROM series WHERE source='EARNINGS' "
        "AND metric='earnings_yoy_pct' ORDER BY date ASC").fetchall())
    cpi = dict(conn.execute(
        "SELECT date, value FROM series WHERE source='CPI' "
        "AND metric='inflation_yoy_pct' ORDER BY date ASC").fetchall())
    nominal_vs_infl = [{"month": d[:7], "wage": wage[d], "cpi": cpi[d]}
                       for d in sorted(wage) if d in cpi]

    # --- sector wage growth (YoY) from CES earnings levels ---
    metrics = [r[0] for r in conn.execute(
        "SELECT DISTINCT metric FROM series WHERE source='WAGE_SECTOR' ORDER BY metric"
    ).fetchall()]

    sector_year = {}      # {sector: {year: yoy_growth}}
    all_years = set()
    for m in metrics:
        name = m.replace("wage_usd_", "")
        pts = conn.execute(
            "SELECT date, value FROM series WHERE source='WAGE_SECTOR' "
            "AND metric=? ORDER BY date ASC", (m,)).fetchall()
        usd = {d: v for d, v in pts}
        yearly = {}
        for d, v in pts:
            year = d[:4]
            prior = str(int(year) - 1) + d[4:]
            if prior in usd and usd[prior]:
                yearly[year] = round((v / usd[prior] - 1) * 100, 2)  # YoY %
                all_years.add(year)
        if yearly:
            sector_year[name] = yearly

    years = sorted(all_years)
    rows = []
    for name, yearly in sector_year.items():
        values = [yearly.get(y) for y in years]
        present = [yearly[y] for y in years if y in yearly]
        trend, pct = "flat", None
        if len(present) >= 2:
            # for wage GROWTH, "trend" = is growth accelerating or cooling?
            diff = present[-1] - present[0]
            pct = round(diff, 1)
            trend = "up" if diff > 0.3 else "down" if diff < -0.3 else "flat"
        rows.append({"sector": name, "values": values, "trend": trend, "trend_pct": pct})
    rows.sort(key=lambda r: (r["values"][-1] if r["values"][-1] is not None else -99), reverse=True)

    return {
        "nominal_vs_infl": nominal_vs_infl,
        "sector_table": {"years": years, "rows": rows} if rows else None,
    }


def load_series():
    """Read all rows, attach analytics + KPI status, return for the page."""
    conn = sqlite3.connect(DB_PATH)
    out = []
    latest_overall = None
    for p in PANELS:
        # The real-wage panel is DERIVED, not stored - compute it specially.
        if p["source"] == "REAL_WAGE":
            pts = analytics.real_wage_series(conn)
            stats = analytics.real_wage_summary(conn)
        else:
            rows = conn.execute(
                "SELECT date, value FROM series "
                "WHERE source = ? AND metric = ? ORDER BY date ASC",
                (p["source"], p["metric"]),
            ).fetchall()
            pts = rows
            stats = analytics.summary(conn, p["source"], p["metric"])
        status = classify(stats["latest_value"], p["thresholds"])
        if stats["latest_date"]:
            if latest_overall is None or stats["latest_date"] > latest_overall:
                latest_overall = stats["latest_date"]
        out.append({
            **{k: v for k, v in p.items() if k != "thresholds"},
            "points": [{"date": d, "value": v} for (d, v) in pts],
            "stats": stats,
            "status": status,
        })
    sector_table = build_sector_table(conn)
    sector_netchange = build_sector_netchange(conn)

    # Per-source freshness: the latest date we have for each source. Sources
    # update on different cadences (JOLTS lags ~5wks, sentiment is current),
    # so showing each one's actual "as of" date is honest.
    freshness = {}
    for src, in conn.execute("SELECT DISTINCT source FROM series"):
        d = conn.execute(
            "SELECT MAX(date) FROM series WHERE source = ?", (src,)
        ).fetchone()[0]
        if d:
            freshness[src] = d[:7]

    conn.close()
    return {"panels": out, "as_of": latest_overall,
            "sector_table": sector_table,
            "sector_netchange": sector_netchange,
            "freshness": freshness,
            "unemployment_views": build_unemployment_views(sqlite3.connect(DB_PATH)),
            "wage_views": build_wage_views(sqlite3.connect(DB_PATH))}


PAGE = r"""<!DOCTYPE html>
<html lang="en" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>U.S. Labor Market Monitor</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  :root {
    --paper:#F7F5F0; --ink:#1A1A17; --muted:#6F6D64; --hair:#E0DDD3;
    --card:#FFFFFF; --grid:#EEEBE2; --accent:#336BCC;
    --healthy:#1A7A4F; --watch:#B9822A; --stress:#B23B1E;
    --healthy-bg:#EAF6EF; --watch-bg:#FBF3E3; --stress-bg:#FBEDE8;
    --tile:#FBFAF7;
  }
  html[data-theme="dark"] {
    --paper:#141410; --ink:#E9E7DE; --muted:#9A978C; --hair:#2C2B24;
    --card:#1C1B16; --grid:#26251E; --accent:#6C97E8;
    --healthy:#5FC08C; --watch:#E0B25C; --stress:#E17A5E;
    --healthy-bg:#16241C; --watch-bg:#28211320; --stress-bg:#2A1913;
    --tile:#211F19;
  }
  * { box-sizing:border-box; }
  body {
    margin:0; background:var(--paper); color:var(--ink);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
    line-height:1.5; transition:background .2s,color .2s;
  }
  .mono {
    font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
    font-variant-numeric:tabular-nums;
  }
  /* Masthead */
  header { border-bottom:2px solid var(--ink); background:var(--card); }
  .mast {
    max-width:1040px; margin:0 auto; padding:14px 32px;
    display:flex; align-items:baseline; justify-content:space-between; gap:16px;
  }
  .eyebrow {
    font-size:11px; letter-spacing:.14em; text-transform:uppercase;
    color:var(--muted);
  }
  h1 {
    font-family:Georgia,"Times New Roman",serif; font-weight:600;
    font-size:30px; letter-spacing:-.01em; margin:2px 0 4px;
  }
  .sources { font-size:11px; letter-spacing:.06em; color:var(--muted); }
  /* Documentation panel */
  .guide { max-width:1040px; margin:0 auto; padding:0 32px; }
  .guide details {
    border:1px solid var(--hair); border-radius:8px; background:var(--tile);
    margin-top:14px;
  }
  .guide summary {
    cursor:pointer; padding:12px 16px; font-size:13px; font-weight:600;
    list-style:none; display:flex; align-items:center; gap:8px;
  }
  .guide summary::-webkit-details-marker { display:none; }
  .guide summary::before { content:"\25B8"; color:var(--muted); font-size:11px; }
  .guide details[open] summary::before { content:"\25BE"; }
  .guide .body { padding:4px 16px 16px; font-size:13px; line-height:1.55; }
  .guide h3 {
    font-size:11px; letter-spacing:.08em; text-transform:uppercase;
    color:var(--muted); margin:16px 0 6px;
  }
  .guide table { width:100%; border-collapse:collapse; font-size:12px; margin-top:4px; }
  .guide th, .guide td {
    text-align:left; padding:6px 8px; border-bottom:1px solid var(--hair);
    vertical-align:top;
  }
  .guide th { color:var(--muted); font-size:10px; letter-spacing:.06em;
    text-transform:uppercase; }
  .guide td.g { color:var(--healthy); white-space:nowrap; }
  .guide td.a { color:var(--watch); white-space:nowrap; }
  .guide td.r { color:var(--stress); white-space:nowrap; }
  .guide .fresh { display:flex; flex-wrap:wrap; gap:8px 20px; margin-top:4px; }
  .guide .fresh span { font-family:ui-monospace,Menlo,monospace; font-size:12px; }
  .guide .fresh b { color:var(--muted); font-weight:500; }
  .themebtn {
    border:1px solid var(--hair); background:var(--tile); color:var(--ink);
    border-radius:6px; padding:6px 12px; font-size:12px; cursor:pointer;
    white-space:nowrap;
  }
  .themebtn:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
  /* Tab bar */
  .tabbar {
    max-width:1040px; margin:0 auto; padding:0 32px;
    display:flex; gap:2px; border-bottom:1px solid var(--hair);
  }
  .tab {
    font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
    font-size:12px; letter-spacing:.06em; text-transform:uppercase;
    padding:12px 16px; cursor:pointer; color:var(--muted);
    background:none; border:none; border-bottom:2px solid transparent;
    margin-bottom:-1px;
  }
  .tab:hover { color:var(--ink); }
  .tab.active { color:var(--ink); border-bottom-color:var(--accent); font-weight:600; }
  .tab:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
  /* KPI strip */
  .kpis {
    max-width:1040px; margin:0 auto; padding:20px 32px 4px;
    display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:12px;
  }
  .tile {
    background:var(--tile); border:1px solid var(--hair);
    border-left:3px solid var(--muted); border-radius:8px; padding:12px 14px;
  }
  .tile.healthy { border-left-color:var(--healthy); }
  .tile.watch   { border-left-color:var(--watch); }
  .tile.stress  { border-left-color:var(--stress); }
  .tile .lab { font-size:10px; letter-spacing:.1em; color:var(--muted); text-transform:uppercase; }
  .tile .val { font-size:22px; font-weight:600; margin-top:3px; }
  .tile .dot { font-size:11px; margin-top:2px; }
  .tile.healthy .dot { color:var(--healthy); }
  .tile.watch   .dot { color:var(--watch); }
  .tile.stress  .dot { color:var(--stress); }
  .tile.neutral .dot { color:var(--muted); }
  /* Exhibits */
  .wrap { max-width:1040px; margin:0 auto; padding:16px 32px 64px; }
  .tabread {
    background:var(--tile); border:1px solid var(--hair); border-radius:8px;
    padding:14px 16px; margin:8px 0 4px; font-size:14px; line-height:1.55;
  }
  .tabread .mono {
    display:inline-block; font-size:10px; letter-spacing:.1em; color:var(--accent);
    margin-right:8px;
  }
  .exhibit { border-top:1px solid var(--hair); padding:22px 0 8px; }
  .extag {
    font-size:11px; letter-spacing:.08em; color:var(--muted);
    display:flex; gap:8px; align-items:center; margin-bottom:6px;
  }
  .extag b { color:var(--ink); font-weight:600; }
  .exhead { display:flex; justify-content:space-between; align-items:baseline; gap:16px; }
  .exhead h2 { font-size:19px; font-weight:600; margin:0; letter-spacing:-.01em; }
  .latest { text-align:right; white-space:nowrap; }
  .latest .n { font-size:22px; font-weight:600; }
  .latest .l { font-size:11px; color:var(--muted); }
  .sub { color:var(--muted); font-size:13px; margin:4px 0 10px; max-width:640px; }
  .badges { display:flex; gap:8px; margin-bottom:12px; flex-wrap:wrap; }
  .badge {
    font-size:12px; font-weight:600; padding:3px 9px; border-radius:999px;
    border:1px solid var(--hair); background:var(--tile);
  }
  .badge .k { color:var(--muted); font-weight:500; margin-right:5px;
    font-size:10px; letter-spacing:.08em; }
  .badge.up { color:var(--healthy); background:var(--healthy-bg); }
  .badge.down { color:var(--stress); background:var(--stress-bg); }
  .badge.flat { color:var(--muted); }
  .chartbox { position:relative; height:200px; }
  .srcline { font-size:11px; color:var(--muted); margin:10px 0 0; padding-top:8px; }
  .graphread {
    background:var(--tile); border-left:3px solid var(--accent);
    border-radius:0 6px 6px 0; padding:10px 14px; margin:12px 0 2px;
    font-size:13px; line-height:1.5;
  }
  .graphread .lbl {
    font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
    font-size:10px; letter-spacing:.1em; color:var(--accent);
    display:block; margin-bottom:3px;
  }
  .empty { color:var(--muted); font-size:13px; padding:16px 0; }
  /* Sector table */
  .sectorwrap { border-top:1px solid var(--hair); padding:22px 0 8px; }
  .sectortbl { width:100%; border-collapse:collapse; font-size:13px; }
  .sectortbl th, .sectortbl td {
    text-align:right; padding:8px 10px; border-bottom:1px solid var(--hair);
    font-variant-numeric:tabular-nums;
  }
  .sectortbl th { color:var(--muted); font-size:11px; letter-spacing:.06em;
    text-transform:uppercase; font-weight:600; }
  .sectortbl td.name, .sectortbl th.name { text-align:left; }
  .sectortbl tr:hover td { background:var(--tile); }
  .trend { font-weight:600; }
  .trend.up { color:var(--healthy); }
  .trend.down { color:var(--stress); }
  .trend.flat { color:var(--muted); }
  footer {
    max-width:1040px; margin:0 auto; padding:20px 32px 40px;
    border-top:1px solid var(--hair); color:var(--muted); font-size:11px;
  }
  @media (max-width:720px) {
    .mast { flex-wrap:wrap; }
  }
  @media (prefers-reduced-motion:reduce) { * { transition:none !important; } }
</style>
</head>
<body>
<header>
  <div class="mast">
    <div>
      <div class="eyebrow mono">Labor/26 &middot; Monthly monitor</div>
      <h1>U.S. Labor Market Monitor</h1>
      <div class="sources mono">BLS &middot; ADZUNA &middot; U. MICHIGAN &mdash; <span id="asof">updated monthly</span></div>
    </div>
    <button class="themebtn mono" id="themebtn" aria-label="Toggle theme">&#9789; Dark</button>
  </div>
</header>
<nav class="tabbar" id="tabbar"></nav>
<div class="guide"><details id="guide"></details></div>
<div class="kpis" id="kpis"></div>
<div class="wrap" id="wrap"></div>
<footer class="mono">
  Openings, entry-level demand, inflation, and sentiment on one timeline.
  Reads live from jobs.db &mdash; re-run collect.py, then refresh to update.
  KPI tile colors follow published reference thresholds where they exist;
  job-count series carry no standard bounds and stay neutral.
</footer>
<script>
const UNIT_FMT = {
  count:   v => v.toLocaleString(),
  percent: v => v.toFixed(2) + "%",
  index:   v => v.toFixed(1),
  dollars: v => "$" + v.toFixed(2),
};
const STATUS_WORD = { healthy:"Healthy", watch:"Watch", stress:"Stress", neutral:"\u2014" };

function badge(label, pct) {
  if (pct === null || pct === undefined)
    return `<span class="badge flat"><span class="k mono">${label}</span>n/a</span>`;
  const cls = pct > 0.05 ? "up" : pct < -0.05 ? "down" : "flat";
  const arrow = pct > 0.05 ? "\u2191" : pct < -0.05 ? "\u2193" : "\u2192";
  const val = (pct >= 0 ? "+" : "") + pct.toFixed(2) + "%";
  return `<span class="badge ${cls}"><span class="k mono">${label}</span>${arrow} ${val}</span>`;
}

// Theme toggle
const root = document.documentElement;
const tbtn = document.getElementById("themebtn");
function setTheme(t) {
  root.setAttribute("data-theme", t);
  tbtn.innerHTML = t === "dark" ? "\u2600 Light" : "\u263D Dark";
}
tbtn.addEventListener("click", () =>
  setTheme(root.getAttribute("data-theme") === "dark" ? "light" : "dark"));

function cssVar(name) {
  return getComputedStyle(root).getPropertyValue(name).trim();
}

const CHART_OK = typeof Chart !== "undefined";
let ALL = [];          // all panels from the server
let ACTIVE = null;     // active tab name
let SECTOR_TABLE = null; // JOLTS-by-sector table payload
let SECTOR_NETCHANGE = null; // CES YoY net-change payload
let UNEMP_VIEWS = null;  // unemployment tab views
let WAGE_VIEWS = null;   // wages tab views
const charts = [];     // track Chart instances so we can destroy on re-render

// A tiny honest "read" per tab, generated from the real numbers - no invented
// prose. Mirrors the reference dashboard's commentary, but only says what the
// data supports. Returns "" if there's nothing solid to say.
function tabRead(panels) {
  const by = {};
  panels.forEach(p => { by[p.source + ":" + p.metric] = p; });
  const bits = [];
  const unemp = by["UNEMPLOYMENT:u3_rate_pct"];
  if (unemp && unemp.stats.latest_value !== null) {
    const w = { healthy:"in a healthy range", watch:"in the watch zone",
                stress:"at stress levels" }[unemp.status] || "";
    bits.push(`Unemployment is ${unemp.stats.latest_value.toFixed(1)}% (${w})`);
  }
  const wage = by["EARNINGS:earnings_yoy_pct"];
  const cpi = by["CPI:inflation_yoy_pct"];   // may be on another tab, still in ALL
  if (wage && wage.stats.latest_value !== null) {
    let s = `wage growth is ${wage.stats.latest_value.toFixed(1)}%`;
    const c = ALL.find(p => p.source==="CPI" && p.metric==="inflation_yoy_pct");
    if (c && c.stats.latest_value !== null) {
      const real = wage.stats.latest_value - c.stats.latest_value;
      s += real >= 0
        ? `, just above inflation (${c.stats.latest_value.toFixed(1)}%) \u2014 a small real gain`
        : `, below inflation (${c.stats.latest_value.toFixed(1)}%) \u2014 real pay is slipping`;
    }
    bits.push(s);
  }
  if (!bits.length) return "";
  return bits.join(". ") + ".";
}

// Per-graph analytical read: describes THIS graph's own movement (direction +
// magnitude over its recent history) and adds an economic reference point where
// one honestly exists (Fed target, break-even, historical norm). Computed live
// from the data, so it updates when the data does. Returns "" if too little data.
function graphRead(s) {
  const pts = s.points;
  if (!pts || pts.length < 4) return "";
  const latest = pts[pts.length - 1].value;
  const fmt = UNIT_FMT[s.unit] || (v => v);

  // Recent direction: change over the last ~6 months (or whatever we have).
  const lookback = Math.min(6, pts.length - 1);
  const past = pts[pts.length - 1 - lookback].value;
  const delta = latest - past;
  const absd = Math.abs(delta);
  const rising = delta > 0;

  // Magnitude word, scaled per unit so "big" means something sensible.
  function movementPhrase() {
    if (s.unit === "percent") {
      if (absd < 0.2) return "has held roughly steady";
      return `has ${rising ? "risen" : "fallen"} ${absd.toFixed(1)}pp`;
    }
    if (s.unit === "index") {
      if (absd < 1) return "has held roughly steady";
      return `has ${rising ? "risen" : "fallen"} ${absd.toFixed(0)} points`;
    }
    if (s.unit === "dollars") {
      if (absd < 0.10) return "has held roughly steady";
      return `has ${rising ? "risen" : "fallen"} $${absd.toFixed(2)}`;
    }
    // count
    const pct = past ? (delta / past * 100) : 0;
    if (Math.abs(pct) < 1) return "has held roughly steady";
    return `has ${rising ? "risen" : "fallen"} ${Math.abs(pct).toFixed(0)}%`;
  }

  const months = lookback;
  const cleanName = {
    "CPI:inflation_yoy_pct": "inflation",
    "UNEMPLOYMENT:u3_rate_pct": "the unemployment rate",
    "EARNINGS:earnings_usd": "average hourly pay",
    "EARNINGS:earnings_yoy_pct": "wage growth",
    "REAL_WAGE:real_wage_pct": "real wage growth",
    "SENTIMENT:umich_ics": "consumer sentiment",
    "BLS_JOLTS:total_openings": "job openings",
    "ADZUNA:entry_level_index": "the entry-level index",
  }[s.source + ":" + s.metric] || s.title.toLowerCase();
  let read = `Over the last ${months} months, ${cleanName} `
    + `${movementPhrase()}, now at ${fmt(latest)}.`;

  // Economic interpretation, per metric - only where a real reference exists.
  let interp = "";
  const key = s.source + ":" + s.metric;
  if (key === "CPI:inflation_yoy_pct") {
    const gap = latest - 2.0;
    interp = gap > 0.3
      ? ` That's about ${gap.toFixed(1)}pp above the Fed's 2% target, so price pressure remains elevated.`
      : Math.abs(gap) <= 0.3
        ? ` That's essentially at the Fed's 2% target.`
        : ` That's below the Fed's 2% target.`;
  } else if (key === "UNEMPLOYMENT:u3_rate_pct") {
    interp = latest < 4.0
      ? ` Below 4% is historically considered near full employment.`
      : latest < 5.0
        ? ` Between 4-5% is moderate by historical standards, but the direction matters more than the level.`
        : ` Above 5% signals meaningful labor-market slack.`;
  } else if (key === "EARNINGS:earnings_yoy_pct") {
    interp = ` Compare this against inflation: wage growth above CPI means workers gain real ground, below means they lose it.`;
  } else if (key === "REAL_WAGE:real_wage_pct") {
    interp = latest >= 0
      ? ` Positive means paychecks are outpacing prices \u2014 real buying power is rising.`
      : ` Negative means prices are outrunning pay \u2014 real buying power is eroding despite nominal raises.`;
  } else if (key === "SENTIMENT:umich_ics") {
    interp = latest < 70
      ? ` Readings below the long-run average (~85) indicate consumers remain downbeat about the economy.`
      : ` Readings near or above the long-run average (~85) indicate relatively confident consumers.`;
  } else if (key === "BLS_JOLTS:total_openings") {
    interp = ` Openings well above pre-2020 norms (~7M) indicate labor demand is still firm; a sustained fall would signal cooling.`;
  } else if (key === "ADZUNA:entry_level_index") {
    interp = ` This is a proxy index, so read its direction rather than its level.`;
  }

  return read + interp;
}

function renderUnemployment() {
  const v = UNEMP_VIEWS;
  const kpis = document.getElementById("kpis");
  const wrap = document.getElementById("wrap");
  kpis.innerHTML = "";
  wrap.innerHTML = "";

  // KPI strip: latest U-3, its yearly avg, highest/lowest sector.
  const latestRate = v.last12.length ? v.last12[v.last12.length - 1].rate : null;
  const thisYearAvg = v.yearly_avg.length ? v.yearly_avg[v.yearly_avg.length - 1] : null;
  const hi = v.sector_latest[0], lo = v.sector_latest[v.sector_latest.length - 1];
  function tile(label, val, cls) {
    return `<div class="tile ${cls||'neutral'}"><div class="lab mono">${label}</div>`
      + `<div class="val mono">${val}</div></div>`;
  }
  kpis.innerHTML =
    tile("U-3 RATE (LATEST)", latestRate !== null ? latestRate.toFixed(1)+"%" : "\u2014",
         latestRate === null ? "neutral" : latestRate < 4 ? "healthy" : latestRate < 5 ? "watch" : "stress")
    + tile(`${thisYearAvg ? thisYearAvg.year : ""} AVG`, thisYearAvg ? thisYearAvg.rate.toFixed(1)+"%" : "\u2014")
    + (hi ? tile("HIGHEST SECTOR", hi.rate.toFixed(1)+"%") : "")
    + (lo ? tile("LOWEST SECTOR", lo.rate.toFixed(1)+"%") : "");

  const muted = cssVar("--muted"), grid = cssVar("--grid"),
        accent = cssVar("--accent"), healthy = cssVar("--healthy"),
        stress = cssVar("--stress");

  // Helper to add one exhibit with a canvas and return the canvas.
  function exhibit(tag, title, sub, srcline, heightPx) {
    const ex = document.createElement("div");
    ex.className = "exhibit";
    ex.innerHTML = `<div class="extag mono">${tag}</div>`
      + `<div class="exhead"><h2>${title}</h2></div>`
      + `<div class="sub">${sub}</div>`;
    const box = document.createElement("div");
    box.className = "chartbox";
    box.style.height = (heightPx || 240) + "px";
    const canvas = document.createElement("canvas");
    box.appendChild(canvas);
    ex.appendChild(box);
    if (srcline) {
      const p = document.createElement("p");
      p.className = "srcline mono"; p.textContent = srcline;
      ex.appendChild(p);
    }
    wrap.appendChild(ex);
    return CHART_OK ? canvas : (box.innerHTML =
      `<div class="empty">Chart library unavailable \u2014 refresh with a connection.</div>`, null);
  }

  // 1) Yearly average line with points.
  let c = exhibit("Exhibit U1 | BLS CPS", "Average unemployment rate by year",
    "Annual mean of the monthly U-3 rate. Each point is one year's average.",
    "Source: BLS CPS, U-3 headline rate (LNS14000000), averaged by calendar year.");
  if (c) charts.push(new Chart(c, {
    type: "line",
    data: { labels: v.yearly_avg.map(r => r.year),
      datasets: [{ data: v.yearly_avg.map(r => r.rate), borderColor: accent,
        backgroundColor: accent+"22", borderWidth: 2, pointRadius: 4,
        pointBackgroundColor: accent, tension: .2, fill: true }] },
    options: { responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false },
        tooltip: { callbacks: { label: x => x.parsed.y.toFixed(2)+"%" } } },
      scales: { x: { ticks: { color: muted, font:{family:"ui-monospace"} }, grid:{display:false} },
        y: { ticks: { color: muted, font:{family:"ui-monospace"}, callback:x=>x+"%" }, grid:{color:grid} } } }
  }));

  // 2) Unemployment across sectors (latest month) - horizontal bars.
  c = exhibit("Exhibit U2 | BLS CPS (NSA)", "Unemployment rate by sector",
    "Latest month, each private-industry sector. Not seasonally adjusted, so read levels with seasonal context.",
    "Source: BLS CPS unemployment rate by industry (LNU040322xx), not seasonally adjusted.",
    Math.max(240, v.sector_latest.length * 32));
  if (c) charts.push(new Chart(c, {
    type: "bar",
    data: { labels: v.sector_latest.map(r => r.sector),
      datasets: [{ data: v.sector_latest.map(r => r.rate),
        backgroundColor: v.sector_latest.map(r => r.rate < 4 ? healthy : r.rate < 5 ? cssVar("--watch") : stress),
        borderWidth: 0 }] },
    options: { indexAxis: "y", responsive: true, maintainAspectRatio: false,
      plugins: { legend:{display:false}, tooltip:{callbacks:{label:x=>x.parsed.x.toFixed(1)+"%"}} },
      scales: { x: { ticks:{color:muted,font:{family:"ui-monospace"},callback:x=>x+"%"}, grid:{color:grid} },
        y: { ticks:{color:muted,font:{family:"ui-monospace"}}, grid:{display:false} } } }
  }));

  // 3) YoY change in sector unemployment - horizontal bars, signed.
  if (v.sector_yoy.length) {
    c = exhibit("Exhibit U3 | BLS CPS (NSA)", "Change in sector unemployment (year over year)",
      "Percentage-point change vs the same month a year earlier (same-month cancels seasonality). Red = unemployment rose, green = fell.",
      "Source: BLS CPS unemployment rate by industry, same-month year-over-year difference.",
      Math.max(240, v.sector_yoy.length * 32));
    if (c) charts.push(new Chart(c, {
      type: "bar",
      data: { labels: v.sector_yoy.map(r => r.sector),
        datasets: [{ data: v.sector_yoy.map(r => r.change),
          backgroundColor: v.sector_yoy.map(r => r.change > 0 ? stress : healthy), borderWidth: 0 }] },
      options: { indexAxis: "y", responsive: true, maintainAspectRatio: false,
        plugins: { legend:{display:false}, tooltip:{callbacks:{label:x=>(x.parsed.x>=0?"+":"")+x.parsed.x.toFixed(1)+"pp"}} },
        scales: { x: { ticks:{color:muted,font:{family:"ui-monospace"},callback:x=>(x>=0?"+":"")+x+"pp"}, grid:{color:grid} },
          y: { ticks:{color:muted,font:{family:"ui-monospace"}}, grid:{display:false} } } }
    }));
  }

  // 4) Last 12 months monthly bars.
  c = exhibit("Exhibit U4 | BLS CPS", "Unemployment rate, last 12 months",
    "Monthly U-3 rate over the trailing year.",
    "Source: BLS CPS, U-3 headline rate (LNS14000000), seasonally adjusted.");
  if (c) charts.push(new Chart(c, {
    type: "bar",
    data: { labels: v.last12.map(r => r.month),
      datasets: [{ data: v.last12.map(r => r.rate), backgroundColor: accent, borderWidth: 0 }] },
    options: { responsive: true, maintainAspectRatio: false,
      plugins: { legend:{display:false}, tooltip:{callbacks:{label:x=>x.parsed.y.toFixed(1)+"%"}} },
      scales: { x: { ticks:{color:muted,font:{family:"ui-monospace"}}, grid:{display:false} },
        y: { ticks:{color:muted,font:{family:"ui-monospace"},callback:x=>x+"%"}, grid:{color:grid} } } }
  }));

  // 5) Sector table across years with trend column.
  if (v.sector_table && v.sector_table.rows.length) {
    const t = v.sector_table;
    const arrows = { up:"\u2191", down:"\u2193", flat:"\u2192" };
    const sw = document.createElement("div");
    sw.className = "sectorwrap";
    let html = `<div class="extag mono">Exhibit U5 | BLS CPS (NSA)</div>`
      + `<div class="exhead"><h2>Unemployment rate by sector, by year</h2></div>`
      + `<div class="sub">Latest month of each year per sector. Trend compares earliest year on record to latest. `
      + `Note: for unemployment, a DOWN trend (green) is good.</div>`;
    html += `<table class="sectortbl"><thead><tr><th class="name">Sector</th>`;
    t.years.forEach(y => html += `<th>${y}</th>`);
    html += `<th>Trend</th></tr></thead><tbody>`;
    t.rows.forEach(r => {
      html += `<tr><td class="name">${r.sector}</td>`;
      r.values.forEach(val => html += `<td class="mono">${val === null ? "\u2014" : val.toFixed(1)+"%"}</td>`);
      // For unemployment, falling is good: swap the color mapping.
      const cls = r.trend === "up" ? "down" : r.trend === "down" ? "up" : "flat";
      const pct = r.trend_pct === null ? "" : ` ${r.trend_pct>=0?"+":""}${r.trend_pct}%`;
      html += `<td class="trend ${cls} mono">${arrows[r.trend]}${pct}</td></tr>`;
    });
    html += `</tbody></table>`;
    html += `<p class="srcline mono">Source: BLS CPS unemployment rate by industry (LNU040322xx), `
      + `not seasonally adjusted. Green trend = unemployment fell (good); red = rose.</p>`;
    sw.innerHTML = html;
    wrap.appendChild(sw);
  }
}

async function main() {
  const res = await fetch("/data");
  const payload = await res.json();
  ALL = payload.panels;
  SECTOR_TABLE = payload.sector_table || null;
  SECTOR_NETCHANGE = payload.sector_netchange || null;
  const FRESH = payload.freshness || {};
  UNEMP_VIEWS = payload.unemployment_views || null;
  WAGE_VIEWS = payload.wage_views || null;
  if (payload.as_of)
    document.getElementById("asof").textContent = "as of " + payload.as_of.slice(0,7);

  // Documentation panel: sources + freshness + the KPI threshold table.
  // This makes the rigor already in the dashboard visible and legible.
  const guide = document.getElementById("guide");
  const srcRows = [
    ["Job openings (JOLTS)", "BLS JOLTS", "BLS_JOLTS"],
    ["Openings by sector", "BLS JOLTS by industry", "JOLTS_SECTOR"],
    ["Net job change by sector", "BLS CES employment", "CES_SECTOR"],
    ["Entry-level demand", "Adzuna API (proxy)", "ADZUNA"],
    ["Inflation (CPI YoY)", "BLS CPI-U", "CPI"],
    ["Unemployment (U-3)", "BLS CPS", "UNEMPLOYMENT"],
    ["Avg hourly earnings", "BLS CES", "EARNINGS"],
    ["Consumer sentiment", "U. Michigan", "SENTIMENT"],
  ];
  let g = `<summary>Sources, Definitions &amp; Methodology</summary><div class="body">`;
  g += `<h3>Data freshness (latest month on record, by source)</h3><div class="fresh">`;
  srcRows.forEach(([label, , key]) => {
    if (FRESH[key]) g += `<span><b>${label}:</b> ${FRESH[key]}</span>`;
  });
  g += `</div>`;
  g += `<h3>Sources</h3><table><thead><tr><th>Metric</th><th>Source</th></tr></thead><tbody>`;
  srcRows.forEach(([label, src]) => {
    g += `<tr><td>${label}</td><td>${src}</td></tr>`;
  });
  g += `</tbody></table>`;
  g += `<h3>KPI signal thresholds</h3>`
    + `<p style="color:var(--muted);margin:0 0 6px">Tiles color automatically from these `
    + `bounds. Metrics with no standard economic threshold stay neutral rather than `
    + `implying precision that isn't there.</p>`;
  g += `<table><thead><tr><th>Metric</th><th>Healthy</th><th>Watch</th><th>Stress</th><th>Rationale</th></tr></thead><tbody>`
    + `<tr><td>Unemployment (U-3)</td><td class="g">&lt; 4.0%</td><td class="a">4.0–4.9%</td><td class="r">≥ 5.0%</td><td>Sub-4% ≈ full employment; 5%+ historically signals recession onset.</td></tr>`
    + `<tr><td>CPI YoY</td><td class="g">&lt; 2.5%</td><td class="a">2.5–3.5%</td><td class="r">≥ 3.5%</td><td>Fed targets ~2%; 3.5%+ is a persistent overshoot.</td></tr>`
    + `<tr><td>Wage growth</td><td class="g">3.0–4.5%</td><td class="a">outside band</td><td class="r">&lt;2% or &gt;6%</td><td>"Goldilocks" band supports spending without a wage-price spiral.</td></tr>`
    + `<tr><td>Real wage growth</td><td class="g">≥ +0.5%</td><td class="a">0 to +0.5%</td><td class="r">&lt; 0%</td><td>Positive = pay outpacing prices; negative = buying power eroding.</td></tr>`
    + `<tr><td>Consumer sentiment</td><td class="g">≥ 70</td><td class="a">55–70</td><td class="r">&lt; 55</td><td>Long-run avg ~85; sub-55 is crisis-level (2008, 2022).</td></tr>`
    + `<tr><td>Job openings / pay level</td><td colspan="3" style="color:var(--muted)">neutral — no standard bound</td><td>Counts and dollar levels have no accepted healthy/stress cutoff.</td></tr>`
    + `</tbody></table>`;
  g += `<h3>Methodology notes</h3>`
    + `<p style="color:var(--muted);margin:0">Year-over-year figures compare the same month a year `
    + `earlier (seasonality cancels). Real wage growth = wage growth − CPI, computed live. `
    + `Lead-lag analysis (see leadlag.py) first-differences each series to avoid spurious trend `
    + `correlation and reports significance (p-value + 95% CI). Sources update on different `
    + `cadences — JOLTS and CPI lag ~4–6 weeks; sentiment is near-current.</p>`;
  g += `</div>`;
  guide.innerHTML = g;

  // Unique tabs in panel order, with "Overview" (everything) always first.
  const tabs = ["Overview"];
  ALL.forEach(p => { if (p.tab && !tabs.includes(p.tab)) tabs.push(p.tab); });
  // Insert a dedicated Unemployment tab (built from custom views, not panels)
  // right after Jobs, if we have the data for it.
  if (UNEMP_VIEWS) {
    const jobsIdx = tabs.indexOf("Jobs");
    const at = jobsIdx >= 0 ? jobsIdx + 1 : tabs.length;
    tabs.splice(at, 0, "Unemployment");
  }
  ACTIVE = tabs[0];

  // Build tab bar.
  const tabbar = document.getElementById("tabbar");
  tabs.forEach(t => {
    const b = document.createElement("button");
    b.className = "tab" + (t === ACTIVE ? " active" : "");
    b.textContent = t;
    b.addEventListener("click", () => {
      ACTIVE = t;
      [...tabbar.children].forEach(c =>
        c.classList.toggle("active", c.textContent === t));
      render();
    });
    tabbar.appendChild(b);
  });

  render();
}

function render() {
  // Destroy any prior charts so re-render doesn't leak or double-draw.
  while (charts.length) { try { charts.pop().destroy(); } catch (e) {} }

  // The Unemployment tab is built from custom views, not standard panels.
  if (ACTIVE === "Unemployment") { renderUnemployment(); return; }

  // Overview shows everything; a named tab shows only its own panels.
  const panels = ACTIVE === "Overview"
    ? ALL
    : ALL.filter(p => p.tab === ACTIVE);

  // KPI strip (filtered to active tab).
  const kpis = document.getElementById("kpis");
  kpis.innerHTML = "";
  panels.forEach(s => {
    const fmt = UNIT_FMT[s.unit] || (v => v);
    const v = s.stats.latest_value;
    const tile = document.createElement("div");
    tile.className = "tile " + s.status;
    tile.innerHTML = `<div class="lab mono">${s.kpi_label}</div>
      <div class="val mono">${v === null ? "\u2014" : fmt(v)}</div>
      <div class="dot mono">\u25CF ${STATUS_WORD[s.status]}</div>`;
    kpis.appendChild(tile);
  });

  const wrap = document.getElementById("wrap");
  wrap.innerHTML = "";

  // Tab commentary read (only for tabs where we have something honest to say).
  const read = tabRead(panels);
  if (read) {
    const c = document.createElement("div");
    c.className = "tabread";
    c.innerHTML = `<span class="mono">${ACTIVE.toUpperCase()} READ</span> ${read}`;
    wrap.appendChild(c);
  }

  // Exhibits (filtered to active tab).
  panels.forEach(s => {
    const fmt = UNIT_FMT[s.unit] || (v => v);
    const last = s.points.length ? s.points[s.points.length-1] : null;
    const ex = document.createElement("div");
    ex.className = "exhibit";
    ex.innerHTML = `
      <div class="extag mono">Exhibit ${s.exhibit}<span>|</span><b>${s.src_tag}</b>
        <span>|</span>${last ? "As of " + last.date.slice(0,7) : "No data"}</div>
      <div class="exhead">
        <h2>${s.title}</h2>
        ${last ? `<div class="latest"><div class="n mono">${fmt(last.value)}</div>
          <div class="l mono">${last.date.slice(0,7)}</div></div>` : ""}
      </div>
      <div class="sub">${s.subtitle}</div>`;

    if (!s.points.length) {
      const e = document.createElement("div");
      e.className = "empty";
      e.textContent = "No data yet. Run collect.py.";
      ex.appendChild(e);
      wrap.appendChild(ex);
      return;
    }

    const badges = document.createElement("div");
    badges.className = "badges";
    badges.innerHTML = badge("MoM", s.stats.mom_pct) + badge("YTD", s.stats.ytd_pct);
    ex.appendChild(badges);

    const box = document.createElement("div");
    box.className = "chartbox";
    const canvas = document.createElement("canvas");
    box.appendChild(canvas);
    ex.appendChild(box);

    const srcline = document.createElement("p");
    srcline.className = "srcline mono";
    srcline.textContent = s.source_line;
    ex.appendChild(srcline);

    // Per-graph analytical read - ONLY in the focused tabs, not on Overview
    // (Overview is the at-a-glance scan; the tabs are where you go to understand).
    if (ACTIVE !== "Overview") {
      const readTxt = graphRead(s);
      if (readTxt) {
        const gr = document.createElement("div");
        gr.className = "graphread";
        gr.innerHTML = `<span class="lbl">ANALYSIS</span>${readTxt}`;
        ex.appendChild(gr);
      }
    }

    wrap.appendChild(ex);

    if (!CHART_OK) {
      box.innerHTML = `<div class="empty">Chart library unavailable \u2014 `
        + `check your internet connection, then refresh.</div>`;
      return;
    }
    try {
      const muted = cssVar("--muted"), grid = cssVar("--grid");
      charts.push(new Chart(canvas, {
        type:"line",
        data:{
          labels:s.points.map(p=>p.date.slice(0,7)),
          datasets:[{
            data:s.points.map(p=>p.value),
            borderColor:s.color, backgroundColor:s.color+"1E",
            borderWidth:2, pointRadius:0, tension:.25, fill:true,
          }],
        },
        options:{
          responsive:true, maintainAspectRatio:false,
          plugins:{ legend:{display:false},
            tooltip:{ callbacks:{ label:c=>fmt(c.parsed.y) } } },
          scales:{
            x:{ ticks:{maxTicksLimit:8,color:muted,font:{family:"ui-monospace"}}, grid:{display:false} },
            y:{ ticks:{color:muted,font:{family:"ui-monospace"},callback:v=>fmt(v)}, grid:{color:grid} },
          },
        },
      }));
    } catch (err) {
      box.innerHTML = `<div class="empty">Could not draw this chart.</div>`;
    }
  });

  // CES year-over-year net job change - horizontal bar chart, Jobs tab only.
  if (ACTIVE === "Jobs" && SECTOR_NETCHANGE && SECTOR_NETCHANGE.rows.length && CHART_OK) {
    const nc = SECTOR_NETCHANGE;
    const bw = document.createElement("div");
    bw.className = "sectorwrap";
    bw.innerHTML =
      `<div class="extag mono">Exhibit B<span>|</span><b>BLS CES</b>`
      + `<span>|</span>${nc.period}</div>`
      + `<div class="exhead"><h2>Net job change by sector (year over year)</h2></div>`
      + `<div class="sub">Jobs gained or lost per sector over the last 12 months `
      + `(same month year-over-year, so seasonality cancels). Green = growing, `
      + `red = shrinking. Source: BLS CES employment levels.</div>`;
    const cbox = document.createElement("div");
    cbox.className = "chartbox";
    cbox.style.height = (Math.max(220, nc.rows.length * 34)) + "px";
    const ccanvas = document.createElement("canvas");
    cbox.appendChild(ccanvas);
    bw.appendChild(cbox);
    wrap.appendChild(bw);

    const healthy = cssVar("--healthy"), stress = cssVar("--stress"),
          muted = cssVar("--muted"), grid = cssVar("--grid");
    try {
      charts.push(new Chart(ccanvas, {
        type: "bar",
        data: {
          labels: nc.rows.map(r => r.sector),
          datasets: [{
            data: nc.rows.map(r => r.change),
            backgroundColor: nc.rows.map(r => r.change >= 0 ? healthy : stress),
            borderWidth: 0,
          }],
        },
        options: {
          indexAxis: "y",              // horizontal bars
          responsive: true, maintainAspectRatio: false,
          plugins: {
            legend: { display: false },
            tooltip: { callbacks: {
              label: c => (c.parsed.x >= 0 ? "+" : "")
                + Math.round(c.parsed.x).toLocaleString() + " jobs" } },
          },
          scales: {
            x: { ticks: { color: muted, font: { family: "ui-monospace" },
                 callback: v => (v >= 0 ? "+" : "")
                   + (Math.round(v/1000)) + "K" },
                 grid: { color: grid } },
            y: { ticks: { color: muted, font: { family: "ui-monospace" } },
                 grid: { display: false } },
          },
        },
      }));
    } catch (err) {
      cbox.innerHTML = `<div class="empty">Could not draw the bar chart.</div>`;
    }
  }

  // Sector openings table - only on the Jobs tab, and only if we have the data.
  if (ACTIVE === "Jobs" && SECTOR_TABLE && SECTOR_TABLE.rows.length) {
    const fmtK = v => v === null ? "\u2014" : Math.round(v).toLocaleString();
    const arrows = { up:"\u2191", down:"\u2193", flat:"\u2192" };
    const sw = document.createElement("div");
    sw.className = "sectorwrap";
    let html = `<div class="extag mono">Exhibit S<span>|</span><b>BLS JOLTS</b>`
      + `<span>|</span>Openings by sector</div>`
      + `<div class="exhead"><h2>Job openings by sector</h2></div>`
      + `<div class="sub">Openings per industry (latest month of each year). `
      + `The trend column compares the earliest year on record to the latest.</div>`;
    html += `<table class="sectortbl"><thead><tr><th class="name">Sector</th>`;
    SECTOR_TABLE.years.forEach(y => { html += `<th>${y}</th>`; });
    html += `<th>Trend</th></tr></thead><tbody>`;
    SECTOR_TABLE.rows.forEach(r => {
      html += `<tr><td class="name">${r.sector}</td>`;
      r.values.forEach(v => { html += `<td class="mono">${fmtK(v)}</td>`; });
      const pct = r.trend_pct === null ? "" :
        ` ${r.trend_pct >= 0 ? "+" : ""}${r.trend_pct}%`;
      html += `<td class="trend ${r.trend} mono">${arrows[r.trend]}${pct}</td></tr>`;
    });
    html += `</tbody></table>`;
    html += `<p class="srcline mono">Source: BLS JOLTS by industry `
      + `(seasonally adjusted openings levels). Figures are the latest available `
      + `month within each year, in number of openings.</p>`;
    sw.innerHTML = html;
    wrap.appendChild(sw);
  }

  // Wages tab extras: nominal-vs-inflation two-line chart + sector wage table.
  if (ACTIVE === "Wages" && WAGE_VIEWS) {
    const wv = WAGE_VIEWS;
    const accent = cssVar("--accent"), stress = cssVar("--stress"),
          muted = cssVar("--muted"), grid = cssVar("--grid");

    // Two-line chart: nominal wage growth vs inflation.
    if (wv.nominal_vs_infl && wv.nominal_vs_infl.length && CHART_OK) {
      const ex = document.createElement("div");
      ex.className = "exhibit";
      ex.innerHTML = `<div class="extag mono">Exhibit W1<span>|</span><b>BLS CES + CPI</b></div>`
        + `<div class="exhead"><h2>Wage growth vs inflation</h2></div>`
        + `<div class="sub">Nominal wage growth (blue) against inflation (red). When the blue `
        + `line is above red, raises are beating prices \u2014 the gap between them is real wage growth.</div>`;
      const box = document.createElement("div");
      box.className = "chartbox";
      const canvas = document.createElement("canvas");
      box.appendChild(canvas); ex.appendChild(box);
      const p = document.createElement("p");
      p.className = "srcline mono";
      p.textContent = "Source: BLS CES avg hourly earnings YoY vs BLS CPI-U YoY. "
        + "Real wage growth = the gap between the two lines.";
      ex.appendChild(p);
      wrap.appendChild(ex);
      try {
        charts.push(new Chart(canvas, {
          type: "line",
          data: { labels: wv.nominal_vs_infl.map(r => r.month),
            datasets: [
              { label: "Wage growth", data: wv.nominal_vs_infl.map(r => r.wage),
                borderColor: accent, backgroundColor: "transparent", borderWidth: 2,
                pointRadius: 0, tension: .25 },
              { label: "Inflation (CPI)", data: wv.nominal_vs_infl.map(r => r.cpi),
                borderColor: stress, backgroundColor: "transparent", borderWidth: 2,
                pointRadius: 0, tension: .25 },
            ] },
          options: { responsive: true, maintainAspectRatio: false,
            plugins: { legend: { display: true, labels: { color: muted, font: { family: "ui-monospace" } } },
              tooltip: { callbacks: { label: c => c.dataset.label + ": " + c.parsed.y.toFixed(2) + "%" } } },
            scales: { x: { ticks:{maxTicksLimit:8,color:muted,font:{family:"ui-monospace"}}, grid:{display:false} },
              y: { ticks:{color:muted,font:{family:"ui-monospace"},callback:v=>v+"%"}, grid:{color:grid} } } }
        }));
      } catch (err) { box.innerHTML = `<div class="empty">Could not draw this chart.</div>`; }
    }

    // Sector wage-growth table.
    if (wv.sector_table && wv.sector_table.rows.length) {
      const t = wv.sector_table;
      const arrows = { up:"\u2191", down:"\u2193", flat:"\u2192" };
      const sw2 = document.createElement("div");
      sw2.className = "sectorwrap";
      let html = `<div class="extag mono">Exhibit W2<span>|</span><b>BLS CES</b></div>`
        + `<div class="exhead"><h2>Wage growth by sector (year over year)</h2></div>`
        + `<div class="sub">Annual wage growth per sector. Trend shows whether growth is `
        + `accelerating (up) or cooling (down) from the earliest year on record to the latest.</div>`;
      html += `<table class="sectortbl"><thead><tr><th class="name">Sector</th>`;
      t.years.forEach(y => html += `<th>${y}</th>`);
      html += `<th>Trend</th></tr></thead><tbody>`;
      t.rows.forEach(r => {
        html += `<tr><td class="name">${r.sector}</td>`;
        r.values.forEach(v => html += `<td class="mono">${v === null ? "\u2014" : v.toFixed(1)+"%"}</td>`);
        const pct = r.trend_pct === null ? "" : ` ${r.trend_pct>=0?"+":""}${r.trend_pct}pp`;
        html += `<td class="trend ${r.trend} mono">${arrows[r.trend]}${pct}</td></tr>`;
      });
      html += `</tbody></table>`;
      html += `<p class="srcline mono">Source: BLS CES average hourly earnings by sector `
        + `(CES..03), year-over-year growth. Trend = change in the growth rate itself, `
        + `earliest to latest year (accelerating vs cooling).</p>`;
      sw2.innerHTML = html;
      wrap.appendChild(sw2);
    }
  }
}
main();
</script>
</body>
</html>"""


class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/data":
            body = json.dumps(load_series()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, *args):
        pass


def main():
    url = f"http://localhost:{PORT}"
    print(f"Dashboard running at {url}")
    print("Open that URL in your browser. Press Ctrl+C here to stop.")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    with socketserver.TCPServer(("", PORT), Handler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")


if __name__ == "__main__":
    main()