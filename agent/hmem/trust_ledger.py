"""Append-only trust ledger (port of reference/src/trustLedger.ts; AGENTS.md section 9.1).

Entries are never mutated or deleted. history() reconstructs a memory's full
change record from the previousState/newState snapshots carried on each entry.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable, Dict, List, Optional

from .models import LedgerEntry


class TrustLedgerService:
    def __init__(self, clock: Callable[[], int], persist_dir: Optional[str] = None) -> None:
        self._clock = clock
        self._entries: List[LedgerEntry] = []
        self._file: Optional[str] = None
        if persist_dir:
            self._file = os.path.join(os.fspath(persist_dir), "ledger.jsonl")
            if os.path.exists(self._file):
                with open(self._file, "r", encoding="utf-8") as fh:
                    for line in fh.read().split("\n"):
                        if line:
                            self._entries.append(LedgerEntry.from_dict(json.loads(line)))

    def log(
        self,
        memory_id: str,
        action: str,
        actor: str,
        *,
        previousState: Optional[Dict[str, Any]] = None,
        newState: Optional[Dict[str, Any]] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> LedgerEntry:
        entry = LedgerEntry(
            memoryId=memory_id, action=action, actor=actor, timestamp=self._clock(),
            previousState=previousState, newState=newState, details=details,
        )
        self._entries.append(entry)
        if self._file:
            with open(self._file, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry.to_dict(), separators=(",", ":"), ensure_ascii=False) + "\n")
        return entry

    def history(self, memory_id: str) -> List[LedgerEntry]:
        return [e for e in self._entries if e.memoryId == memory_id]

    def all(self) -> List[LedgerEntry]:
        return list(self._entries)

    def stats(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for e in self._entries:
            out[e.action] = out.get(e.action, 0) + 1
        return out
