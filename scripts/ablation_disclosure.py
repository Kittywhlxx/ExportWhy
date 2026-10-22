"""Ablation for the disclosure controller: how much sensitive data does the triage read?

Three configurations, same 13 alerts, same evidence loop, same verdict rules:
  naive      PLANNER=naive: every evidence query is SELECT * (no column scoping)
  scoped     the shipped templates: each query names only the columns the decision needs
  nemotron   scoped + Nemotron-drafted SQL kept only when it passes the validator (taken from the live traces)

Metrics per alert, summed over the run: distinct sensitive columns read (pii / confidential / internal per
field_sensitivity), tables accessed but not needed, verdict agreement with ground truth.

    SAMPLE_MODE=true .venv/bin/python scripts/ablation_disclosure.py [--traces traces/]
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("SAMPLE_MODE", "true")

from agent import main as agent_main  # noqa: E402
from agent.roles import Task  # noqa: E402

SENS = {(r["table_name"], r["column_name"]): r["level"] for r in csv.DictReader(open(ROOT / "data/sample/field_sensitivity.csv"))}
TRUTH = {r["alert_id"]: r["expected_verdict"] for r in csv.DictReader(open(ROOT / "data/sample/ground_truth.csv"))}


def levels(touched: list[str]) -> Counter:
    c = Counter()
    for item in touched:                       # "table.column (level)"
        lvl = item.rsplit("(", 1)[1].rstrip(")")
        c[lvl] += 1
    return c


def summarise(name: str, results: list[dict]) -> dict:
    lv = Counter(); unused = 0; tables = 0; ok = 0
    for r in results:
        d = r["disclosure"]
        lv += levels(d["sensitive_columns_touched"]); unused += len(d["accessed_but_unused"]); tables += len(d["tables_accessed"])
        ok += r["verdict"] == TRUTH.get(r["alert_id"])
    n = len(results)
    return {"config": name, "alerts": n, "pii": lv["pii"], "confidential": lv["confidential"], "internal": lv["internal"],
            "sensitive_total": sum(lv.values()), "unused_tables": unused, "tables_per_alert": round(tables / n, 1), "ground_truth": f"{ok}/{n}"}


def run_config(planner: str) -> list[dict]:
    os.environ["PLANNER"] = planner
    out = []
    for aid in sorted(TRUTH):
        _, o = agent_main.run(Task(alert_id=aid), persist_memory=False)
        out.append(o)
    os.environ.pop("PLANNER", None)
    return out


def from_traces(trace_dir: Path) -> list[dict]:
    """Latest live-model trace per alert (llm == meterless-nightshift)."""
    latest: dict[str, dict] = {}
    for p in sorted(trace_dir.glob("*.json")):
        t = json.loads(p.read_text()); o = t.get("outcome") or {}
        if o.get("llm") == "meterless-nightshift" and o.get("alert_id"):
            latest[o["alert_id"]] = o
    return [latest[k] for k in sorted(latest)]


def main() -> int:
    ap = argparse.ArgumentParser(); ap.add_argument("--traces", default=str(ROOT / "traces")); ap.add_argument("--out", default=str(ROOT / "docs/ablation_disclosure.md"))
    ap.add_argument("--live-traces", action="store_true", help="add a row computed from the latest live-model trace per alert in --traces (only meaningful on a frozen directory)")
    a = ap.parse_args()
    rows = [summarise("naive (SELECT *)", run_config("naive")), summarise("scoped templates (this system)", run_config(""))]
    live = from_traces(Path(a.traces)) if a.live_traces else []
    if live:
        rows.append(summarise(f"scoped + Nemotron-drafted SQL (validated), traces from {a.traces}", live))
    cols = ["config", "alerts", "pii", "confidential", "internal", "sensitive_total", "unused_tables", "tables_per_alert", "ground_truth"]
    md = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)] + ["| " + " | ".join(str(r[c]) for c in cols) + " |" for r in rows]
    text = ("# Disclosure ablation\n\nSame 13 alerts (seed 7), same evidence loop and verdict rules; only how the queries are written differs. "
            "Counts are distinct sensitive columns read, summed over the 13 triages, classified by `field_sensitivity`.\n\n" + "\n".join(md) +
            "\n\nnaive = every evidence query is `SELECT *`; scoped = the shipped column-scoped templates. Both rows are reproducible offline. "
            "A live-model row is only added with --live-traces on a frozen trace directory, because mixing trace versions is not a like-for-like comparison.\n")
    Path(a.out).write_text(text); print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
