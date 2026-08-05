"""지원 스키마와 작성 세션 저장 계약 및 메모리 구현."""

from __future__ import annotations

from copy import deepcopy
import json
import time
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


class DynamoDBIntakeStore:
    """Lambda 호출 사이에서 스키마와 지원서 세션을 유지하는 운영 저장소."""

    def __init__(
        self, *, table_name: str, region: str, table: Any | None = None
    ) -> None:
        self._table_name = table_name
        self._region = region
        self._table = table

    def _get_table(self) -> Any:
        if self._table is None:
            import boto3

            self._table = boto3.resource("dynamodb", region_name=self._region).Table(
                self._table_name
            )
        return self._table

    def _save(self, record_id: str, record_type: str, record: dict[str, Any]) -> None:
        # JSON 문자열로 저장하면 DynamoDB가 float를 거부하거나 Decimal로 돌려주는
        # 차이를 API 도메인 밖으로 격리할 수 있다.
        self._get_table().put_item(
            Item={
                "record_id": record_id,
                "record_type": record_type,
                "payload": json.dumps(record, ensure_ascii=False),
                "updated_at": int(time.time()),
                "expires_at": int(time.time()) + 60 * 60 * 24 * 30,
            }
        )

    def _get(self, record_id: str) -> dict[str, Any] | None:
        item = self._get_table().get_item(
            Key={"record_id": record_id}, ConsistentRead=True
        ).get("Item")
        if not item:
            return None
        try:
            value = json.loads(str(item.get("payload") or ""))
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None

    def save_schema(self, record: dict[str, Any]) -> None:
        self._save(str(record["schema_id"]), "schema", record)

    def get_schema(self, schema_id: str) -> dict[str, Any] | None:
        return self._get(schema_id)

    def save_application(self, record: dict[str, Any]) -> None:
        self._save(str(record["application_id"]), "application", record)

    def get_application(self, application_id: str) -> dict[str, Any] | None:
        return self._get(application_id)
