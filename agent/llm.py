"""Power layer: Meterless Nightshift (OpenAI-compatible runtime) as the primary provider.

Contract (Meterless Runtime provider contract v4, from meterless.ai/engines.html):
  base URL <host>/v1 (default http://127.0.0.1:8088/v1), Authorization: Bearer <runtime token>,
  header `meterless-app: <stable lowercase app id>` on every call, `meterless-scope: user` on chat,
  GET /v1/models decides online/unauthorized, POST /v1/chat/completions, model ids are routing
  modes such as meterless/auto; 180 s timeout; 429 retried after 2 s, 4 s, 8 s.

SAMPLE_MODE (or LLM_STUB=true) swaps in a deterministic stub so the whole loop runs offline.
"""
from __future__ import annotations

import json
import re
import time

from . import config

APP_ID = "exportwhy"


class LLMError(RuntimeError):
    pass


class Nightshift:
    name = "meterless-nightshift"

    def __init__(self):
        from openai import OpenAI  # imported lazily so SAMPLE_MODE needs no network client
        base = (config.env("METERLESS_BASE_URL", "http://127.0.0.1:8088/v1") or "").rstrip("/")
        if not base.endswith("/v1"):
            base += "/v1"
        token = config.env("METERLESS_RUNTIME_TOKEN")
        if not token or token.startswith("your-") or token == "rt-local":
            raise LLMError("METERLESS_RUNTIME_TOKEN is not set (copy it from the Nightshift Console)")
        self.model = config.env("METERLESS_MODEL", "meterless/auto")
        self.client = OpenAI(base_url=base, api_key=token, timeout=180.0, max_retries=0,
                             default_headers={"meterless-app": APP_ID})

    def health(self) -> dict:
        """GET /v1/models decides: 200 with meterless ids = online."""
        models = self.client.models.list()
        ids = [m.id for m in models.data]
        return {"online": any(i.startswith("meterless/") for i in ids), "models": ids[:20], "selected": self.model}

    def chat(self, system: str, user: str, json_mode: bool = False, max_tokens: int = 2000, max_seconds: float | None = None) -> tuple[str, dict]:
        """max_seconds: the triage's remaining wall-clock budget; the request timeout and the retries never exceed it."""
        delays = [2, 4, 8]
        deadline = time.time() + max_seconds if max_seconds else None
        client = self.client.with_options(timeout=max(5.0, min(180.0, max_seconds))) if max_seconds else self.client
        # Nightshift may route to a reasoning model (Nemotron 3 Ultra) that emits its thinking as text before
        # the answer and ignores thinking toggles. A low max_tokens cuts the reply off mid-thought, so the
        # budget stays large enough for ~600 thinking tokens plus the answer; strip_thinking() removes the
        # thinking. The routed model is recorded per call from the compute receipt.
        system = "/no_think " + system
        for attempt in range(4):
            try:
                if deadline and time.time() > deadline:
                    raise LLMError("triage time budget exhausted before the model call")
                resp = client.chat.completions.create(
                    model=self.model, temperature=0.1, max_tokens=max_tokens,
                    messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                    extra_headers={"meterless-scope": "user"})
                text = strip_thinking(resp.choices[0].message.content or "")
                extra = getattr(resp, "model_extra", None) or {}
                receipt = extra.get("meterless") if isinstance(extra, dict) else None
                meta = {"provider": self.name, "model_requested": self.model, "model_served": getattr(resp, "model", None),
                        "routed_model": (receipt or {}).get("routed_model"), "receipt_id": (receipt or {}).get("receipt_id"),
                        "usage": getattr(resp, "usage", None) and resp.usage.model_dump()}
                return text, meta
            except Exception as exc:  # noqa: BLE001 - provider errors are heterogeneous
                status = getattr(exc, "status_code", None)
                if status in (429, 500, 502, 503, 504) and attempt < 3 and not (deadline and time.time() + delays[attempt] > deadline):
                    time.sleep(delays[attempt]); continue           # transient provider/route error, only while the budget allows a retry
                raise LLMError(f"Nightshift request failed: {exc}") from exc
        raise LLMError("unreachable")


class StubLLM:
    """Deterministic stand-in: the roles call it, but all decisions come from agent/roles.py rules.
    It never fabricates facts; it only echoes the structured input it is given."""
    name = "stub"

    def health(self) -> dict:
        return {"online": True, "models": ["stub"], "selected": "stub"}

    def chat(self, system: str, user: str, json_mode: bool = False, max_tokens: int = 0, max_seconds: float | None = None) -> tuple[str, dict]:
        return ("", {"provider": self.name, "model_requested": "stub", "model_served": "stub"})


def get_llm():
    if config.sample_mode() or (config.env("LLM_STUB", "false") or "").lower() in ("1", "true", "yes"):
        return StubLLM()
    return Nightshift()


THINK = re.compile(r"<think>.*?</think>|^.*?</think>", re.S)


def strip_thinking(text: str) -> str:
    """Reasoning models may leak a <think>...</think> block (or its tail) into the content."""
    return THINK.sub("", text).strip()


def extract_json(text: str):
    """Pull the first JSON object/array out of a model reply (handles ```json fences)."""
    if not text:
        return None
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    body = m.group(1) if m else text
    start = min([i for i in (body.find("{"), body.find("[")) if i >= 0], default=-1)
    if start < 0:
        return None
    for end in range(len(body), start, -1):
        try:
            return json.loads(body[start:end])
        except json.JSONDecodeError:
            continue
    return None
