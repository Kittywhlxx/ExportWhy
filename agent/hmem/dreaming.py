"""Dreaming (port of reference/src/dreaming.ts; AGENTS.md section 7).

Constructive synthesis with a human approval boundary. Proposals are NEVER
auto-materialised; only approve() turns one into durable memory, with
derivedFrom lineage.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional

from .embeddings import cosine, jaccard
from .memory_store import MemoryStoreService
from .models import DEFAULTS, DreamProposal, MemoryRecord, Provenance
from .trust_ledger import TrustLedgerService


class DreamingService:
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
        self._proposals: Dict[str, DreamProposal] = {}
        self._seq = 0

    # Multi-signal relatedness: content similarity, tag overlap, shared domain/type, entity overlap.
    @staticmethod
    def _relatedness(a: MemoryRecord, b: MemoryRecord) -> float:
        return (
            0.5 * max(0, cosine(a.embedding, b.embedding))
            + 0.2 * jaccard(a.tags, b.tags)
            + 0.1 * (1 if a.domain == b.domain else 0)
            + 0.2 * jaccard(a.entities, b.entities)
        )

    def _cluster(self) -> List[List[MemoryRecord]]:
        records = self._store.all()
        used = set()
        clusters: List[List[MemoryRecord]] = []
        for seed in records:
            if seed.id in used:
                continue
            group = [seed]
            for other in records:
                if other.id == seed.id or other.id in used:
                    continue
                if self._relatedness(seed, other) >= DEFAULTS["dreamRelatedness"]:
                    group.append(other)
            if len(group) >= DEFAULTS["dreamClusterMinSize"]:
                used.update(g.id for g in group)
                clusters.append(group)
        return clusters

    def dream(self) -> List[DreamProposal]:
        created: List[DreamProposal] = []
        cap = DEFAULTS["maxDreamProposalsPerType"]

        def propose(type: str, content: str, derived_from: List[str], suggested_domain: Optional[str] = None) -> None:
            self._seq += 1
            proposal = DreamProposal(
                id=f"dream-{self._seq}", type=type, content=content, derivedFrom=derived_from,
                status="proposed", suggestedDomain=suggested_domain,
            )
            self._proposals[proposal.id] = proposal
            created.append(proposal)
            self._ledger.log(proposal.id, "dream_proposed", self._actor,
                             details={"type": type, "derivedFrom": list(derived_from)})

        # Insights from clusters.
        insights = 0
        for cluster in self._cluster():
            if insights >= cap:
                break
            domains = "/".join(dict.fromkeys(m.domain for m in cluster))
            propose(
                "insight",
                f"Across {len(cluster)} related memories ({domains}): " + " + ".join(m.content for m in cluster),
                [m.id for m in cluster],
            )
            insights += 1

        # Domain suggestions for uncategorised memories.
        domain_count = 0
        for m in self._store.all():
            if domain_count >= cap:
                break
            if m.domain != "general":
                continue
            suggested = self._store.classify_domain(m.content, m.tags, m.type)
            if suggested != "general":
                propose("domain", f'Reclassify "{m.content[:60]}" as {suggested}', [m.id], suggested)
                domain_count += 1

        # Invariants from preference/personal/correction signals.
        invariants = 0
        prefs = [
            m for m in self._store.all()
            if m.type in ("preference", "personal") or "event:user_correction" in m.tags
        ]
        by_domain: Dict[str, List[MemoryRecord]] = {}
        for p in prefs:
            by_domain.setdefault(p.domain, []).append(p)
        for domain, lst in by_domain.items():
            if invariants >= cap:
                break
            if len(lst) < 2:
                continue
            propose(
                "invariant",
                f"Stable pattern in {domain}: " + "; ".join(m.content for m in lst),
                [m.id for m in lst],
            )
            invariants += 1

        return created

    def pending(self) -> List[DreamProposal]:
        return [p for p in self._proposals.values() if p.status == "proposed"]

    def approve(self, proposal_id: str) -> Optional[MemoryRecord]:
        p = self._proposals.get(proposal_id)
        if p is None or p.status != "proposed":
            raise ValueError(f"H-MEM: no pending proposal {proposal_id}")
        p.status = "approved"
        self._ledger.log(proposal_id, "dream_approved", self._actor, details={"type": p.type})

        if p.type == "domain":
            for mid in p.derivedFrom:
                m = self._store.get(mid)
                if m is not None and p.suggestedDomain:
                    self._store.update(mid, {"domain": p.suggestedDomain})
            return None
        # insight -> long-term factual; invariant -> long-term preference. Both carry lineage.
        return self._store.add(
            p.content,
            type="factual" if p.type == "insight" else "preference",
            layer="long_term",
            tags=[f"dream:{p.type}"],
            confidence=0.75,
            source="dreaming",
            provenance=Provenance(origin="dream", learnedAt=self._clock(), label=f"approved {p.type} proposal"),
            derivedFrom=p.derivedFrom,
        )

    def reject(self, proposal_id: str) -> None:
        p = self._proposals.get(proposal_id)
        if p is None or p.status != "proposed":
            raise ValueError(f"H-MEM: no pending proposal {proposal_id}")
        p.status = "rejected"  # logged, not materialised
        self._ledger.log(proposal_id, "dream_rejected", self._actor, details={"type": p.type})
