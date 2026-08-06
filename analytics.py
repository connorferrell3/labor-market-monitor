"""
Job market dashboard - analytics layer (the new 4th stage).

Collect -> Store -> ANALYZE -> Display

This module READS raw values from jobs.db and COMPUTES derived numbers:
month-over-month % change and year-to-date % change. It does not fetch
anything or write to the collectors. It only knows the standard row shape,
same as the dashboard - so it's decoupled the same way.

Two ideas you'll use constantly in data work live here:

  percent change = (new - old) / old * 100

  ...and the two things that make it break in the real world:
    1. the "old" value might not exist (start of series, or a gap)
    2. the "old" value might be zero (division by zero)
  Every function below guards for both. That guarding IS the skill -
  the formula is the easy part.
"""

import sqlite3

DB_PATH = "jobs.db"


def _series(conn, source, metric):
    """Return [(date, value), ...] sorted oldest-first for one metric."""
    return conn.execute(
        "SELECT date, value FROM series "
        "WHERE source = ? AND metric = ? ORDER BY date ASC",
        (source, metric),
    ).fetchall()


def pct_change(new, old):
    """
    The core formula, with the two real-world guards.
    Returns None when the change is undefined rather than crashing or lying.
    """
    if old is None or new is None:
        return None            # nothing to compare against
    if old == 0:
        return None            # division by zero -> undefined, not infinity
    return (new - old) / old * 100


def month_over_month(conn, source, metric):
    """
    % change from the second-to-last month to the last month.
    This answers "how did this metric move most recently?"
    """
    rows = _series(conn, source, metric)
    if len(rows) < 2:
        return None            # need at least two points to have a change
    latest_value = rows[-1][1]
    prev_value = rows[-2][1]
    return pct_change(latest_value, prev_value)


def year_to_date(conn, source, metric):
    """
    % change from the FIRST reading of the current year to the latest reading.
    "Current year" = the year of the most recent data point, so this stays
    correct no matter when you run it.

    Example: if the latest point is 2026-07 and the first 2026 reading was
    2026-01, YTD = change from January to July.
    """
    rows = _series(conn, source, metric)
    if len(rows) < 2:
        return None
    latest_date, latest_value = rows[-1]
    current_year = latest_date[:4]          # 'YYYY' from 'YYYY-MM-01'

    # find the earliest row whose date is in the current year
    start_value = None
    for date, value in rows:
        if date[:4] == current_year:
            start_value = value
            break

    return pct_change(latest_value, start_value)


def summary(conn, source, metric):
    """Bundle both changes plus the latest value into one dict for the UI."""
    rows = _series(conn, source, metric)
    latest = rows[-1] if rows else None
    return {
        "latest_date": latest[0] if latest else None,
        "latest_value": latest[1] if latest else None,
        "mom_pct": month_over_month(conn, source, metric),
        "ytd_pct": year_to_date(conn, source, metric),
    }


def real_wage_series(conn):
    """
    Build the real wage growth series: wage growth minus inflation, month by month.

    This is a DERIVED indicator - it isn't stored anywhere, it's computed from
    two series you already collect:
        real wage growth = earnings_yoy_pct  -  CPI inflation_yoy_pct

    The economic meaning: when this is POSITIVE, paychecks are growing faster
    than prices, so workers' buying power is rising. When NEGATIVE, prices are
    outrunning pay - people earn more dollars but can afford less. This is the
    single clearest read on whether wage gains are "real" or just keeping up
    with (or losing to) inflation.

    We only produce a value for months where BOTH inputs exist, then align them
    by date - the same alignment discipline the lead-lag analysis uses.
    Returns [(date, real_wage_pct), ...] oldest-first.
    """
    wages = dict(_series(conn, "EARNINGS", "earnings_yoy_pct"))
    infl = dict(_series(conn, "CPI", "inflation_yoy_pct"))

    out = []
    for date in sorted(wages):
        if date in infl:                     # only months present in BOTH
            real = round(wages[date] - infl[date], 2)
            out.append((date, real))
    return out


def real_wage_summary(conn):
    """Latest-value + MoM/YTD style summary for the derived real-wage series."""
    rows = real_wage_series(conn)
    if not rows:
        return {"latest_date": None, "latest_value": None,
                "mom_pct": None, "ytd_pct": None}
    latest_date, latest_value = rows[-1]
    # MoM here is the change in the SPREAD in percentage points, not a % change,
    # so we report the simple difference (clearer for a value already in %).
    mom = None
    if len(rows) >= 2:
        mom = round(latest_value - rows[-2][1], 2)
    ytd = None
    current_year = latest_date[:4]
    for date, value in rows:
        if date[:4] == current_year:
            ytd = round(latest_value - value, 2)
            break
    return {"latest_date": latest_date, "latest_value": latest_value,
            "mom_pct": mom, "ytd_pct": ytd}


# Quick self-test you can run directly: `python analytics.py`
if __name__ == "__main__":
    conn = sqlite3.connect(DB_PATH)
    tests = [
        ("BLS_JOLTS", "total_openings"),
        ("ADZUNA", "entry_level_index"),
        ("CPI", "inflation_yoy_pct"),
        ("SENTIMENT", "umich_ics"),
    ]
    for src, met in tests:
        s = summary(conn, src, met)
        mom = f"{s['mom_pct']:+.2f}%" if s["mom_pct"] is not None else "n/a"
        ytd = f"{s['ytd_pct']:+.2f}%" if s["ytd_pct"] is not None else "n/a"
        print(f"{src:<10} {met:<20} latest={s['latest_value']}  MoM={mom}  YTD={ytd}")
    conn.close()