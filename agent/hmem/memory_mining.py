"""Acquisition pipeline (port of reference/src/memoryMining.ts; AGENTS.md section 4).

The model-free fallback path is the DEFAULT: heuristic sentence extraction
preserves capture continuity with no model available. An optional ``miner_fn``
hook (synchronous ``str -> str`` here; async in the reference) accepts an
extraction prompt and returns a strict JSON array of memory strings; its output
is parsed robustly (markdown fences stripped, format noise tolerated).
"""

from __future__ import annotations

import json
import re
from typing import Callable, Dict, List, Optional

from .embeddings import JS_WS_CLASS, js_len, js_trim
from .memory_store import MemoryStoreService
from .models import MemoryRecord, Provenance

EVENT_TYPE_MAP: Dict[str, str] = {
    "chat_message": "general",
    "user_correction": "preference",
    "file_save": "factual",
    "file_open": "general",
    "file_edit": "factual",
    "model_response": "factual",
    "plan_completion": "factual",
}

SIGNAL = re.compile(
    r"\b(prefer|always|never|decided|chose|choose|use|because|instead|must|should|important|remember)\b",
    re.I | re.ASCII,
)
_SENTENCE_SPLIT = re.compile(f"(?<=[.!?])[{JS_WS_CLASS}]+|\n+")
_FENCE_OPEN = re.compile(f"^```(?:json)?[{JS_WS_CLASS}]*", re.I)
_FENCE_CLOSE = re.compile(f"```[{JS_WS_CLASS}]*\\Z")


class MemoryMiningService:
    def __init__(
        self,
        store: MemoryStoreService,
        clock: Callable[[], int],
        miner_fn: Optional[Callable[[str], str]] = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._miner_fn = miner_fn

    def mine_interaction(
        self, event: str, text: str, *, chatId: Optional[str] = None, layer: Optional[str] = None
    ) -> List[MemoryRecord]:
        candidates = self._mine_with_model(event, text) if self._miner_fn else self.mine_model_free(text)
        default_layer = "working" if self._miner_fn else "short_term"
        return [
            self._store.add(
                content,
                type=EVENT_TYPE_MAP.get(event),
                layer=layer if layer is not None else default_layer,
                tags=[f"event:{event}"],
                source=f"interaction:{event}",
                provenance=Provenance(origin=event, learnedAt=self._clock(), label=f"mined from {event}"),
                chatId=chatId,
            )
            for content in candidates
        ]

    def mine_document(self, file_name: str, text: str) -> List[MemoryRecord]:
        truncated = text[:8000]  # safe token window
        candidates = self._mine_with_model("file_save", truncated) if self._miner_fn else self.mine_model_free(truncated)
        return [
            self._store.add(
                content,
                type="factual",
                layer="working",
                tags=["document", f"file:{file_name}"],
                source=f"document:{file_name}",
            )
            for content in candidates
        ]

    # 4.3: no-model fallback. Concise direct summaries as short-term memories.
    def mine_model_free(self, text: str) -> List[str]:
        parts = [js_trim(s) for s in _SENTENCE_SPLIT.split(text)]
        return [s for s in parts if 20 <= js_len(s) <= 300 and SIGNAL.search(s)][:10]

    def _mine_with_model(self, event: str, text: str) -> List[str]:
        prompt = f"Extract durable memories from this {event} as a strict JSON array of strings:\n\n{text}"
        raw_response = self._miner_fn(prompt)  # type: ignore[misc]
        return parse_strict_json_array(raw_response)


def _reject_constant(name: str):
    raise ValueError(f"non-JSON constant {name}")  # JSON.parse rejects NaN/Infinity


# Robust parsing: strip markdown fences, tolerate minor noise (section 4.1 step 3).
def parse_strict_json_array(raw: str) -> List[str]:
    cleaned = js_trim(_FENCE_CLOSE.sub("", _FENCE_OPEN.sub("", js_trim(raw), count=1), count=1))
    start = cleaned.find("[")
    end = cleaned.rfind("]")
    if start != -1 and end > start:
        cleaned = cleaned[start:end + 1]
    try:
        parsed = json.loads(cleaned, parse_constant=_reject_constant)
    except ValueError:
        return []
    if not isinstance(parsed, list):
        return []
    return [x for x in parsed if isinstance(x, str) and js_len(js_trim(x)) >= 5]
