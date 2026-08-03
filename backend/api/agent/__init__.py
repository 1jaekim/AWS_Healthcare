"""루트 agent 패키지를 backend/api 실행 경로에서 찾기 위한 호환 레이어."""

from __future__ import annotations

from pathlib import Path

_ROOT_AGENT = Path(__file__).resolve().parents[3] / "agent"
if _ROOT_AGENT.exists():
    __path__.append(str(_ROOT_AGENT))
