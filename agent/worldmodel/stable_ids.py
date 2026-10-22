"""Stable, deterministic, content-addressable ids (port of reference/src/stableIds.ts).

Also hosts the small JavaScript-compatibility helpers (JSON.stringify number and
string formatting, Date ISO parsing/formatting, UTF-16 lengths) that the ids and
canonical JSON depend on, so outputs match the TypeScript reference byte for byte.

``None`` plays the role of TypeScript ``undefined``: canonical_json drops object
members whose value is None (TS drops ``undefined`` members). Inside arrays and
at top level None serializes as ``null`` (TS maps both undefined and null to
``null`` there).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional

# ------------------------------------------------------------------ sha-256


def sha256_hex(text: str) -> str:
    """Hex sha-256 of the UTF-8 encoding (TextEncoder semantics: lone surrogates -> U+FFFD)."""
    try:
        data = text.encode("utf-8")
    except UnicodeEncodeError:
        data = (
            text.encode("utf-16-le", "surrogatepass")
            .decode("utf-16-le", "replace")
            .encode("utf-8")
        )
    return hashlib.sha256(data).hexdigest()


# ------------------------------------------------------------------ JS-compatible JSON


def js_number(x: Any) -> str:
    """Number formatting of JSON.stringify (Number::toString; NaN/Infinity -> null)."""
    if isinstance(x, bool):
        return "true" if x else "false"
    if isinstance(x, int):
        return str(x)
    f = float(x)
    if math.isnan(f) or math.isinf(f):
        return "null"
    if f == 0:
        return "0"
    sign = "-" if f < 0 else ""
    digits, n = _shortest_digits(abs(f))  # f == 0.digits * 10**n
    k = len(digits)
    if k <= n <= 21:
        return sign + digits + "0" * (n - k)
    if 0 < n <= 21:
        return sign + digits[:n] + "." + digits[n:]
    if -6 < n <= 0:
        return sign + "0." + "0" * (-n) + digits
    e = n - 1
    es = ("+" if e >= 0 else "-") + str(abs(e))
    if k == 1:
        return sign + digits + "e" + es
    return sign + digits[0] + "." + digits[1:] + "e" + es


def _shortest_digits(f: float):
    """(digits, n) with f == 0.digits * 10**n, digits the shortest round-trip string."""
    r = repr(f)
    if "e" in r:
        m, _, e = r.partition("e")
        exp = int(e)
    else:
        m, exp = r, 0
    if "." in m:
        ip, fp = m.split(".")
    else:
        ip, fp = m, ""
    if fp == "0":
        fp = ""
    digits = (ip + fp).lstrip("0")
    lead_zeros = len(ip + fp) - len((ip + fp).lstrip("0"))
    n = len(ip) + exp - lead_zeros
    digits = digits.rstrip("0") or "0"
    return digits, n


def js_string(s: str) -> str:
    """JSON.stringify of a string (non-ASCII left raw, controls escaped)."""
    return json.dumps(s, ensure_ascii=False)


def _plain(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        to_dict = getattr(value, "to_dict", None)
        return to_dict() if to_dict else dataclasses.asdict(value)
    if isinstance(value, datetime):
        return to_iso(value)
    return value


def canonical_json(value: Any) -> str:
    """Deterministic JSON: object keys sorted, None members dropped, no spaces."""
    value = _plain(value)
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return js_number(value)
    if isinstance(value, str):
        return js_string(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(canonical_json(v) for v in value) + "]"
    if isinstance(value, dict):
        keys = sorted(k for k in value if value[k] is not None)
        return "{" + ",".join(js_string(str(k)) + ":" + canonical_json(value[k]) for k in keys) + "}"
    raise TypeError(f"canonical_json: unsupported type {type(value).__name__}")


def js_stringify(value: Any) -> str:
    """JSON.stringify(value) without sorting (insertion order), None members dropped."""
    value = _plain(value)
    if isinstance(value, dict):
        return "{" + ",".join(
            js_string(str(k)) + ":" + js_stringify(v) for k, v in value.items() if v is not None
        ) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(js_stringify(v) for v in value) + "]"
    return canonical_json(value)


def strip_none(value: Any) -> Any:
    """Recursive copy with None-valued object members removed (what JSON.stringify writes)."""
    value = _plain(value)
    if isinstance(value, dict):
        return {k: strip_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, (list, tuple)):
        return [strip_none(v) for v in value]
    return value


# ------------------------------------------------------------------ JS Date helpers

_ISO_RE = re.compile(
    r"^([+-]\d{6}|\d{4})(?:-(\d{2})(?:-(\d{2}))?)?"
    r"(?:T(\d{2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?(Z|[+-]\d{2}:\d{2})?)?$"
)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MS = timedelta(milliseconds=1)


def parse_iso_ms(s: str) -> float:
    """Date.parse for ISO-8601 strings; NaN when unparseable.

    Deviation: a date-time without offset is read as UTC (JS reads it as local time).
    """
    m = _ISO_RE.match(s) if isinstance(s, str) else None
    if not m:
        return math.nan
    y, mo, d, hh, mi, ss, frac, tz = m.groups()
    try:
        dt = datetime(
            int(y), int(mo or 1), int(d or 1), int(hh or 0), int(mi or 0), int(ss or 0),
            tzinfo=timezone.utc,
        )
    except ValueError:
        return math.nan
    ms = (dt - _EPOCH) // _MS
    if frac:
        ms += int((frac + "00")[:3])
    if tz and tz != "Z":
        sign = 1 if tz[0] == "+" else -1
        ms -= sign * (int(tz[1:3]) * 60 + int(tz[4:6])) * 60_000
    return ms


def ms_to_iso(ms: float) -> str:
    """new Date(ms).toISOString(); raises ValueError on NaN (JS throws RangeError)."""
    if isinstance(ms, float) and math.isnan(ms):
        raise ValueError("Invalid time value")
    dt = _EPOCH + timedelta(milliseconds=int(ms))
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def to_iso(dt: datetime) -> str:
    """Date.toISOString(): UTC, millisecond precision. Naive datetimes are taken as UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def to_ms(dt: datetime) -> int:
    """Date.getTime()."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - _EPOCH) // _MS


# ------------------------------------------------------------------ UTF-16 helpers


def utf16_units(s: str) -> List[int]:
    """String as UTF-16 code units (JS string indexing / .length)."""
    if all(ord(c) < 0x10000 for c in s):
        return [ord(c) for c in s]
    b = s.encode("utf-16-le", "surrogatepass")
    return [b[i] | (b[i + 1] << 8) for i in range(0, len(b), 2)]


def utf16_key(s: str):
    """Sort key matching JS default Array.prototype.sort (UTF-16 code unit order)."""
    return utf16_units(s)


# ------------------------------------------------------------------ name normalization

# JS \s and String.prototype.trim whitespace set.
_JS_WS = "\t\n\u000b\u000c\r    -     　﻿"
_JS_WS_RUN = re.compile(f"[{_JS_WS}]+")
_JS_WS_EDGE = re.compile(f"^[{_JS_WS}]+|[{_JS_WS}]+$")
_COMBINING = re.compile("[̀-ͯ]")
_ZERO_WIDTH = re.compile("[​-‍﻿]")


def normalize_name(name: str) -> str:
    """NFKD -> strip U+0300..U+036F -> strip zero-width -> lowercase -> collapse ws -> trim."""
    s = unicodedata.normalize("NFKD", name)
    s = _COMBINING.sub("", s)
    s = _ZERO_WIDTH.sub("", s)
    s = s.lower()
    s = _JS_WS_RUN.sub(" ", s)
    return _JS_WS_EDGE.sub("", s)


# ------------------------------------------------------------------ id derivation


def external_id(type: str, system: str, id: str) -> str:
    """Externally keyed entity or context: ``type:system:externalId``."""
    return f"{type}:{system}:{id}"


def entity_id_from_name(type: str, name: str) -> str:
    """``ent_<first 16 hex of sha256(f"{type}:{normalize_name(name)}")>``."""
    return "ent_" + sha256_hex(f"{type}:{normalize_name(name)}")[:16]


def context_id_from_attrs(type: str, attrs: dict, parent: Optional[str] = None) -> str:
    """Attr-keyed context id, hashed WITH the parent."""
    return "ctx_" + sha256_hex(f"{type}:{canonical_json(attrs)}:{parent if parent is not None else ''}")[:16]


def relationship_id(
    from_: str, type: str, to: str, context: Optional[str] = None, valid_from: Optional[str] = None
) -> str:
    """hash(from|type|to|context|validFrom); re-adding the same edge is a no-op."""
    ctx = context if context is not None else ""
    vf = valid_from if valid_from is not None else ""
    return "rel_" + sha256_hex(f"{from_}|{type}|{to}|{ctx}|{vf}")[:16]


def fact_id(
    about: str,
    predicate: str,
    value: Any,
    asserted_at: str,
    source_kind: str,
    source_url: Optional[str] = None,
) -> str:
    """Content-addressed fact id; asserting the identical fact twice is a no-op."""
    url = source_url if source_url is not None else ""
    return "fact_" + sha256_hex(
        f"{about}|{predicate}|{canonical_json(value)}|{asserted_at}|{source_kind}|{url}"
    )[:16]
