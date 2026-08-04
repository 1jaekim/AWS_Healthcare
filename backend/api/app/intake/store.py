"""지원 스키마와 작성 세션 저장 계약 및 메모리 구현."""

from __future__ import annotations

from copy import deepcopy
from threading import RLock
from typing import Any


class IntakeStore:
    """DynamoDB로 교체 가능한 최소 저장 계약의 인메모리 구현."""

    def __init__(self) -> None:
        self._schemas: dict[str, dict[str, Any]] = {}
        self._applications: dict[str, dict[str, Any]] = {}
        self._lock = RLock()

    def save_schema(self, record: dict[str, Any]) -> None:
        with self._lock:
            self._schemas[str(record["schema_id"])] = deepcopy(record)

    def get_schema(self, schema_id: str) -> dict[str, Any] | None:
        with self._lock:
            value = self._schemas.get(schema_id)
            return deepcopy(value) if value else None

    def save_application(self, record: dict[str, Any]) -> None:
        with self._lock:
            self._applications[str(record["application_id"])] = deepcopy(record)

    def get_application(self, application_id: str) -> dict[str, Any] | None:
        with self._lock:
            value = self._applications.get(application_id)
            return deepcopy(value) if value else None
