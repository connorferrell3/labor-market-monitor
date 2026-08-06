"""
Diagnostic probe for the JOLTS sector issue.

Runs ONE sector request against BLS and prints the raw response structure so we
can see exactly why parsing found "0 usable rows". Run it once and paste me the
output.

    python probe_sectors.py
"""

import os
import json
import requests

key = os.environ["BLS_API_KEY"]

# Two sectors, FULL-LENGTH level IDs, small year range to keep output short.
series_ids = ["JTS230000000000000JOL", "JTS300000000000000JOL"]   # Construction, Mfg

resp = requests.post(
    "https://api.bls.gov/publicAPI/v2/timeseries/data/",
    json={
        "seriesid": series_ids,
        "startyear": "2024",
        "endyear": "2025",
        "registrationkey": key,
    },
    timeout=60,
)
data = resp.json()

print("HTTP status:", resp.status_code)
print("API status :", data.get("status"))
print("API message:", data.get("message"))
print()

series_list = data.get("Results", {}).get("series", [])
print(f"Series returned: {len(series_list)}")
for s in series_list:
    d = s.get("data", [])
    print(f"\n  seriesID: {s.get('seriesID')}")
    print(f"  data rows: {len(d)}")
    if d:
        # Show the first row VERBATIM so we can see its exact keys/values.
        print("  first row (raw):")
        print("   ", json.dumps(d[0], indent=2).replace("\n", "\n    "))