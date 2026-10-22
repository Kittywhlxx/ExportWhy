"""Settings. Everything comes from environment variables (.env is loaded once here)."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def sample_mode() -> bool:
    return (env("SAMPLE_MODE", "false") or "false").lower() in ("1", "true", "yes")


ZETARIS_REST_URL = env("ZETARIS_REST_URL", "http://localhost:8888/api/v1.0")
ZETARIS_ORG_ID = env("ZETARIS_ORG_ID", "1")
ZETARIS_API_KEY = env("ZETARIS_API_KEY")
SAMPLE_DB = ROOT / "data" / "sample" / "depriskr_sample.sqlite"
TRACE_DIR = ROOT / "traces"
