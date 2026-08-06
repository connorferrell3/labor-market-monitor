"""
One-time cleanup: remove sentiment rows before 2015 that were stored
before we added the year filter to collect.py.

Safe to run more than once - if there's nothing to delete, it just reports 0.
This ONLY touches SENTIMENT rows dated before 2015-01-01. Nothing else.

Usage:
    python cleanup_sentiment.py
"""

import sqlite3

DB_PATH = "jobs.db"
CUTOFF = "2015-01-01"

conn = sqlite3.connect(DB_PATH)

# Count first so you can see what will go.
before = conn.execute(
    "SELECT COUNT(*) FROM series WHERE source = 'SENTIMENT' AND date < ?",
    (CUTOFF,),
).fetchone()[0]

conn.execute(
    "DELETE FROM series WHERE source = 'SENTIMENT' AND date < ?",
    (CUTOFF,),
)
conn.commit()

remaining = conn.execute(
    "SELECT COUNT(*) FROM series WHERE source = 'SENTIMENT'"
).fetchone()[0]

print(f"Removed {before} pre-2015 sentiment rows.")
print(f"Sentiment rows remaining: {remaining}")
conn.close()