"""The four roles. Planner -> Retriever -> Evaluator -> Reporter, with the Evaluator sending the
Planner back for more evidence (bounded by MAX_ROUNDS). Rules decide; the LLM drafts SQL and prose.

Evidence classes are the unit of planning: the Planner proposes queries per class, the Evaluator
checks which classes the decision still lacks and whether the sources agree.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

from . import catalog
from .llm import extract_json
from .zetaris import DataLayerError, QueryResult

MAX_ROUNDS = 3
TODAY = dt.date.today().isoformat()

# ------------------------------------------------------------------ task -----------------------

@dataclass
class Task:
    kind: str                      # "request" | "audit" | "ask"
    text: str
    request_id: str | None = None
    service: str | None = None
    package: str | None = None
    version: str | None = None

    def describe(self) -> str:
        if self.kind == "request":
            return f"Evaluate adoption request {self.request_id}"
        if self.kind == "audit":
            return f"Audit the pinned dependencies of service {self.service}"
        return self.text


@dataclass
class Step:
    sid: str
    evidence: str                  # evidence class
    purpose: str
    sql: str
    origin: str = "rules"          # "rules" | "llm"
    result: QueryResult | None = None
    error: str | None = None


# evidence class -> SQL template (ANSI; identical on Zetaris Lightning SQL and SQLite)
TEMPLATES: dict[str, tuple[str, str]] = {
    "request": ("the request itself and who owns the target service",
        "SELECT r.request_id, r.service, r.package, r.requested_version, r.requested_by, r.requested_on, r.reason, r.status, s.owner_team, s.tier "
        "FROM depriskr.adoption_requests r JOIN depriskr.services s ON s.service = r.service WHERE r.request_id = '{request_id}'"),
    "current_pins": ("what the service (and the rest of the organisation) currently pins for this package",
        "SELECT service, package, pinned_version, scope, added_on FROM depriskr.dependencies WHERE package = '{package}' ORDER BY service"),
    "release": ("release metadata on PyPI for the requested version",
        "SELECT package, version, summary, license, license_expression, requires_python, yanked, yanked_reason, source_url "
        "FROM depriskr.pypi_release WHERE package = '{package}' AND version = '{version}'"),
    "vulns_pypi": ("vulnerabilities PyPI reports for that exact release",
        "SELECT vuln_id, aliases, fixed_in, summary, withdrawn, link, source_url FROM depriskr.pypi_vulnerability "
        "WHERE package = '{package}' AND version = '{version}' ORDER BY vuln_id"),
    "advisories_depsdev": ("deps.dev's independent view of the release: advisories, licenses, publish date",
        "SELECT v.package, v.version, v.published_at, v.is_default, v.is_deprecated, v.licenses, v.advisory_count, v.source_repo, v.source_url, a.advisory_id "
        "FROM depriskr.depsdev_version v LEFT JOIN depriskr.depsdev_advisory a ON a.package = v.package AND a.version = v.version "
        "WHERE v.package = '{package}' AND v.version = '{version}' ORDER BY a.advisory_id"),
    "license_policy": ("the organisation's decision for the licenses the two sources report",
        "SELECT license, decision, note FROM depriskr.license_policy ORDER BY license"),
    "exceptions": ("approved risk exceptions for this service and package, with expiry",
        "SELECT exception_id, service, package, advisory_id, approved_by, approved_on, expires_on, reason FROM depriskr.risk_exceptions "
        "WHERE package = '{package}' ORDER BY expires_on"),
    "project_health": ("source-repository health and OpenSSF Scorecard",
        "SELECT p.project_id, p.stars, p.open_issues, p.license, p.scorecard_overall, p.scorecard_date, c.check_name, c.score, p.source_url "
        "FROM depriskr.depsdev_project p LEFT JOIN depriskr.depsdev_scorecard_check c ON c.project_id = p.project_id "
        "WHERE p.package = '{package}' AND c.check_name IN ('Maintained', 'Vulnerabilities', 'Security-Policy', 'Code-Review', 'Dangerous-Workflow') ORDER BY c.check_name"),
    "newer_versions": ("whether a newer default release exists on the registry",
        "SELECT version, published_at, is_default, source_url FROM depriskr.depsdev_package_version WHERE package = '{package}' AND is_default = 1"),
    "audit_pins": ("every pin of the service",
        "SELECT d.service, d.package, d.pinned_version, d.scope, d.added_on, s.owner_team FROM depriskr.dependencies d JOIN depriskr.services s ON s.service = d.service "
        "WHERE d.service = '{service}' ORDER BY d.package"),
    "audit_vulns": ("vulnerabilities for each pinned release, both sources",
        "SELECT d.package, d.pinned_version, v.vuln_id, v.aliases, v.fixed_in, v.withdrawn, v.source_url FROM depriskr.dependencies d "
        "LEFT JOIN depriskr.pypi_vulnerability v ON v.package = d.package AND v.version = d.pinned_version WHERE d.service = '{service}' ORDER BY d.package, v.vuln_id"),
    "audit_advisories": ("deps.dev advisory counts and licenses for each pin",
        "SELECT d.package, d.pinned_version, x.advisory_count, x.licenses, x.is_default, x.source_url FROM depriskr.dependencies d "
        "LEFT JOIN depriskr.depsdev_version x ON x.package = d.package AND x.version = d.pinned_version WHERE d.service = '{service}' ORDER BY d.package"),
    "audit_exceptions": ("exceptions on file for the service",
        "SELECT exception_id, package, advisory_id, approved_by, expires_on, reason FROM depriskr.risk_exceptions WHERE service = '{service}' ORDER BY expires_on"),
}

INITIAL = {"request": ["request", "release", "vulns_pypi", "advisories_depsdev"],
           "audit": ["audit_pins", "audit_vulns", "audit_advisories"]}


# ------------------------------------------------------------------ planner --------------------

class Planner:
    name = "planner"

    def __init__(self, llm, trace):
        self.llm, self.trace = llm, trace
        self.counter = 0

    def plan(self, task: Task, classes: list[str], memory_context: str, reason: str) -> list[Step]:
        params = {"request_id": task.request_id, "service": task.service, "package": task.package, "version": task.version}
        steps = [self._template(c, params) for c in classes]
        drafted = 0
        if self.llm.name != "stub":
            drafted = self._llm_refine(task, steps, memory_context, reason)
        self.trace.step(self.name, "plan", reason=reason, evidence=classes, llm_drafted=drafted,
                        steps=[{"id": s.sid, "evidence": s.evidence, "origin": s.origin, "sql": s.sql} for s in steps])
        return steps

    def _template(self, cls: str, params: dict) -> Step:
        self.counter += 1
        purpose, sql = TEMPLATES[cls]
        return Step(sid=f"q{self.counter}", evidence=cls, purpose=purpose, sql=sql.format(**{k: v or "" for k, v in params.items()}))

    def _llm_refine(self, task: Task, steps: list[Step], memory_context: str, reason: str) -> int:
        """Ask the model to write the SQL for each evidence class; keep a draft only if it is
        read-only, touches only known tables, and still targets the task's package/version."""
        system = ("You write ANSI SQL for a dependency-risk analyst. Only SELECT statements. Only the tables below. "
                  "Reply with a JSON array of {\"id\": step id, \"sql\": \"...\"}.\n" + catalog.schema_card())
        user = (f"Task: {task.describe()} (package={task.package}, version={task.version}, service={task.service}, request_id={task.request_id}).\n"
                f"Why we are planning now: {reason}\n"
                f"Relevant memory from earlier runs:\n{memory_context or '(none)'}\n\nSteps to write SQL for:\n" +
                "\n".join(f"- {s.sid} [{s.evidence}]: {s.purpose}. Reference SQL: {s.sql}" for s in steps))
        text, meta = self.llm.chat(system, user, json_mode=True)
        drafts = extract_json(text) or []
        kept = 0
        by_id = {s.sid: s for s in steps}
        for d in drafts if isinstance(drafts, list) else []:
            s = by_id.get(str(d.get("id")))
            sql = str(d.get("sql", "")).strip().rstrip(";")
            if not s or not sql:
                continue
            ok, why = self._validate(sql, task)
            if ok:
                s.sql, s.origin = sql, "llm"; kept += 1
            else:
                self.trace.step(self.name, "rejected_llm_sql", step=s.sid, why=why, sql=sql[:300])
        self.trace.step(self.name, "llm", **meta)
        return kept

    @staticmethod
    def _validate(sql: str, task: Task) -> tuple[bool, str]:
        if not re.match(r"^\s*select\b", sql, re.I) or re.search(r"\b(insert|update|delete|drop|create|alter|grant)\b", sql, re.I):
            return False, "not a plain SELECT"
        if not catalog.tables_in_sql(sql):
            return False, "no known depriskr table referenced"
        for ref in re.findall(r"\b([a-z_]+)\.([a-z_]+)\b", sql, re.I):
            if ref[0].lower() == catalog.SCHEMA and ref[1].lower() not in catalog.TABLES:
                return False, f"unknown table {ref[1]}"
        if task.package and task.package not in sql:
            return False, "does not reference the task's package"
        return True, ""


# ------------------------------------------------------------------ retriever ------------------

class Retriever:
    name = "retriever"

    def __init__(self, backend, trace):
        self.backend, self.trace = backend, trace
        self.footprint: dict[str, set[str]] = {}       # table -> columns seen in results

    def run(self, steps: list[Step]) -> list[Step]:
        for s in steps:
            try:
                s.result = self.backend.query(s.sql)
                for t in catalog.tables_in_sql(s.sql):
                    self.footprint.setdefault(t, set()).update(s.result.columns)
                self.trace.step(self.name, "query", step=s.sid, evidence=s.evidence, backend=s.result.source, query_id=s.result.query_id,
                                rows=len(s.result.rows), elapsed_ms=s.result.elapsed_ms, tables=catalog.tables_in_sql(s.sql), sql=s.sql)
            except DataLayerError as exc:
                s.error = str(exc)
                self.trace.step(self.name, "query_failed", step=s.sid, evidence=s.evidence, error=s.error[:300], sql=s.sql)
        return steps


# ------------------------------------------------------------------ evaluator ------------------

@dataclass
class Fact:
    key: str
    text: str
    cites: list[dict] = field(default_factory=list)   # {"step":..,"table":..,"column":..,"source_url":..}


@dataclass
class Assessment:
    facts: list[Fact]
    conflicts: list[str]
    missing: list[str]
    verdict: str | None
    reasons: list[str]
    disclosure: dict
    derived: dict


def _ids_of(vuln_rows: list[dict]) -> set[str]:
    out = set()
    for r in vuln_rows:
        if r.get("vuln_id"):
            out.add(r["vuln_id"])
        out.update(x for x in str(r.get("aliases") or "").split(",") if x)
    return out


def _cves(ids: set[str]) -> set[str]:
    return {i for i in ids if i.startswith("CVE-")}


def _cve_or_id(row: dict) -> str:
    cves = sorted(_cves(_ids_of([row])))
    return cves[0] if cves else str(row.get("vuln_id"))


class Evaluator:
    name = "evaluator"

    def __init__(self, trace):
        self.trace = trace

    def assess(self, task: Task, done: dict[str, list[Step]], footprint: dict, rounds: int) -> Assessment:
        rows = {cls: [r for s in steps if s.result for r in s.result.rows] for cls, steps in done.items()}
        cite = {cls: steps[-1].sid for cls, steps in done.items() if steps}
        step_tables = {cls: sorted({t for s in steps for t in catalog.tables_in_sql(s.sql)}) for cls, steps in done.items()}
        facts: list[Fact] = []
        conflicts: list[str] = []
        missing: list[str] = []
        reasons: list[str] = []
        derived: dict = {}

        def fact(key, text, cls, table, column=None, extra=(), url=None):
            """extra: additional (evidence_class, table, column) triples the fact also rests on.
            url: the upstream source for this fact; defaults to the first source_url in the class's rows."""
            url = url or next((r.get("source_url") for r in rows.get(cls, []) if r.get("source_url")), None)
            cites = [{"step": cite.get(cls), "table": f"{catalog.SCHEMA}.{table}", "column": column, "source_url": url}]
            for t in step_tables.get(cls, []):          # JOIN partners count as used too
                if t != table:
                    cites.append({"step": cite.get(cls), "table": f"{catalog.SCHEMA}.{t}", "column": None, "source_url": None})
            for xcls, xtable, xcol in extra:
                if xcls in cite:
                    cites.append({"step": cite.get(xcls), "table": f"{catalog.SCHEMA}.{xtable}", "column": xcol, "source_url": None})
            facts.append(Fact(key, text, cites))
            return facts[-1]

        if task.kind == "request":
            self._assess_request(task, rows, fact, conflicts, missing, reasons, derived)
        else:
            self._assess_audit(task, rows, fact, conflicts, missing, reasons, derived)

        # disclosure: which tables were touched vs which produced a fact
        used = {c["table"].split(".")[1] for f in facts for c in f.cites}
        touched = sorted(footprint)
        unnecessary = sorted(set(touched) - used)
        disclosure = {"tables_accessed": touched, "columns_seen": {t: sorted(c) for t, c in footprint.items()},
                      "tables_used_in_answer": sorted(used), "accessed_but_unused": unnecessary}

        verdict = None if (missing and rounds < MAX_ROUNDS) else derived.get("verdict", "needs-review")
        a = Assessment(facts, conflicts, missing, verdict, reasons, disclosure, derived)
        self.trace.step(self.name, "assess", round=rounds, facts=len(facts), conflicts=conflicts, missing=missing,
                        verdict=verdict, reasons=reasons, disclosure=disclosure)
        return a

    # ---- adoption request
    def _assess_request(self, task, rows, fact, conflicts, missing, reasons, derived):
        req = rows.get("request", [])
        if not req:
            missing.append("request"); derived["verdict"] = "needs-review"; reasons.append("request row not found"); return
        r0 = req[0]
        fact("request", f"{r0['request_id']}: {r0['service']} (team {r0['owner_team']}, tier {r0['tier']}) asks for {r0['package']} {r0['requested_version']}; status {r0['status']}.",
             "request", "adoption_requests", "request_id")

        rel = rows.get("release", [])
        if "release" not in rows:
            missing.append("release")
        elif not rel:
            reasons.append("release not found on PyPI for that version")
            fact("release", f"PyPI has no release {task.package} {task.version}.", "release", "pypi_release", "version")
        else:
            l = rel[0]
            lic = l.get("license_expression") or l.get("license") or "(none declared)"
            fact("release", f"PyPI release {task.package} {task.version}: license {lic}; requires_python {l.get('requires_python')}; yanked={l.get('yanked')}.",
                 "release", "pypi_release", "license_expression")
            derived["pypi_license"] = l.get("license_expression") or l.get("license")
            if str(l.get("yanked")).lower() in ("1", "true"):
                reasons.append(f"release is yanked: {l.get('yanked_reason')}")

        if "vulns_pypi" not in rows:
            missing.append("vulns_pypi")
        else:
            v = rows["vulns_pypi"]
            ids = _ids_of(v); cves = _cves(ids)
            unfixed = sorted({_cve_or_id(r) for r in v if not (r.get("fixed_in") or "").strip()})
            derived["pypi_vuln_ids"], derived["pypi_cves"], derived["unfixed"] = sorted(ids), sorted(cves), unfixed
            fact("vulns_pypi", f"PyPI lists {len(v)} advisory records for {task.package} {task.version} = {len(cves)} distinct CVEs"
                 + (f"; unfixed: {', '.join(unfixed)}" if unfixed else "") + ".", "vulns_pypi", "pypi_vulnerability", "vuln_id")

        if "advisories_depsdev" not in rows:
            missing.append("advisories_depsdev")
        else:
            d = rows["advisories_depsdev"]
            if not d:
                reasons.append("deps.dev has no record of this release")
            else:
                adv = sorted({r["advisory_id"] for r in d if r.get("advisory_id")})
                derived["depsdev_advisories"] = adv
                derived["depsdev_licenses"] = d[0].get("licenses")
                derived["is_default"] = str(d[0].get("is_default")).lower() in ("1", "true")
                fact("advisories_depsdev", f"deps.dev: {task.package} {task.version} published {d[0].get('published_at')}, licenses {d[0].get('licenses')}, "
                     f"{len(adv)} advisory ids, default version={derived['is_default']}.", "advisories_depsdev", "depsdev_version", "advisory_count")
                # cross-source consistency
                if "pypi_vuln_ids" in derived:
                    pyp = set(derived["pypi_vuln_ids"]); dd = set(adv)
                    only_dd = sorted(dd - pyp); only_py = sorted({i for i in pyp if i.startswith(("GHSA", "PYSEC"))} - dd)
                    if only_dd or only_py:
                        conflicts.append(f"advisory sets differ: only deps.dev {only_dd or '-'}; only PyPI {only_py or '-'}")
                    else:
                        fact("corroboration", "PyPI and deps.dev agree on the advisory set for this release.", "advisories_depsdev", "depsdev_advisory", "advisory_id")
                pl = (derived.get("pypi_license") or "").strip(); dl = (derived.get("depsdev_licenses") or "").strip()
                if pl and dl and pl.lower() != dl.lower() and pl.lower() not in dl.lower():
                    conflicts.append(f"license strings differ: PyPI '{pl}' vs deps.dev '{dl}'")

        # evidence the decision needs, depending on what we found so far
        has_vulns = bool(derived.get("pypi_cves") or derived.get("depsdev_advisories"))
        if has_vulns and "exceptions" not in rows:
            missing.append("exceptions")
        if has_vulns and "newer_versions" not in rows:
            missing.append("newer_versions")
        if ("pypi_license" in derived or "depsdev_licenses" in derived) and "license_policy" not in rows:
            missing.append("license_policy")
        if "project_health" not in rows:
            missing.append("project_health")
        if "current_pins" not in rows:
            missing.append("current_pins")

        # evaluate what we have
        if "license_policy" in rows:
            pol = {r["license"].lower(): r for r in rows["license_policy"]}
            cands = [x.strip() for x in (derived.get("depsdev_licenses") or "").split(",") if x.strip()] + ([derived["pypi_license"]] if derived.get("pypi_license") else [])
            decisions = {c: (pol.get(c.lower(), {}).get("decision") or "unknown") for c in cands}
            derived["license_decisions"] = decisions
            fact("license_policy", "License policy: " + (", ".join(f"{k} -> {v}" for k, v in decisions.items()) or "no license to check") + ".",
                 "license_policy", "license_policy", "decision")
            if any(v == "denied" for v in decisions.values()):
                reasons.append("a reported license is denied by policy")
            elif any(v in ("review", "unknown") for v in decisions.values()):
                reasons.append("a reported license needs legal review (not in the allow list)")
        if "exceptions" in rows:
            active, expired = [], []
            for e in rows["exceptions"]:
                if e.get("service") == task.service:
                    (active if str(e.get("expires_on")) >= TODAY else expired).append(e)
            derived["active_exceptions"] = [e["exception_id"] for e in active]; derived["expired_exceptions"] = [e["exception_id"] for e in expired]
            fact("exceptions", f"Exceptions for {task.service}/{task.package}: active {[e['advisory_id'] for e in active] or 'none'}; expired {[e['advisory_id'] for e in expired] or 'none'}.",
                 "exceptions", "risk_exceptions", "expires_on")
        if "newer_versions" in rows and rows["newer_versions"]:
            nv = rows["newer_versions"][0]["version"]; derived["default_version"] = nv
            fact("newer_versions", f"Registry default version is {nv}" + (" (same as requested)." if nv == task.version else f" (requested {task.version})."),
                 "newer_versions", "depsdev_package_version", "is_default")
        if "project_health" in rows and rows["project_health"]:
            p = rows["project_health"][0]; checks = {r["check_name"]: r["score"] for r in rows["project_health"] if r.get("check_name")}
            derived["scorecard"] = p.get("scorecard_overall"); derived["checks"] = checks
            shown = ", ".join(f"{k}={v}" for k, v in checks.items() if v not in (None, ""))
            fact("project_health", f"Source repo {p['project_id']}: OpenSSF Scorecard {p.get('scorecard_overall')} ({str(p.get('scorecard_date') or '')[:10]}); "
                 f"checks {shown or 'n/a'}; open issues {p.get('open_issues')}.",
                 "project_health", "depsdev_project", "scorecard_overall")
            try:
                if float(p.get("scorecard_overall") or 0) < 5:
                    reasons.append("OpenSSF Scorecard below 5")
            except ValueError:
                pass
        if "current_pins" in rows:
            pins = rows["current_pins"]
            mine = [r for r in pins if r.get("service") == task.service]
            fact("current_pins", f"{task.service} currently pins {task.package} {mine[0]['pinned_version']}." if mine else f"{task.service} does not use {task.package} today; "
                 f"{len(pins)} other service pin(s) exist.", "current_pins", "dependencies", "pinned_version")

        # verdict
        unfixed = derived.get("unfixed", [])
        cves = set(derived.get("pypi_cves", []))
        covered = set()
        for e in rows.get("exceptions", []):
            if e.get("service") == task.service and str(e.get("expires_on")) >= TODAY:
                covered.add(e["advisory_id"])
        uncovered = sorted(cves - covered)
        if unfixed:
            derived["verdict"] = "block"; reasons.insert(0, f"unfixed vulnerabilities in the requested version: {', '.join(unfixed)}")
        elif uncovered:
            derived["verdict"] = "block"; reasons.insert(0, f"known CVEs without an active exception: {', '.join(uncovered)}")
        elif any(v == "denied" for v in derived.get("license_decisions", {}).values()):
            derived["verdict"] = "block"
        elif conflicts or any(v in ("review", "unknown") for v in derived.get("license_decisions", {}).values()) or "scorecard" not in derived:
            derived["verdict"] = "needs-review"
        elif derived.get("scorecard") is not None and float(derived["scorecard"]) < 5:
            derived["verdict"] = "approve-with-conditions"; reasons.append("low Scorecard: add to the watch list")
        else:
            derived["verdict"] = "approve"

    # ---- service audit
    def _assess_audit(self, task, rows, fact, conflicts, missing, reasons, derived):
        pins = rows.get("audit_pins", [])
        if not pins:
            missing.append("audit_pins"); derived["verdict"] = "needs-review"; reasons.append("service has no pins or does not exist"); return
        fact("pins", f"{task.service} (team {pins[0]['owner_team']}) pins {len(pins)} packages: " + ", ".join(f"{p['package']} {p['pinned_version']}" for p in pins) + ".",
             "audit_pins", "dependencies", "pinned_version")
        if "audit_vulns" not in rows:
            missing.append("audit_vulns"); return
        by_pkg: dict[str, list[dict]] = {}
        for r in rows["audit_vulns"]:
            if r.get("vuln_id"):
                by_pkg.setdefault(r["package"], []).append(r)
        if "audit_advisories" in rows:
            n_rows = [r for r in rows["audit_advisories"] if r.get("advisory_count") not in (None, "")]
            fact("advisories_depsdev", f"deps.dev covers {len(n_rows)} of {len(pins)} pins; advisory counts: " +
                 ", ".join(f"{r['package']} {r['pinned_version']}={r['advisory_count']}" for r in n_rows) + ".", "audit_advisories", "depsdev_version", "advisory_count")
            for r in rows["audit_advisories"]:
                n_py = len(_cves(_ids_of(by_pkg.get(r["package"], [])))); n_dd = r.get("advisory_count")
                if n_dd not in (None, "") and n_py == 0 and int(n_dd) > 0:
                    conflicts.append(f"{r['package']} {r['pinned_version']}: deps.dev reports {n_dd} advisories, PyPI reports none")
        else:
            missing.append("audit_advisories")
        if by_pkg and "audit_exceptions" not in rows:
            missing.append("audit_exceptions")
        exc = {(e["package"], e["advisory_id"]): e for e in rows.get("audit_exceptions", [])}
        findings = []
        for pkg, vs in by_pkg.items():
            cves = sorted(_cves(_ids_of(vs))); unfixed = sorted({_cve_or_id(v) for v in vs if not (v.get("fixed_in") or "").strip()})
            active = [c for c in cves if (pkg, c) in exc and str(exc[(pkg, c)]["expires_on"]) >= TODAY]
            expired = [c for c in cves if (pkg, c) in exc and str(exc[(pkg, c)]["expires_on"]) < TODAY]
            uncovered = [c for c in cves if c not in active]
            ver = vs[0]["pinned_version"]
            findings.append({"package": pkg, "version": ver, "cves": len(cves), "unfixed": unfixed, "active_exceptions": active, "expired_exceptions": expired, "uncovered": uncovered})
            fact(f"vulns:{pkg}", f"{pkg} {ver}: {len(cves)} CVEs on PyPI; exceptions active {active or 'none'}, expired {expired or 'none'}; uncovered {len(uncovered)}.",
                 "audit_vulns", "pypi_vulnerability", "aliases", url=vs[0].get("source_url"),
                 extra=[("audit_exceptions", "risk_exceptions", "expires_on")] if (active or expired) else ())
        derived["findings"] = findings
        clean = [p["package"] for p in pins if p["package"] not in by_pkg]
        if clean:
            fact("clean", f"No known vulnerabilities on PyPI for: {', '.join(clean)}.", "audit_vulns", "pypi_vulnerability", "vuln_id", url=None)
        worst = [f for f in findings if f["uncovered"] or f["unfixed"]]
        if worst:
            derived["verdict"] = "block"; reasons.insert(0, f"{len(worst)} pinned package(s) have CVEs without an active exception: " + ", ".join(f["package"] for f in worst))
        elif conflicts:
            derived["verdict"] = "needs-review"
        else:
            derived["verdict"] = "approve"


# ------------------------------------------------------------------ reporter -------------------

class Reporter:
    name = "reporter"

    def __init__(self, llm, trace):
        self.llm, self.trace = llm, trace

    def report(self, task: Task, a: Assessment, memory_context: str, rounds: int) -> str:
        lines = [f"# DepRisk report: {task.describe()}", "", f"**Verdict: {a.verdict}**", ""]
        if a.reasons:
            lines += ["Why:"] + [f"- {r}" for r in a.reasons] + [""]
        summary = self._summary(task, a)
        if summary:
            lines += [summary, ""]
        lines += ["## Evidence (each fact cites the table, the query step and the upstream source)"]
        for f in a.facts:
            c = f.cites[0]
            src = f" <{c['source_url']}>" if c.get("source_url") else ""
            lines.append(f"- {f.text}  [{c['table']}.{c.get('column') or '*'} | {c['step']}]{src}")
        if a.conflicts:
            lines += ["", "## Source conflicts (flagged, not silently resolved)"] + [f"- {c}" for c in a.conflicts]
        if a.missing:
            lines += ["", f"## Evidence still missing after {rounds} round(s)"] + [f"- {m}" for m in a.missing]
        d = a.disclosure
        lines += ["", "## Access footprint (disclosure check)",
                  f"- tables accessed: {', '.join(d['tables_accessed']) or 'none'}",
                  f"- tables that contributed to the answer: {', '.join(d['tables_used_in_answer']) or 'none'}",
                  f"- accessed but not needed: {', '.join(d['accessed_but_unused']) or 'none'}"]
        if memory_context:
            lines += ["", "## Memory used (H-MEM)", memory_context]
        return "\n".join(lines)

    def _summary(self, task: Task, a: Assessment) -> str:
        if self.llm.name == "stub":
            return ""
        system = ("You are a careful security analyst. Write 3-5 sentences for an engineering manager. Use ONLY the facts given; "
                  "do not add any claim that is not in the list; mention the verdict and the main reason first.")
        user = f"Verdict: {a.verdict}\nReasons: {a.reasons}\nFacts:\n" + "\n".join(f"- {f.text}" for f in a.facts) + f"\nConflicts: {a.conflicts}"
        text, meta = self.llm.chat(system, user)
        self.trace.step(self.name, "llm", **meta)
        return text.strip()
