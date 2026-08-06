"""
One-time cleanup: trim every series to the shared analysis window.

We changed collect.py to start all metrics at 2021 (post-COVID) so the
lead-lag analysis has a clean, consistent overlap. But jobs.db may still hold
older rows from earlier runs (sentiment back to 2015, etc.). Upsert only
adds/updates - it never deletes - so those stale rows linger until we remove
them here.

This deletes rows dated before CUTOFF for every source EXCEPT Adzuna, whose
single snapshot is always stamped with the current month and shouldn't be
touched.

Safe to run more than once. Run it once after updating collect.py, then run
collect.py to refill the 2021+ window.

Usage:
    python cleanup_window.py
"""

import sqlite3

DB_PATH = "jobs.db"
CUTOFF = "2021-01-01"   # keep this in sync with START_YEAR in collect.py

conn = sqlite3.connect(DB_PATH)

# Count what will be removed, per source, so the change is transparent.
rows = conn.execute(
    "SELECT source, COUNT(*) FROM series "
    "WHERE date < ? AND source != 'ADZUNA' "
    "GROUP BY source ORDER BY source",
    (CUTOFF,),
).fetchall()

if not rows:
    print(f"Nothing to remove - no rows before {CUTOFF} (besides Adzuna).")
else:
    print(f"Rows dated before {CUTOFF} that will be removed:")
    for source, n in rows:
        print(f"  {source:<14} {n}")

conn.execute(
    "DELETE FROM series WHERE date < ? AND source != 'ADZUNA'",
    (CUTOFF,),
)
conn.commit()

total = conn.execute("SELECT COUNT(*) FROM series").fetchone()[0]
print(f"\nDone. Rows remaining in jobs.db: {total}")
conn.close()