"""Register the three DepRisk sources in Zetaris and build the `depriskr` schemastore views.

Sources
  1. orgdb        JDBC datasource: the synthetic inventory Postgres (infra/orgdb), host `orgdb`.
  2. PYPI_REST    Lightning REST tables, one per (package, version): https://pypi.org/pypi/<p>/<v>/json
  3. DEPSDEV_REST Lightning REST tables: deps.dev version / package / project endpoints.

The (package, version) list is derived from the inventory CSVs, so the registered endpoints are
exactly the pins and adoption targets the organisation cares about. The views union the
per-package tables into one schema (`depriskr.*`) with the same columns as data/build_sample.py.

Usage: .venv/bin/python data/zetaris/register.py [--dry-run] [--skip-exists] [--only views]
Needs ZETARIS_API_KEY and ORGDB_PASSWORD in .env. Idempotent with --skip-exists.
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from agent import config                      # noqa: E402
from agent.zetaris import DataLayerError, ZetarisREST  # noqa: E402

SAMPLE = ROOT / "data" / "sample"
INVENTORY = ["services", "dependencies", "license_policy", "risk_exceptions", "adoption_requests"]
PROJECTS = {  # package -> deps.dev project id (source repo), from the deps.dev version endpoint
    "requests": "github.com/psf/requests", "httpx": "github.com/encode/httpx",
    "pyyaml": "github.com/yaml/pyyaml", "python-jose": "github.com/mpdavis/python-jose",
    "pyjwt": "github.com/jpadilla/pyjwt", "urllib3": "github.com/urllib3/urllib3",
    "pillow": "github.com/python-pillow/pillow", "cryptography": "github.com/pyca/cryptography",
}


def ident(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower())


def package_versions() -> list[tuple[str, str]]:
    seen: set[tuple[str, str]] = set()
    with open(SAMPLE / "dependencies.csv", newline="") as fh:
        for r in csv.DictReader(fh):
            seen.add((r["package"].lower(), r["pinned_version"]))
    with open(SAMPLE / "adoption_requests.csv", newline="") as fh:
        for r in csv.DictReader(fh):
            seen.add((r["package"].lower(), r["requested_version"]))
    return sorted(seen)


def rest_table(name: str, db: str, url: str) -> str:
    return (f'CREATE LIGHTNING REST TABLE {name} FROM {db} REQUEST('
            f'endpoint "{url}", method "get", response_type "json", http_encoding "URLENCODED"'
            f') HEADER () BODY ()')


def statements(orgdb_password: str) -> list[tuple[str, str]]:
    """(phase, sql) in dependency order."""
    pv = package_versions()
    pkgs = sorted({p for p, _ in pv})
    out: list[tuple[str, str]] = []

    # 1. internal inventory over JDBC
    out.append(("sources", 'CREATE DATASOURCE orgdb DESCRIBE BY "Synthetic internal dependency inventory (Postgres)" OPTIONS ('
                           'jdbcdriver "org.postgresql.Driver", jdbcurl "jdbc:postgresql://orgdb:5432/orgdata", '
                           f'username "orgdata", password "{orgdb_password}", fetchsize "1000")'))
    out.append(("sources", "REGISTER DATASOURCE TABLES FROM orgdb"))

    # 2./3. public APIs as REST tables
    out.append(("sources", 'CREATE LIGHTNING DATABASE PYPI_REST DESCRIBE BY "PyPI JSON API per release"'))
    out.append(("sources", 'CREATE LIGHTNING DATABASE DEPSDEV_REST DESCRIBE BY "deps.dev v3 API"'))
    for p, v in pv:
        out.append(("sources", rest_table(f"pypi_{ident(p)}_{ident(v)}", "PYPI_REST", f"https://pypi.org/pypi/{p}/{v}/json")))
        out.append(("sources", rest_table(f"ver_{ident(p)}_{ident(v)}", "DEPSDEV_REST",
                                          f"https://api.deps.dev/v3/systems/pypi/packages/{p}/versions/{v}")))
    for p in pkgs:
        out.append(("sources", rest_table(f"pkg_{ident(p)}", "DEPSDEV_REST", f"https://api.deps.dev/v3/systems/pypi/packages/{p}")))
        out.append(("sources", rest_table(f"proj_{ident(p)}", "DEPSDEV_REST",
                                          "https://api.deps.dev/v3/projects/" + PROJECTS[p].replace("/", "%2F"))))

    # 4. one schema for the agent
    out.append(("views", "CREATE SCHEMASTORE CONTAINER depriskr"))
    for t in INVENTORY:
        out.append(("views", f"CREATE SCHEMASTORE VIEW {t} WITH CONTAINER depriskr AS SELECT * FROM orgdb.{t}"))

    def union(parts: list[str]) -> str:
        return "\nUNION ALL\n".join(parts)

    rel, vul, dver, dadv, dpkg, dproj, dchk = [], [], [], [], [], [], []
    for p, v in pv:
        t = f"pypi_rest.pypi_{ident(p)}_{ident(v)}"
        rel.append(f"SELECT '{p}' AS package, '{v}' AS version, info.summary AS summary, substring(info.license, 1, 80) AS license, "
                   f"info.license_expression AS license_expression, info.requires_python AS requires_python, "
                   f"info.yanked AS yanked, info.yanked_reason AS yanked_reason, info.home_page AS home_page, "
                   f"'https://pypi.org/pypi/{p}/{v}/json' AS source_url FROM {t}")
        vul.append(f"SELECT '{p}' AS package, '{v}' AS version, vuln.id AS vuln_id, concat_ws(',', vuln.aliases) AS aliases, "
                   f"concat_ws(',', vuln.fixed_in) AS fixed_in, substring(vuln.summary, 1, 300) AS summary, vuln.withdrawn AS withdrawn, "
                   f"vuln.link AS link, 'https://pypi.org/pypi/{p}/{v}/json' AS source_url FROM {t} LATERAL VIEW explode(vulnerabilities) AS vuln")
        d = f"depsdev_rest.ver_{ident(p)}_{ident(v)}"
        u = f"https://api.deps.dev/v3/systems/pypi/packages/{p}/versions/{v}"
        dver.append(f"SELECT '{p}' AS package, '{v}' AS version, publishedAt AS published_at, isDefault AS is_default, "
                    f"isDeprecated AS is_deprecated, concat_ws(',', licenses) AS licenses, '{PROJECTS[p]}' AS source_repo, "
                    f"size(advisoryKeys) AS advisory_count, '{u}' AS source_url FROM {d}")
        dadv.append(f"SELECT '{p}' AS package, '{v}' AS version, adv.id AS advisory_id, '{u}' AS source_url FROM {d} LATERAL VIEW explode(advisoryKeys) AS adv")
    for p in pkgs:
        k = f"depsdev_rest.pkg_{ident(p)}"
        dpkg.append(f"SELECT '{p}' AS package, ver.versionKey.version AS version, ver.publishedAt AS published_at, ver.isDefault AS is_default, "
                    f"'https://api.deps.dev/v3/systems/pypi/packages/{p}' AS source_url FROM {k} LATERAL VIEW explode(versions) AS ver")
        j = f"depsdev_rest.proj_{ident(p)}"
        u = "https://api.deps.dev/v3/projects/" + PROJECTS[p].replace("/", "%2F")
        dproj.append(f"SELECT '{PROJECTS[p]}' AS project_id, '{p}' AS package, starsCount AS stars, forksCount AS forks, openIssuesCount AS open_issues, "
                     f"license, scorecard.overallScore AS scorecard_overall, scorecard.date AS scorecard_date, '{u}' AS source_url FROM {j}")
        dchk.append(f"SELECT '{PROJECTS[p]}' AS project_id, '{p}' AS package, chk.name AS check_name, chk.score AS score, "
                    f"substring(chk.reason, 1, 200) AS reason, '{u}' AS source_url FROM {j} LATERAL VIEW explode(scorecard.checks) AS chk")
    for name, parts in [("pypi_release", rel), ("pypi_vulnerability", vul), ("depsdev_version", dver), ("depsdev_advisory", dadv),
                        ("depsdev_package_version", dpkg), ("depsdev_project", dproj), ("depsdev_scorecard_check", dchk)]:
        out.append(("views", f"CREATE SCHEMASTORE VIEW {name} WITH CONTAINER depriskr AS\n{union(parts)}"))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-exists", action="store_true")
    ap.add_argument("--only", choices=["sources", "views"])
    args = ap.parse_args()
    pw = config.env("ORGDB_PASSWORD")
    if not pw:
        print("ORGDB_PASSWORD missing from .env"); return 2
    stmts = [(ph, s) for ph, s in statements(pw) if not args.only or ph == args.only]
    if args.dry_run:
        for ph, s in stmts:
            print(f"-- [{ph}]\n{s.replace(pw, '<redacted>')}\n")
        print(f"{len(stmts)} statements"); return 0
    z = ZetarisREST(timeout=300)
    ok = skipped = failed = 0
    for i, (ph, s) in enumerate(stmts, 1):
        head = s.replace(pw, "<redacted>").split("\n")[0][:110]
        try:
            r = z.run_ddl(s)
            ok += 1; print(f"[{i:3d}/{len(stmts)}] OK    {head}  ({r.elapsed_ms} ms)")
        except DataLayerError as exc:
            msg = str(exc).replace(pw, "<redacted>")
            if args.skip_exists and re.search(r"already exist|AlreadyExists|duplicate", msg, re.I):
                skipped += 1; print(f"[{i:3d}/{len(stmts)}] SKIP  {head}")
            else:
                failed += 1; print(f"[{i:3d}/{len(stmts)}] FAIL  {head}\n        {msg[:300]}")
                if ph == "sources":
                    print("stopping: later statements depend on this one"); break
    print(f"\n{ok} ok, {skipped} skipped, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
