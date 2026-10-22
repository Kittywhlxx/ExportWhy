"""SAMPLE_MODE twin: load data/sample/*.csv (minus ground_truth) into one SQLite file whose tables carry
the same names as the Zetaris schemastore views (`dexa.<table>`). Run after gen_data.py."""
from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

HERE = Path(__file__).resolve().parent
SAMPLE = HERE / "sample"
DB = SAMPLE / "dexa_sample.sqlite"
TABLES = ["accounts", "export_events", "alerts", "datasets", "policies", "policy_exceptions", "field_sensitivity", "tickets"]
NUMERIC = {"rows_exported", "record_count", "max_rows", "max_rows_without_ticket"}
BOOL = {"allowed", "requires_ticket", "contains_pii"}


def main() -> None:
    if DB.exists():
        DB.unlink()
    conn = sqlite3.connect(DB)
    for name in TABLES:
        with open(SAMPLE / f"{name}.csv", newline="") as fh:
            rows = list(csv.DictReader(fh))
        cols = list(rows[0].keys())
        defs = ", ".join(f"{c} {'INTEGER' if c in NUMERIC or c in BOOL else 'TEXT'}" for c in cols)
        conn.execute(f"CREATE TABLE {name} ({defs})")
        conn.executemany(f"INSERT INTO {name} VALUES ({', '.join('?' * len(cols))})",
                         [[(1 if r[c] == "True" else 0) if c in BOOL else r[c] for c in cols] for r in rows])
        print(f"{name:18s} {len(rows):5d} rows")
    conn.commit(); conn.close()
    print("sqlite:", DB)


if __name__ == "__main__":
    main()
