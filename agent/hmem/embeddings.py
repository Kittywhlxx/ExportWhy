"""Deterministic mock embedder (port of reference/src/embeddings.ts).

FNV-1a hash per token into a 64-dim vector, summed and L2-normalised. Pure
function of the input text. JavaScript string semantics are reproduced where
they affect results: ``\\s`` uses the ECMAScript whitespace set, FNV-1a runs
over UTF-16 code units with 32-bit unsigned (Math.imul) arithmetic, and sums
are plain left folds in index order.
"""

from __future__ import annotations

import math
import re
from typing import Iterable, List

DIM = 64

# ECMAScript WhiteSpace + LineTerminator, i.e. what JS `\s` matches (regex char-class body).
JS_WS_CLASS = "\t\n\v\f\r    -     　﻿"
_JS_WS_CHARS = "\t\n\v\f\r   " + "".join(chr(c) for c in range(0x2000, 0x200B)) + \
    "    　﻿"

_NON_TOKEN = re.compile(f"[^a-z0-9{JS_WS_CLASS}]")
_WS_RUN = re.compile(f"[{JS_WS_CLASS}]+")


def js_trim(s: str) -> str:
    """String.prototype.trim()."""
    return s.strip(_JS_WS_CHARS)


def js_len(s: str) -> int:
    """String length in UTF-16 code units, like JS `.length`."""
    return len(s.encode("utf-16-le", "surrogatepass")) // 2


def tokenize(text: str) -> List[str]:
    lowered = _NON_TOKEN.sub(" ", text.lower())
    return [t for t in _WS_RUN.split(lowered) if js_len(t) > 1]


def fnv1a(s: str) -> int:
    h = 0x811C9DC5
    data = s.encode("utf-16-le", "surrogatepass")
    for i in range(0, len(data), 2):
        h ^= data[i] | (data[i + 1] << 8)  # charCodeAt(i)
        h = (h * 0x01000193) & 0xFFFFFFFF  # Math.imul(h, 0x01000193) >>> 0
    return h


def embed(text: str) -> List[float]:
    vec = [0.0] * DIM
    for token in tokenize(text):
        h = fnv1a(token)
        idx = h % DIM
        sign = 1 if (h >> 8) % 2 == 0 else -1
        mag = 1 + ((h >> 16) % 8) / 8
        vec[idx] += sign * mag
    s = 0.0
    for v in vec:
        s += v * v
    norm = math.sqrt(s)
    return vec if norm == 0 else [v / norm for v in vec]


def cosine(a: List[float], b: List[float]) -> float:
    dot = 0.0
    for i in range(len(a)):
        dot += a[i] * b[i]
    return dot  # both vectors are unit-length


def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    sa = set(a)
    sb = set(b)
    if not sa or not sb:
        return 0
    inter = sum(1 for x in sa if x in sb)
    return inter / (len(sa) + len(sb) - inter)
