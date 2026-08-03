"""Streamlit UI 설정.

백엔드 주소와 타임아웃을 환경 변수로 받는다. 기본값은 로컬 uvicorn 이다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_str(name: str, default: str) -> str:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip()


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class AppSettings:
    """UI 전역 설정."""

    api_base_url: str = field(
        default_factory=lambda: _env_str(
            "SCREENING_API_BASE_URL", "http://127.0.0.1:8000"
        ).rstrip("/")
    )
    request_timeout: float = field(
        default_factory=lambda: _env_float("SCREENING_API_TIMEOUT", 60.0)
    )
    default_actor: str = field(
        default_factory=lambda: _env_str("SCREENING_ACTOR", "streamlit-ui")
    )
    cache_ttl_seconds: int = field(
        default_factory=lambda: int(_env_float("SCREENING_CACHE_TTL", 60))
    )


settings = AppSettings()
