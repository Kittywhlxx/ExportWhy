"""Build the SAMPLE_MODE database and the flattened sample CSVs.

Inputs : data/sample/raw/*.json  (cached public API responses, trimmed; see data/README.md)
         data/sample/*.csv       (synthetic internal inventory)
Outputs: data/sample/flat/*.csv  (one CSV per table, same columns as the Zetaris views)
         data/sample/depriskr_sample.sqlite (schema `depriskr`, same table names as Zetaris)

The flattened tables mirror the schemastore views that data/zetaris/register.py creates in
Zetaris, so the agent's SQL is identical in both modes.
"""
from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

HERE = Path(__file__).resolve().parent
RAW = HERE / "sample" / "raw"
FLAT = HERE / "sample" / "flat"
DB = HERE / "sample" / "depriskr_sample.sqlite"

INVENTORY = ["services", "dependencies", "license_policy", "risk_exceptions", "adoption_requests"]


def _join(xs):
    return ",".join(str(x) for x in (xs or []))


def _license_classifier(classifiers):
    for c in classifiers or []:
        if c.startswith("License ::"):
            return c.split("::")[-1].strip()
    return None


def flatten() -> dict[str, list[dict]]:
    t: dict[str, list[dict]] = {k: [] for k in [
        "pypi_release", "pypi_vulnerability", "depsdev_version", "depsdev_advisory",
        "depsdev_package_version", "depsdev_project", "depsdev_scorecard_check"]}
    project_of_package: dict[str, str] = {}

    for f in sorted(RAW.glob("pypi_version_*.json")):
        d = json.loads(f.read_text()); info = d["info"]; url = d["_source_url"]
        pkg, ver = info["name"].lower(), info["version"]
        t["pypi_release"].append({
            "package": pkg, "version": ver, "summary": info.get("summary"),
            "license": (info.get("license") or "")[:80] or None,
            "license_expression": info.get("license_expression"),
            "license_classifier": _license_classifier(info.get("classifiers")),
            "requires_python": info.get("requires_python"), "yanked": bool(info.get("yanked")),
            "yanked_reason": info.get("yanked_reason"), "home_page": info.get("home_page"),
            "requires_dist_count": len(info.get("requires_dist") or []), "source_url": url})
        for v in d.get("vulnerabilities", []):
            t["pypi_vulnerability"].append({
                "package": pkg, "version": ver, "vuln_id": v.get("id"), "aliases": _join(v.get("aliases")),
                "fixed_in": _join(v.get("fixed_in")), "summary": (v.get("summary") or "")[:300],
                "withdrawn": v.get("withdrawn"), "link": v.get("link"), "source_url": url})

    for f in sorted(RAW.glob("depsdev_version_*.json")):
        d = json.loads(f.read_text()); vk = d["versionKey"]; url = d["_source_url"]
        pkg, ver = vk["name"].lower(), vk["version"]
        repo = next((r["projectKey"]["id"] for r in d.get("relatedProjects", []) if r.get("relationType") == "SOURCE_REPO"), None)
        if repo:
            project_of_package[pkg] = repo
        t["depsdev_version"].append({
            "package": pkg, "version": ver, "published_at": d.get("publishedAt"),
            "is_default": bool(d.get("isDefault")), "is_deprecated": bool(d.get("isDeprecated")),
            "licenses": _join(d.get("licenses")), "source_repo": repo,
            "advisory_count": len(d.get("advisoryKeys") or []), "source_url": url})
        for a in d.get("advisoryKeys", []):
            t["depsdev_advisory"].append({"package": pkg, "version": ver, "advisory_id": a.get("id"), "source_url": url})

    for f in sorted(RAW.glob("depsdev_package_*.json")):
        d = json.loads(f.read_text()); pkg = d["packageKey"]["name"].lower(); url = d["_source_url"]
        for v in d.get("versions", []):
            t["depsdev_package_version"].append({
                "package": pkg, "version": v["versionKey"]["version"], "published_at": v.get("publishedAt"),
                "is_default": bool(v.get("isDefault")), "source_url": url})

    for f in sorted(RAW.glob("depsdev_project_*.json")):
        d = json.loads(f.read_text()); pid = d["projectKey"]["id"]; url = d["_source_url"]
        pkg = next((p for p, r in project_of_package.items() if r == pid), None)
        sc = d.get("scorecard") or {}
        t["depsdev_project"].append({
            "project_id": pid, "package": pkg, "stars": d.get("starsCount"), "forks": d.get("forksCount"),
            "open_issues": d.get("openIssuesCount"), "license": d.get("license"),
            "scorecard_overall": sc.get("overallScore"), "scorecard_date": sc.get("date"), "source_url": url})
        for c in sc.get("checks", []):
            t["depsdev_scorecard_check"].append({
                "project_id": pid, "package": pkg, "check_name": c.get("name"), "score": c.get("score"),
                "reason": (c.get("reason") or "")[:200], "source_url": url})
    return t


def write_csvs(tables: dict[str, list[dict]]) -> None:
    FLAT.mkdir(parents=True, exist_ok=True)
    for name, rows in tables.items():
        with open(FLAT / f"{name}.csv", "w", newline="") as fh:
            if not rows:
                continue
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader(); w.writerows(rows)


def build_sqlite(tables: dict[str, list[dict]]) -> None:
    if DB.exists():
        DB.unlink()
    conn = sqlite3.connect(DB)
    for name in INVENTORY:
        with open(HERE / "sample" / f"{name}.csv", newline="") as fh:
            rows = list(csv.DictReader(fh))
        tables[name] = rows
    for name, rows in tables.items():
        if not rows:
            continue
        cols = list(rows[0].keys())
        conn.execute(f"CREATE TABLE {name} ({', '.join(cols)})")
        conn.executemany(f"INSERT INTO {name} VALUES ({', '.join('?' * len(cols))})",
                         [[r.get(c) for c in cols] for r in rows])
    conn.commit(); conn.close()


if __name__ == "__main__":
    tables = flatten()
    write_csvs(tables)
    build_sqlite(tables)
    for k, v in tables.items():
        print(f"{k:28s} {len(v):5d} rows")
    print("sqlite:", DB)
