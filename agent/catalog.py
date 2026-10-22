"""What the agent is allowed to know about the data layer: the `depriskr` schema.

This card is given to the Planner. It is the same in Zetaris mode (schemastore container
`depriskr`, see data/zetaris/register.py) and SAMPLE_MODE (SQLite attached as `depriskr`).
"""
from __future__ import annotations

SCHEMA = "depriskr"

TABLES: dict[str, dict] = {
    "services": {"system": "orgdb (internal inventory, Postgres)",
                 "columns": ["service", "owner_team", "tier", "language"],
                 "about": "services the organisation runs"},
    "dependencies": {"system": "orgdb (internal inventory, Postgres)",
                     "columns": ["service", "package", "pinned_version", "scope", "added_on"],
                     "about": "which package version each service pins (direct or transitive)"},
    "license_policy": {"system": "orgdb (internal inventory, Postgres)",
                       "columns": ["license", "decision", "note"],
                       "about": "allowed / review / denied licenses"},
    "risk_exceptions": {"system": "orgdb (internal inventory, Postgres)",
                        "columns": ["exception_id", "service", "package", "advisory_id", "approved_by", "approved_on", "expires_on", "reason"],
                        "about": "approved exceptions for known advisories, with expiry"},
    "adoption_requests": {"system": "orgdb (internal inventory, Postgres)",
                          "columns": ["request_id", "service", "package", "requested_version", "requested_by", "requested_on", "reason", "status"],
                          "about": "open requests to adopt or upgrade a package"},
    "pypi_release": {"system": "PyPI JSON API (public, per release)",
                     "columns": ["package", "version", "summary", "license", "license_expression", "requires_python", "yanked", "yanked_reason", "home_page", "source_url"],
                     "about": "release metadata as published on PyPI"},
    "pypi_vulnerability": {"system": "PyPI JSON API (public, OSV-fed)",
                           "columns": ["package", "version", "vuln_id", "aliases", "fixed_in", "summary", "withdrawn", "link", "source_url"],
                           "about": "known vulnerabilities affecting that exact release; aliases are comma-separated CVE/GHSA/PYSEC ids"},
    "depsdev_version": {"system": "deps.dev v3 API (public, Google)",
                        "columns": ["package", "version", "published_at", "is_default", "is_deprecated", "licenses", "source_repo", "advisory_count", "source_url"],
                        "about": "independent view of the release: detected licenses, advisory count, source repo"},
    "depsdev_advisory": {"system": "deps.dev v3 API (public, Google)",
                         "columns": ["package", "version", "advisory_id", "source_url"],
                         "about": "advisory ids deps.dev associates with the release"},
    "depsdev_package_version": {"system": "deps.dev v3 API (public, Google)",
                                "columns": ["package", "version", "published_at", "is_default", "source_url"],
                                "about": "all published versions of a package; is_default marks the current default"},
    "depsdev_project": {"system": "deps.dev v3 API (public, OpenSSF Scorecard)",
                        "columns": ["project_id", "package", "stars", "forks", "open_issues", "license", "scorecard_overall", "scorecard_date", "source_url"],
                        "about": "source-repository health and OpenSSF Scorecard overall score"},
    "depsdev_scorecard_check": {"system": "deps.dev v3 API (public, OpenSSF Scorecard)",
                                "columns": ["project_id", "package", "check_name", "score", "reason", "source_url"],
                                "about": "individual Scorecard checks (Maintained, Vulnerabilities, Code-Review, ...)"},
}


def schema_card() -> str:
    lines = [f"Schema `{SCHEMA}` (query tables as {SCHEMA}.<table>; read-only SQL; ANSI SELECT/JOIN only):"]
    for name, t in TABLES.items():
        lines.append(f"- {SCHEMA}.{name} [{t['system']}]: {t['about']}. Columns: {', '.join(t['columns'])}")
    return "\n".join(lines)


def tables_in_sql(sql: str) -> list[str]:
    import re
    found = re.findall(rf"\b{SCHEMA}\.([a-z_]+)", sql, flags=re.I)
    return sorted({f.lower() for f in found if f.lower() in TABLES})
