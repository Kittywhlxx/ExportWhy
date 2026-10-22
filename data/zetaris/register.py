"""Register the three synthetic sources in Zetaris and build the `dexa` schemastore views.

  logsdb / policydb / ticketsdb : three JDBC datasources on the same Postgres server (infra/sources),
  reached from the Zetaris query engine as host `sources` on the zetaris_default network.
  dexa.<table>                  : one schemastore container with a view per table, so the agent sees one
                                  schema while every query is federated across three databases by Zetaris.

Usage: .venv/bin/python data/zetaris/register.py [--dry-run] [--skip-exists]
Needs ZETARIS_API_KEY and SOURCES_DB_PASSWORD in .env.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from agent import config                                 # noqa: E402
from agent.zetaris import DataLayerError, ZetarisREST     # noqa: E402

SOURCES = {"logsdb": ["accounts", "export_events", "alerts"],
           "policydb": ["datasets", "policies", "policy_exceptions", "field_sensitivity"],
           "ticketsdb": ["tickets"]}


def statements(password: str) -> list[tuple[str, str]]:
    out = []
    for db in SOURCES:
        # DESCRIBE BY accepts only alphanumerics and _ . - , (no spaces)
        out.append(("sources", f'CREATE DATASOURCE {db} DESCRIBE BY "synthetic-{db}-postgres-infra-sources" OPTIONS ('
                               f'jdbcdriver "org.postgresql.Driver", jdbcurl "jdbc:postgresql://sources:5432/{db}", '
                               f'username "dexa", password "{password}", fetchsize "1000")'))
        out.append(("sources", f"REGISTER DATASOURCE TABLES FROM {db}"))
    out.append(("views", "CREATE SCHEMASTORE CONTAINER dexa"))
    for db, tables in SOURCES.items():
        for t in tables:
            out.append(("views", f"CREATE SCHEMASTORE VIEW {t} WITH CONTAINER dexa AS SELECT * FROM {db}.{t}"))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-exists", action="store_true")
    args = ap.parse_args()
    pw = config.env("SOURCES_DB_PASSWORD")
    if not pw:
        print("SOURCES_DB_PASSWORD missing from .env"); return 2
    stmts = statements(pw)
    if args.dry_run:
        for ph, s in stmts:
            print(f"-- [{ph}] {s.replace(pw, '<redacted>')}")
        print(f"{len(stmts)} statements"); return 0
    z = ZetarisREST(timeout=300)
    ok = skipped = failed = 0
    for i, (ph, s) in enumerate(stmts, 1):
        head = s.replace(pw, "<redacted>")[:110]
        try:
            r = z.run_ddl(s); ok += 1
            print(f"[{i:2d}/{len(stmts)}] OK    {head}  ({r.elapsed_ms} ms)")
        except DataLayerError as exc:
            msg = str(exc).replace(pw, "<redacted>")
            if args.skip_exists and re.search(r"already exist|AlreadyExists|duplicate", msg, re.I):
                skipped += 1; print(f"[{i:2d}/{len(stmts)}] SKIP  {head}")
            else:
                failed += 1; print(f"[{i:2d}/{len(stmts)}] FAIL  {head}\n        {msg[:300]}")
                if ph == "sources":
                    print("stopping: later statements depend on this one"); break
    print(f"\n{ok} ok, {skipped} skipped, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
