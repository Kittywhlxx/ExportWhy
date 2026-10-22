"""Sleep cycle (port of reference/src/sleepCycle.ts; AGENTS.md section 8).

Preview-first: preview() computes the full action plan and mutates nothing;
execute() takes a backup snapshot first and exposes restore(). Guardrails:
superseding/current records, derived/synthesised memories, and relationship
hubs are NEVER archived.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Set

from .embeddings import cosine
from .memory_store import MemoryStoreService
from .models import DAY_MS, DEFAULTS, MemoryRecord, Provenance, SleepPreview, SleepReport
from .trust_ledger import TrustLedgerService

HUB_THRESHOLD = 3


class SleepCycleService:
    def __init__(
        self,
        store: MemoryStoreService,
        ledger: TrustLedgerService,
        clock: Callable[[], int],
        actor: str,
    ) -> None:
        self._store = store
        self._ledger = ledger
        self._clock = clock
        self._actor = actor
        self._backups: Dict[str, Dict[str, Any]] = {}
        self._backup_seq = 0

    def preview(self) -> SleepPreview:
        now = self._clock()
        records = self._store.all()
        referenced: Set[str] = set()
        for m in records:
            referenced.update(m.relatedTo)
            if m.supersedes:
                referenced.add(m.supersedes)
            referenced.update(m.derivedFrom or [])

        to_consolidate = [
            m.id for m in records
            if m.layer == "short_term" and now - m.lastAccessed >= DEFAULTS["consolidationDays"] * DAY_MS
        ]

        def archivable(m: MemoryRecord) -> bool:
            old_enough = now - m.timestamp >= DEFAULTS["archiveMinDays"] * DAY_MS
            low_access = m.accessCount <= DEFAULTS["archiveMaxAccess"]
            unreferenced = m.id not in referenced
            if not (old_enough and low_access and unreferenced):
                return False
            # Guardrails (section 8.2): never archive these three classes.
            if m.supersedes:
                return False  # superseding/current record
            if len(m.derivedFrom or []) > 0:
                return False  # derived/synthesised memory
            if len(m.relatedTo) >= HUB_THRESHOLD:
                return False  # relationship hub
            return True

        to_archive = [m.id for m in records if archivable(m)]

        to_synthesize: List[List[str]] = []
        used: Set[str] = set()
        for i in range(len(records)):
            if records[i].id in used:
                continue
            group = [records[i]]
            for j in range(i + 1, len(records)):
                if records[j].id in used:
                    continue
                if cosine(records[i].embedding, records[j].embedding) >= DEFAULTS["synthesisCosine"]:
                    group.append(records[j])
            if len(group) >= 2:
                used.update(g.id for g in group)
                to_synthesize.append([g.id for g in group])

        return SleepPreview(toConsolidate=to_consolidate, toArchive=to_archive, toSynthesize=to_synthesize)

    def execute(self, preview: Optional[SleepPreview] = None) -> SleepReport:
        plan = preview if preview is not None else self.preview()
        self._backup_seq += 1
        backup_id = f"backup-{self._backup_seq}"
        self._backups[backup_id] = self._store.snapshot_all()
        action_log = [f"backup {backup_id} created"]

        for mid in plan.toConsolidate:
            if self._store.get(mid) is None:
                continue
            self._store.promote(mid, "long_term")
            self._ledger.log(mid, "sleep_consolidate", self._actor, details={"from": "short_term", "to": "long_term"})
            action_log.append(f"consolidated {mid}")

        for mid in plan.toArchive:
            m = self._store.get(mid)
            if m is None:
                continue
            self._ledger.log(
                mid, "sleep_archive", self._actor,
                details={"age": self._clock() - m.timestamp, "accessCount": m.accessCount},
            )
            self._store.delete(mid, "sleep archive")
            action_log.append(f"archived {mid}")

        synthesized = 0
        for group in plan.toSynthesize:
            members = [m for m in (self._store.get(mid) for mid in group) if m is not None]
            if len(members) < 2:
                continue
            tags: Dict[str, None] = {}
            for m in members:
                for t in m.tags:
                    tags[t] = None
            merged = self._store.add(
                "Synthesis: " + " | ".join(m.content for m in members),
                type="factual",
                layer="long_term",
                tags=list(tags),
                confidence=max(m.confidence for m in members),
                source="sleep:synthesis",
                provenance=Provenance(
                    origin="sleep_synthesis", learnedAt=self._clock(),
                    label=f"synthesized from {len(members)} memories",
                ),
                derivedFrom=[m.id for m in members],
            )
            self._ledger.log(merged.id, "sleep_synthesize", self._actor, details={"sources": list(group)})
            for m in members:
                self._store.delete(m.id, "merged by sleep synthesis")
            synthesized += 1
            action_log.append(f"synthesized {merged.id} from {', '.join(group)}")

        self._store.persist()
        return SleepReport(
            backupId=backup_id,
            consolidated=len(plan.toConsolidate),
            archived=len(plan.toArchive),
            synthesized=synthesized,
            actionLog=action_log,
        )

    def restore(self, backup_id: str) -> None:
        backup = self._backups.get(backup_id)
        if backup is None:
            raise KeyError(f"H-MEM: unknown backup {backup_id}")
        self._store.restore_all(backup["records"], backup["seq"])
        self._ledger.log("*", "restore", self._actor, details={"backupId": backup_id})
