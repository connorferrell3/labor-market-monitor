"""
Job market dashboard - data collection layer.

Pulls monthly job-openings data into a single SQLite time-series table.
All metrics (JOLTS openings, Adzuna postings, CPI inflation, UMich sentiment)
share one schema: (date, source, metric, value) so everything is queryable
together. CPI reuses the BLS key; sentiment needs no key at all.

Setup:
    pip install requests
    export BLS_API_KEY=...            # from data.bls.gov/registrationEngine/
    export ADZUNA_APP_ID=...          # from developer.adzuna.com
    export ADZUNA_APP_KEY=...

Usage:
    python collect.py
"""

import os
import sqlite3
import datetime as dt
import requests

DB_PATH = "jobs.db"

# ---- Entry-level definition (KEEP THIS CONSTANT once you have history) ----
ENTRY_LEVEL_TERMS = ["entry level", "junior", "new grad", "graduate", "0-2 years"]
# Adzuna "what" query uses OR semantics with spaces; we run one query per term
# and de-dupe on posting count is not possible, so we treat the sum as a proxy
# index rather than a unique count. Consistency matters more than absolute value.


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------
def init_db(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS series (
            date   TEXT NOT NULL,       -- 'YYYY-MM-01' (monthly)
            source TEXT NOT NULL,       -- 'BLS_JOLTS', 'ADZUNA', 'CPI', 'SENTIMENT'
            metric TEXT NOT NULL,       -- 'total_openings', 'entry_level_index', ...
            value  REAL NOT NULL,
            PRIMARY KEY (date, source, metric)
        )
    """)
    conn.commit()


def upsert(conn, date, source, metric, value):
    conn.execute(
        "INSERT OR REPLACE INTO series (date, source, metric, value) VALUES (?,?,?,?)",
        (date, source, metric, value),
    )


# --------------------------------------------------------------------------
# BLS JOLTS - total job openings (national, seasonally adjusted)
# Series JTS000000000000000JOL = Total nonfarm job openings, level, SA
# --------------------------------------------------------------------------
def fetch_jolts(conn, start_year, end_year):
    key = os.environ["BLS_API_KEY"]
    series_id = "JTS000000000000000JOL"
    resp = requests.post(
        "https://api.bls.gov/publicAPI/v2/timeseries/data/",
        json={
            "seriesid": [series_id],
            "startyear": str(start_year),
            "endyear": str(end_year),
            "registrationkey": key,
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    if data.get("status") != "REQUEST_SUCCEEDED":
        raise RuntimeError(f"BLS error: {data.get('message')}")

    rows = data["Results"]["series"][0]["data"]
    count = 0
    for r in rows:
        if not r["period"].startswith("M"):   # skip annual averages
            continue
        month = int(r["period"][1:])
        date = f"{r['year']}-{month:02d}-01"
        # JOLTS openings are reported in thousands
        upsert(conn, date, "BLS_JOLTS", "total_openings", float(r["value"]) * 1000)
        count += 1
    conn.commit()
    print(f"JOLTS: stored {count} monthly observations")


# --------------------------------------------------------------------------
# BLS JOLTS by industry - job openings per sector
# Same API, same key as total JOLTS. The only new idea: instead of ONE series,
# we loop over a dict of {sector_name: series_id} and store each under its own
# metric. BLS encodes the industry in the series id: JTS + industry code + JOL.
#
# We store each sector as metric = "openings_<sector>" so they all live in the
# same table, queryable together - the contract table absorbing yet another
# shape with zero downstream changes.
# --------------------------------------------------------------------------
JOLTS_SECTORS = {
    # Full-length BLS series IDs. The short form (JTS2300JOL) silently returns
    # no data on the v2 API - the real ID needs the full padding:
    #   JTS + industry(6) + state(2) + area(5) + sizeclass(2) + element(2) + R/L(1)
    # These end in JOL = job openings LEVEL (a count, in thousands), which is
    # what this table displays. (JOR would be the rate/percent - different thing.)
    "Prof. & business svcs":     "JTS540099000000000JOL",
    "Trade, transport, util.":   "JTS400000000000000JOL",
    "Education & health":        "JTS600000000000000JOL",
    "Health care & social":      "JTS620000000000000JOL",
    "Leisure & hospitality":     "JTS700000000000000JOL",
    "Accommodation & food":      "JTS720000000000000JOL",
    "Manufacturing":             "JTS300000000000000JOL",
    "Retail trade":              "JTS440000000000000JOL",
    "Construction":              "JTS230000000000000JOL",
    "Government":                "JTS900000000000000JOL",
}
# The full-ID structure was confirmed from BLS metadata. If you ever add one,
# keep the padding: industry code, then zeros out to the element+level suffix.
# The per-series diagnostic below reports any ID that returns no rows.


def fetch_jolts_sectors(conn, start_year, end_year):
    key = os.environ["BLS_API_KEY"]

    # We request ONE sector per call. Batching many series in a single request
    # (the earlier approach) returned empty data arrays for every series - a
    # known BLS quirk. Fetching individually mirrors fetch_jolts, which works.
    # It costs one API call per sector (10 total), still trivial against the
    # 500/day limit.
    total = 0
    stored_sectors = 0
    for name, series_id in JOLTS_SECTORS.items():
        resp = requests.post(
            "https://api.bls.gov/publicAPI/v2/timeseries/data/",
            json={
                "seriesid": [series_id],
                "startyear": str(start_year),
                "endyear": str(end_year),
                "registrationkey": key,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "REQUEST_SUCCEEDED":
            print(f"  [JOLTS sectors: '{name}' request failed: {data.get('message')}]")
            continue

        series_list = data.get("Results", {}).get("series", [])
        if not series_list:
            print(f"  [JOLTS sectors: '{name}' ({series_id}) returned no series]")
            continue

        series = series_list[0]
        rows = series.get("data", [])
        # BLS puts an explanation here when a series has no data for the range.
        msg = series.get("message") or data.get("message")
        metric = "openings_" + name
        count = 0
        for r in rows:
            if not r["period"].startswith("M"):
                continue
            month = int(r["period"][1:])
            try:
                value = float(r["value"]) * 1000   # JOLTS reported in thousands
            except ValueError:
                continue
            date = f"{r['year']}-{month:02d}-01"
            upsert(conn, date, "JOLTS_SECTOR", metric, value)
            count += 1

        if count == 0:
            extra = f" (message: {msg})" if msg else ""
            print(f"  [JOLTS sectors: '{name}' ({series_id}) 0 rows{extra}]")
        else:
            stored_sectors += 1
        total += count

    conn.commit()
    print(f"JOLTS sectors: stored {total} observations across "
          f"{stored_sectors} of {len(JOLTS_SECTORS)} sectors")


# --------------------------------------------------------------------------
# CES employment by sector - total jobs (the COUNT of employees) per sector
#
# This is a DIFFERENT survey from JOLTS. JOLTS above = job OPENINGS (unfilled
# positions). CES here = total EMPLOYMENT (jobs that exist / are filled). We
# store the employment LEVEL each month; the dashboard derives year-over-year
# net change (this year's level minus last year's) - the "+687K / -113K" figure
# the reference dashboard shows.
#
# CES series ID structure (verified against BLS ce.supersector file):
#   CES + seasonal(S) + supersector+industry(8 digits) + datatype(2)
#   datatype 01 = all employees (thousands). e.g. Construction = CES2000000001.
# --------------------------------------------------------------------------
CES_SECTORS = {
    "Construction":            "CES2000000001",
    "Manufacturing":           "CES3000000001",
    "Trade, transport, util.": "CES4000000001",
    "Retail trade":            "CES4200000001",
    "Information":             "CES5000000001",
    "Financial activities":    "CES5500000001",
    "Prof. & business svcs":   "CES6000000001",
    "Education & health":      "CES6500000001",
    "Leisure & hospitality":   "CES7000000001",
    "Government":              "CES9000000001",
}


def fetch_ces_sectors(conn, start_year, end_year):
    key = os.environ["BLS_API_KEY"]

    total = 0
    stored_sectors = 0
    for name, series_id in CES_SECTORS.items():
        resp = requests.post(
            "https://api.bls.gov/publicAPI/v2/timeseries/data/",
            json={
                "seriesid": [series_id],
                "startyear": str(start_year),
                "endyear": str(end_year),
                "registrationkey": key,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "REQUEST_SUCCEEDED":
            print(f"  [CES sectors: '{name}' request failed: {data.get('message')}]")
            continue

        series_list = data.get("Results", {}).get("series", [])
        if not series_list:
            print(f"  [CES sectors: '{name}' ({series_id}) returned no series]")
            continue

        series = series_list[0]
        rows = series.get("data", [])
        msg = series.get("message") or data.get("message")
        metric = "employment_" + name
        count = 0
        for r in rows:
            if not r["period"].startswith("M"):
                continue
            month = int(r["period"][1:])
            try:
                value = float(r["value"]) * 1000   # CES reported in thousands
            except ValueError:
                continue
            date = f"{r['year']}-{month:02d}-01"
            upsert(conn, date, "CES_SECTOR", metric, value)
            count += 1

        if count == 0:
            extra = f" (message: {msg})" if msg else ""
            print(f"  [CES sectors: '{name}' ({series_id}) 0 rows{extra}]")
        else:
            stored_sectors += 1
        total += count

    conn.commit()
    print(f"CES sectors: stored {total} observations across "
          f"{stored_sectors} of {len(CES_SECTORS)} sectors")


# --------------------------------------------------------------------------
# Unemployment rate BY INDUSTRY (CPS)
#
# IMPORTANT: unlike the headline U-3 (LNS14000000, seasonally adjusted), the
# by-industry unemployment series are NOT seasonally adjusted - BLS only
# publishes them NSA. That means they swing with seasonal patterns (e.g.
# construction unemployment spikes every winter). We store them as-is and note
# it in the dashboard; for year-over-year comparisons we compare same-month to
# same-month so the seasonality cancels.
#
# Series structure (verified on FRED): LNU040322XX = CPS unemployment RATE by
# industry, NSA, private wage & salary workers. Each XX confirmed individually.
# --------------------------------------------------------------------------
UNEMP_SECTORS = {
    "Construction":            "LNU04032231",
    "Manufacturing":           "LNU04032232",
    "Wholesale & retail":      "LNU04032235",
    "Transport & utilities":   "LNU04032236",
    "Information":             "LNU04032237",
    "Financial activities":    "LNU04032238",
    "Prof. & business svcs":   "LNU04032239",
    "Education & health":      "LNU04032240",
    "Leisure & hospitality":   "LNU04032241",
}


def fetch_unemployment_sectors(conn, start_year, end_year):
    key = os.environ["BLS_API_KEY"]

    total = 0
    stored_sectors = 0
    for name, series_id in UNEMP_SECTORS.items():
        resp = requests.post(
            "https://api.bls.gov/publicAPI/v2/timeseries/data/",
            json={
                "seriesid": [series_id],
                "startyear": str(start_year),
                "endyear": str(end_year),
                "registrationkey": key,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "REQUEST_SUCCEEDED":
            print(f"  [Unemp sectors: '{name}' request failed: {data.get('message')}]")
            continue

        series_list = data.get("Results", {}).get("series", [])
        if not series_list:
            print(f"  [Unemp sectors: '{name}' ({series_id}) returned no series]")
            continue

        series = series_list[0]
        rows = series.get("data", [])
        msg = series.get("message") or data.get("message")
        metric = "unemp_" + name
        count = 0
        for r in rows:
            if not r["period"].startswith("M"):
                continue
            month = int(r["period"][1:])
            try:
                value = float(r["value"])       # already a percent
            except ValueError:
                continue
            date = f"{r['year']}-{month:02d}-01"
            upsert(conn, date, "UNEMP_SECTOR", metric, value)
            count += 1

        if count == 0:
            extra = f" (message: {msg})" if msg else ""
            print(f"  [Unemp sectors: '{name}' ({series_id}) 0 rows{extra}]")
        else:
            stored_sectors += 1
        total += count

    conn.commit()
    print(f"Unemployment sectors: stored {total} observations across "
          f"{stored_sectors} of {len(UNEMP_SECTORS)} sectors")


# --------------------------------------------------------------------------
# CES average hourly earnings BY SECTOR - wage growth per sector
#
# Same supersector codes as CES_SECTORS (employment), but data-type 03 =
# average hourly earnings (dollars) instead of 01 = employees. We store the
# dollar level, then the dashboard derives year-over-year wage growth from it
# (this year's $ vs last year's $), the same way it does for total earnings.
# Government is excluded - CES earnings cover private industries only.
#
# IDs are derived from the already-verified CES_SECTORS supersector codes by
# swapping the trailing 01 -> 03, so no separate verification is needed: the
# supersector codes were confirmed against BLS when CES_SECTORS was built, and
# CES2000000003 / CES3000000003 were re-confirmed on FRED.
# --------------------------------------------------------------------------
WAGE_SECTORS = {
    name: sid[:-2] + "03"
    for name, sid in CES_SECTORS.items()
    if name != "Government"
}


def fetch_wage_sectors(conn, start_year, end_year):
    key = os.environ["BLS_API_KEY"]

    total = 0
    stored_sectors = 0
    for name, series_id in WAGE_SECTORS.items():
        resp = requests.post(
            "https://api.bls.gov/publicAPI/v2/timeseries/data/",
            json={
                "seriesid": [series_id],
                "startyear": str(start_year),
                "endyear": str(end_year),
                "registrationkey": key,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "REQUEST_SUCCEEDED":
            print(f"  [Wage sectors: '{name}' request failed: {data.get('message')}]")
            continue

        series_list = data.get("Results", {}).get("series", [])
        if not series_list:
            print(f"  [Wage sectors: '{name}' ({series_id}) returned no series]")
            continue

        series = series_list[0]
        rows = series.get("data", [])
        msg = series.get("message") or data.get("message")
        metric = "wage_usd_" + name
        count = 0
        for r in rows:
            if not r["period"].startswith("M"):
                continue
            month = int(r["period"][1:])
            try:
                value = float(r["value"])       # dollars per hour
            except ValueError:
                continue
            date = f"{r['year']}-{month:02d}-01"
            upsert(conn, date, "WAGE_SECTOR", metric, value)
            count += 1

        if count == 0:
            extra = f" (message: {msg})" if msg else ""
            print(f"  [Wage sectors: '{name}' ({series_id}) 0 rows{extra}]")
        else:
            stored_sectors += 1
        total += count

    conn.commit()
    print(f"Wage sectors: stored {total} observations across "
          f"{stored_sectors} of {len(WAGE_SECTORS)} sectors")


# --------------------------------------------------------------------------
# Adzuna - posting counts (total + entry-level proxy), US
# --------------------------------------------------------------------------
def fetch_adzuna(conn):
    app_id = os.environ["ADZUNA_APP_ID"]
    app_key = os.environ["ADZUNA_APP_KEY"]
    today = dt.date.today().replace(day=1).isoformat()
    base = "https://api.adzuna.com/v1/api/jobs/us/search/1"

    def count_for(query=None):
        params = {
            "app_id": app_id,
            "app_key": app_key,
            "results_per_page": 1,   # we only want the 'count' field
            "content-type": "application/json",
        }
        if query:
            params["what_phrase"] = query
        r = requests.get(base, params=params, timeout=30)
        r.raise_for_status()
        return r.json().get("count", 0)

    total = count_for()
    upsert(conn, today, "ADZUNA", "total_openings", float(total))
    print(f"Adzuna total openings: {total:,}")

    entry_sum = 0
    for term in ENTRY_LEVEL_TERMS:
        c = count_for(term)
        entry_sum += c
        print(f"  entry-level term '{term}': {c:,}")
    # stored as an index/proxy, not a unique count (terms overlap)
    upsert(conn, today, "ADZUNA", "entry_level_index", float(entry_sum))
    conn.commit()
    print(f"Adzuna entry-level index: {entry_sum:,}")


# --------------------------------------------------------------------------
# BLS CPI - inflation (Consumer Price Index, all items, all urban consumers)
# Series CUUR0000SA0 = CPI-U, U.S. city average, not seasonally adjusted, index
#
# READ THIS: notice how nearly identical this is to fetch_jolts above. Same API,
# same key, same four moves. The ONLY real differences are (a) a different
# series id, and (b) we compute year-over-year inflation % from the raw index
# instead of just storing the number. That "reshape" step (move 3) is where the
# meaning gets added.
# --------------------------------------------------------------------------
def fetch_cpi(conn, start_year, end_year):
    key = os.environ["BLS_API_KEY"]           # SAME key you already have
    series_id = "CUUR0000SA0"

    # MOVE 1: request
    resp = requests.post(
        "https://api.bls.gov/publicAPI/v2/timeseries/data/",
        json={
            "seriesid": [series_id],
            "startyear": str(start_year),
            "endyear": str(end_year),
            "registrationkey": key,
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "REQUEST_SUCCEEDED":
        raise RuntimeError(f"BLS error: {data.get('message')}")

    # MOVE 2: parse. Build a lookup of {('YYYY', month_int): index_value}
    # so we can look back 12 months to compute inflation.
    index_by_month = {}
    for r in data["Results"]["series"][0]["data"]:
        if not r["period"].startswith("M"):
            continue
        month = int(r["period"][1:])
        # BLS sometimes returns '-' (or other non-numbers) when a value isn't
        # available for a month. Skip those rather than crashing on float('-').
        try:
            index_by_month[(r["year"], month)] = float(r["value"])
        except ValueError:
            continue

    # MOVE 3: reshape. CPI is an index number (e.g. 314.2) which means nothing
    # on its own. Inflation is how much it rose vs the SAME month last year.
    # inflation% = (this_year_index / last_year_index - 1) * 100
    count = 0
    for (year, month), idx in index_by_month.items():
        prev = index_by_month.get((str(int(year) - 1), month))
        if prev is None:            # no data 12 months back -> can't compute
            continue
        inflation_pct = (idx / prev - 1) * 100
        date = f"{year}-{month:02d}-01"
        # MOVE 4: upsert
        upsert(conn, date, "CPI", "inflation_yoy_pct", round(inflation_pct, 2))
        count += 1
    conn.commit()
    print(f"CPI: stored {count} monthly inflation rates")


# --------------------------------------------------------------------------
# BLS Unemployment rate (U-3, the headline rate)
# Series LNS14000000 = Unemployment Rate, 16+, seasonally adjusted, percent.
#
# This is the SIMPLEST BLS collector yet: the value BLS returns is already the
# percentage we want, so there's no reshape math (move 3) at all - we store the
# number as-is. Compare it to fetch_cpi to see how little changes between BLS
# series: same request, same parse guard, just no year-over-year step.
# --------------------------------------------------------------------------
def fetch_unemployment(conn, start_year, end_year):
    key = os.environ["BLS_API_KEY"]           # SAME key again
    series_id = "LNS14000000"

    resp = requests.post(
        "https://api.bls.gov/publicAPI/v2/timeseries/data/",
        json={
            "seriesid": [series_id],
            "startyear": str(start_year),
            "endyear": str(end_year),
            "registrationkey": key,
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "REQUEST_SUCCEEDED":
        raise RuntimeError(f"BLS error: {data.get('message')}")

    count = 0
    for r in data["Results"]["series"][0]["data"]:
        if not r["period"].startswith("M"):
            continue
        month = int(r["period"][1:])
        try:
            value = float(r["value"])         # already a percent, store directly
        except ValueError:
            continue
        date = f"{r['year']}-{month:02d}-01"
        upsert(conn, date, "UNEMPLOYMENT", "u3_rate_pct", value)
        count += 1
    conn.commit()
    print(f"Unemployment: stored {count} monthly rates")


# --------------------------------------------------------------------------
# BLS Average Hourly Earnings - all private employees
# Series CES0500000003 = Avg Hourly Earnings, total private, SA, dollars.
#
# We store TWO metrics from this one series, to answer two questions:
#   earnings_usd      -> the dollar level  ("what's a paycheck worth now?")
#   earnings_yoy_pct  -> YoY % change      ("are raises keeping up?")
# The YoY math is the SAME 12-month-lookback pattern as CPI - reused on purpose
# so you can see it's a general technique, not a CPI-specific trick.
# --------------------------------------------------------------------------
def fetch_earnings(conn, start_year, end_year):
    key = os.environ["BLS_API_KEY"]
    series_id = "CES0500000003"

    resp = requests.post(
        "https://api.bls.gov/publicAPI/v2/timeseries/data/",
        json={
            "seriesid": [series_id],
            "startyear": str(start_year),
            "endyear": str(end_year),
            "registrationkey": key,
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "REQUEST_SUCCEEDED":
        raise RuntimeError(f"BLS error: {data.get('message')}")

    # Parse into a month lookup first (needed for the YoY step).
    usd_by_month = {}
    for r in data["Results"]["series"][0]["data"]:
        if not r["period"].startswith("M"):
            continue
        month = int(r["period"][1:])
        try:
            usd_by_month[(r["year"], month)] = float(r["value"])
        except ValueError:
            continue

    count_usd = 0
    count_yoy = 0
    for (year, month), usd in usd_by_month.items():
        date = f"{year}-{month:02d}-01"
        # metric 1: the dollar level, stored directly
        upsert(conn, date, "EARNINGS", "earnings_usd", round(usd, 2))
        count_usd += 1
        # metric 2: YoY %, only when we have data 12 months back
        prev = usd_by_month.get((str(int(year) - 1), month))
        if prev:                              # truthy also guards prev == 0
            yoy = (usd / prev - 1) * 100
            upsert(conn, date, "EARNINGS", "earnings_yoy_pct", round(yoy, 2))
            count_yoy += 1
    conn.commit()
    print(f"Earnings: stored {count_usd} dollar levels, {count_yoy} YoY rates")


# --------------------------------------------------------------------------
# University of Michigan - Index of Consumer Sentiment
# No API key: they publish a CSV on their site. This shows you the OTHER kind
# of source - a downloaded file instead of a JSON API. Move 1 changes (we grab
# a file), move 2 changes (we read CSV rows, not JSON), but moves 3 and 4 are
# the same as always. That's the point: the standard row shields everything
# downstream from how messy the source is.
# --------------------------------------------------------------------------
def fetch_sentiment(conn):
    import csv
    import io

    # Match the START_YEAR used for the other series in main() so every metric
    # shares the same window. Post-COVID (2021+) keeps the overlap clean.
    SENTIMENT_START_YEAR = 2021

    url = "https://www.sca.isr.umich.edu/files/tbmics.csv"

    # MOVE 1: request (a file, not a JSON query)
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()

    # MOVE 2: parse CSV. The file has columns like: Month, Year, ICS
    # (the exact header names can change - if this breaks, print(reader.fieldnames)
    # to see what the columns are actually called, then adjust the keys below.)
    reader = csv.DictReader(io.StringIO(resp.text))

    month_names = {
        "january": 1, "february": 2, "march": 3, "april": 4,
        "may": 5, "june": 6, "july": 7, "august": 8,
        "september": 9, "october": 10, "november": 11, "december": 12,
    }

    count = 0
    for row in reader:
        raw_month = (row.get("Month") or "").strip().lower()
        raw_year = (row.get("YYYY") or "").strip()
        raw_value = (row.get("ICS_ALL") or "").strip()
        if not (raw_month and raw_year and raw_value):
            continue
        # Skip anything before our chosen start year.
        try:
            if int(raw_year) < SENTIMENT_START_YEAR:
                continue
        except ValueError:
            continue
        month = month_names.get(raw_month)
        if month is None:            # sometimes month is already a number
            try:
                month = int(raw_month)
            except ValueError:
                continue
        # MOVE 3 + 4: reshape to standard row, then upsert
        date = f"{raw_year}-{month:02d}-01"
        try:
            value = float(raw_value)
        except ValueError:
            continue
        upsert(conn, date, "SENTIMENT", "umich_ics", value)
        count += 1
    conn.commit()
    print(f"Sentiment: stored {count} monthly observations")


# --------------------------------------------------------------------------
def main():
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    this_year = dt.date.today().year

    # All series share one start year so the lead-lag analysis has a clean,
    # consistent overlap window. 2021 = first full post-COVID year, chosen to
    # avoid the 2020 shock months dominating any correlation.
    START_YEAR = 2021

    # Run each collector in isolation. If one source is down (e.g. Adzuna
    # returns a 503) or misbehaves, we log it and keep going rather than
    # letting one failure abort the whole run. A pipeline that pulls from
    # several independent sources should degrade gracefully, not all-or-nothing.
    def run(label, fn):
        try:
            fn()
        except Exception as e:
            print(f"  [skipped {label}: {type(e).__name__}: {e}]")

    run("JOLTS total",   lambda: fetch_jolts(conn, START_YEAR, this_year))
    run("JOLTS sectors", lambda: fetch_jolts_sectors(conn, START_YEAR, this_year))
    run("CES sectors",   lambda: fetch_ces_sectors(conn, START_YEAR, this_year))
    run("Adzuna",        lambda: fetch_adzuna(conn))
    # CPI and earnings compute year-over-year, so they need data from one year
    # BEFORE the start to produce a value for the start year itself.
    run("CPI",           lambda: fetch_cpi(conn, START_YEAR - 1, this_year))
    run("Unemployment",  lambda: fetch_unemployment(conn, START_YEAR, this_year))
    run("Unemp sectors", lambda: fetch_unemployment_sectors(conn, START_YEAR, this_year))
    run("Earnings",      lambda: fetch_earnings(conn, START_YEAR - 1, this_year))
    run("Wage sectors",  lambda: fetch_wage_sectors(conn, START_YEAR - 1, this_year))
    run("Sentiment",     lambda: fetch_sentiment(conn))

    print("\nCurrent contents:")
    for row in conn.execute(
        "SELECT date, source, metric, value FROM series ORDER BY date DESC, source LIMIT 10"
    ):
        print(f"  {row[0]}  {row[1]:<10} {row[2]:<20} {row[3]:,.2f}")

    conn.close()


if __name__ == "__main__":
    main()