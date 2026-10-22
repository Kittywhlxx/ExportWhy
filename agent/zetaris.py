"""Data layer: every fact the agent uses comes through here.

Two backends with the same interface:
  * ZetarisREST  -- the real thing: Zetaris REST API /sql-editor/sqls/run with an API key.
  * SampleSQLite -- SAMPLE_MODE: a local SQLite file built from the same synthetic sources,
                    exposing the same `<schema>.<view>` names.
Only SELECT statements are allowed from the agent loop; registration scripts use run_ddl.
"""
from __future__ import annotations

import re
import sqlite3
import time
import uuid
from dataclasses import dataclass, field

import requests

from . import config

SELECT_ONLY = re.compile(r"^\s*(select|with|show|describe)\b", re.IGNORECASE)


class DataLayerError(RuntimeError):
    pass


@dataclass
class QueryResult:
    sql: str
    rows: list[dict]
    columns: list[str]
    source: str                 # "zetaris" | "sqlite"
    elapsed_ms: int
    query_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def as_markdown(self, limit: int = 20) -> str:
        if not self.rows:
            return "(no rows)"
        cols = self.columns
        lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
        for r in self.rows[:limit]:
            lines.append("| " + " | ".join(str(r.get(c, "")) for c in cols) + " |")
        if len(self.rows) > limit:
            lines.append(f"... {len(self.rows) - limit} more rows")
        return "\n".join(lines)


class ZetarisREST:
    """Minimal client for the Zetaris REST API (Lightning SQL over HTTP)."""

    name = "zetaris"

    def __init__(self, base_url: str | None = None, org_id: str | None = None,
                 api_key: str | None = None, timeout: int = 120):
        self.base_url = (base_url or config.ZETARIS_REST_URL or "").rstrip("/")
        self.org_id = org_id or config.ZETARIS_ORG_ID or "1"
        self.api_key = api_key or config.ZETARIS_API_KEY
        self.timeout = timeout
        if not self.api_key:
            raise DataLayerError("ZETARIS_API_KEY is not set (create one in Management > API Tokens)")

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "X-Org-ID": str(self.org_id),
            "X-Request-ID": str(uuid.uuid4()),
            "Content-Type": "application/json",
        }

    def _run(self, sql: str, limit: int) -> QueryResult:
        t0 = time.time()
        body = {"queryId": str(uuid.uuid4()), "sql": sql, "source": "SqlEditor", "limit": limit}
        for attempt in range(3):
            try:
                resp = requests.post(f"{self.base_url}/sql-editor/sqls/run", headers=self._headers(),
                                     json=body, timeout=self.timeout)
                break
            except requests.RequestException as exc:   # network blip: bounded retry
                if attempt == 2:
                    raise DataLayerError(f"Zetaris unreachable: {exc}") from exc
                time.sleep(1.5 * (attempt + 1))
        if resp.status_code != 200:
            raise DataLayerError(f"Zetaris HTTP {resp.status_code}: {resp.text[:400]}")
        payload = resp.json()
        rows, columns = _normalise(payload)
        return QueryResult(sql=sql, rows=rows, columns=columns, source=self.name,
                           elapsed_ms=int((time.time() - t0) * 1000), query_id=body["queryId"])

    def query(self, sql: str, limit: int = 200) -> QueryResult:
        if not SELECT_ONLY.match(sql):
            raise DataLayerError("agent queries must be read-only (SELECT/SHOW/DESCRIBE)")
        return self._run(sql, limit)

    def run_ddl(self, sql: str) -> QueryResult:
        """For registration scripts only. Never called from the agent loop."""
        return self._run(sql, 20)


def _normalise(payload) -> tuple[list[dict], list[str]]:
    """/sql-editor/sqls/run returns {"headers": [...], "data": [[...], ...], "total": n, "timeUsed": ms};
    every value is a string. Flatten to a list of dicts and an ordered column list (other shapes tolerated)."""
    if isinstance(payload, dict):
        data = payload.get("data")
        if data is None:
            data = payload.get("records") or payload.get("rows") or []
        cols = payload.get("headers") or payload.get("columns") or payload.get("schema") or []
    else:
        data, cols = payload, []
    if isinstance(cols, list) and cols and isinstance(cols[0], dict):
        cols = [c.get("name") or c.get("columnName") or str(i) for i, c in enumerate(cols)]
    rows: list[dict] = []
    for r in data or []:
        if isinstance(r, dict):
            rows.append(r)
        elif isinstance(r, list):
            names = cols if cols and len(cols) == len(r) else [f"c{i}" for i in range(len(r))]
            rows.append(dict(zip(names, r)))
        else:
            rows.append({"value": r})
    if rows and not cols:
        cols = list(rows[0].keys())
    return rows, list(cols)


class SampleSQLite:
    """SAMPLE_MODE backend. Tables live in an attached schema named after catalog.SCHEMA so the
    agent's SQL (<schema>.<view>) is identical to the Zetaris schemastore container."""

    name = "sqlite"

    def __init__(self, path=None):
        self.path = str(path or config.SAMPLE_DB)
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        from .catalog import SCHEMA
        self.conn.execute(f"ATTACH DATABASE ? AS {SCHEMA}", (self.path,))

    def query(self, sql: str, limit: int = 200) -> QueryResult:
        if not SELECT_ONLY.match(sql):
            raise DataLayerError("agent queries must be read-only (SELECT/SHOW/DESCRIBE)")
        t0 = time.time()
        try:
            cur = self.conn.execute(sql)
        except sqlite3.Error as exc:
            raise DataLayerError(f"sqlite: {exc}") from exc
        columns = [d[0] for d in cur.description or []]
        rows = [dict(r) for r in cur.fetchmany(limit)]
        return QueryResult(sql=sql, rows=rows, columns=columns, source=self.name,
                           elapsed_ms=int((time.time() - t0) * 1000))

    def run_ddl(self, sql: str) -> QueryResult:
        raise DataLayerError("sample backend is read-only")


SOURCES = {"logs": ["accounts", "export_events", "alerts"],
           "policy": ["datasets", "policies", "policy_exceptions", "field_sensitivity"],
           "tickets": ["tickets"]}


class OutageBackend:
    """Demo/failure-path wrapper: queries that touch the listed tables fail as if that source were down.
    Enabled with SIMULATE_OUTAGE=tickets,policy,logs (source names or table names)."""

    def __init__(self, inner, spec: str):
        self.inner = inner
        self.name = inner.name
        self.tables: set[str] = set()
        for item in [x.strip().lower() for x in spec.split(",") if x.strip()]:
            self.tables.update(SOURCES.get(item, [item]))

    def query(self, sql: str, limit: int = 200) -> QueryResult:
        from .catalog import tables_in_sql
        hit = sorted(set(tables_in_sql(sql)) & self.tables)
        if hit:
            raise DataLayerError(f"simulated outage: source for {', '.join(hit)} is unreachable")
        return self.inner.query(sql, limit)

    def run_ddl(self, sql: str) -> QueryResult:
        return self.inner.run_ddl(sql)


def get_backend():
    backend = SampleSQLite() if config.sample_mode() else ZetarisREST()
    spec = config.env("SIMULATE_OUTAGE")
    return OutageBackend(backend, spec) if spec else backend
