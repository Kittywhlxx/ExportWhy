"""Entity resolution (port of reference/src/resolve.ts). Banded thresholds:

  similarity >= 0.85            -> auto-merge   (0.85 EXACTLY is auto-merge)
  0.82 <= similarity < 0.85     -> operator review queue (0.82 EXACTLY queues)
  similarity < 0.82             -> new entity

Similarity is Levenshtein-based, 1 - distance / max(lenA, lenB), on NORMALIZED
names. Lengths and edits are over UTF-16 code units, as in JavaScript.
"""

from __future__ import annotations

from typing import Iterable, Sequence, Union

from .stable_ids import normalize_name, utf16_units


def levenshtein(a: Union[str, Sequence], b: Union[str, Sequence]) -> int:
    if a == b:
        return 0
    if isinstance(a, str):
        a = utf16_units(a)
    if isinstance(b, str):
        b = utf16_units(b)
    m, n = len(a), len(b)
    if m == 0:
        return n
    if n == 0:
        return m
    prev = list(range(n + 1))
    curr = [0] * (n + 1)
    for i in range(1, m + 1):
        curr[0] = i
        for j in range(1, n + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            curr[j] = min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost)
        prev, curr = curr, prev
    return prev[n]


def name_similarity(raw_a: str, raw_b: str) -> float:
    a = utf16_units(normalize_name(raw_a))
    b = utf16_units(normalize_name(raw_b))
    if len(a) == 0 and len(b) == 0:
        return 1
    max_len = max(len(a), len(b))
    if max_len == 0:
        return 1
    return 1 - levenshtein(a, b) / max_len


def decide(similarity: float, auto_merge_at: float = 0.85, review_lower: float = 0.82) -> str:
    """'auto-merge' | 'review' | 'new'. Both boundaries are inclusive (>=)."""
    if similarity >= auto_merge_at:
        return "auto-merge"
    if similarity >= review_lower:
        return "review"
    return "new"


def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    """Entity-overlap Jaccard; 0 when either side is empty (clustering gate >= 0.5)."""
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0
    inter = len(sa & sb)
    return inter / (len(sa) + len(sb) - inter)
