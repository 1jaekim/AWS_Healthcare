"""Protocol Parser가 저장한 DynamoDB 기준을 런타임 행 계약으로 변환한다."""

from __future__ import annotations

import json
import re
import time
from typing import Any


_OPERATOR_ALIASES = {"==": "=", "eq": "=", "gte": ">=", "lte": "<="}


def _field_key(value: Any) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")
    return key


def _narrative_field(criterion_type: str, criterion_id: str) -> str:
    return f"criterion_{criterion_type.lower()}_{_field_key(criterion_id)}"


def _source_identity(item: dict[str, Any]) -> str:
    """스크린샷과 상세 문서가 같은 수집 공고인지 판별하는 안정 키."""
    source_key = str(item.get("source_key") or "")
    match = re.search(r"(kct_[a-z0-9]+)", source_key.lower())
    return match.group(1) if match else str(item.get("trial_id") or "")


def normalize_operator(value: Any) -> str:
    operator = str(value or "").strip().lower()
    return _OPERATOR_ALIASES.get(operator, operator)


def _decoded(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    return [item for item in (value or []) if isinstance(item, dict)]


def _bounds(value: Any, operator: str) -> tuple[str, str]:
    if operator != "between":
        return (json.dumps(value, ensure_ascii=False) if isinstance(value, list) else str(value or ""), "")
    if isinstance(value, list) and len(value) >= 2:
        return str(value[0]), str(value[1])
    normalized = re.sub(r"(?<=\d)\s*[-–~]\s*(?=\d)", ",", str(value or ""))
    parts = [part.strip() for part in normalized.split(",")]
    return (parts[0], parts[1]) if len(parts) >= 2 else ("", "")


def flatten_protocol_item(item: dict[str, Any]) -> list[dict[str, str]]:
    """신규 `criteria` 또는 기존 inclusion/exclusion blob을 동일 행으로 만든다."""
    if item.get("criteria"):
        rows = _decoded(item["criteria"])
        normalized: list[dict[str, str]] = []
        for row in rows:
            criterion_id = str(row.get("criterion_id") or "")
            criterion_type = str(row.get("criterion_type") or "").upper()
            field = _field_key(row.get("field"))
            operator = normalize_operator(row.get("operator"))
            value_low = str(row.get("value_low") or "")
            if field == "eligibility_note":
                field = _narrative_field(criterion_type, criterion_id)
                operator = "="
                value_low = "true" if criterion_type == "INCLUSION" else "false"
            normalized.append(
                {
                    "criterion_id": criterion_id,
                    "criterion_type": criterion_type,
                    "field": field,
                    "operator": operator,
                    "value_low": value_low,
                    "value_high": str(row.get("value_high") or ""),
                    "unit": str(row.get("unit") or ""),
                    "time_window_days": str(row.get("time_window_days") or ""),
                    "label": str(row.get("label") or row.get("description") or value_low),
                }
            )
        return [
            row
            for row in normalized
            if row["criterion_id"]
            and row["criterion_type"] in {"INCLUSION", "EXCLUSION"}
            and row["field"]
            and row["operator"]
        ]

    rows: list[dict[str, str]] = []
    for key, criterion_type in (
        ("inclusion_criteria", "INCLUSION"),
        ("exclusion_criteria", "EXCLUSION"),
    ):
        for index, raw in enumerate(_decoded(item.get(key)), start=1):
            structured = raw.get("structured") or {}
            if not structured:
                description = str(raw.get("description") or "").strip()
                if description:
                    criterion_id = str(
                        raw.get("id") or f"{criterion_type[:3]}-{index:03d}"
                    )
                    rows.append(
                        {
                            "criterion_id": criterion_id,
                            "criterion_type": criterion_type,
                            "field": _narrative_field(criterion_type, criterion_id),
                            "operator": "=",
                            "value_low": "true" if criterion_type == "INCLUSION" else "false",
                            "value_high": "",
                            "unit": "boolean",
                            "time_window_days": "",
                            "label": description,
                        }
                    )
                continue
            operator = normalize_operator(structured.get("operator"))
            low, high = _bounds(structured.get("value"), operator)
            rows.append(
                {
                    "criterion_id": str(raw.get("id") or f"{criterion_type[:3]}-{index:03d}"),
                    "criterion_type": criterion_type,
                    "field": _field_key(structured.get("parameter")),
                    "operator": operator,
                    "value_low": low,
                    "value_high": high,
                    "unit": str(structured.get("unit") or ""),
                    "time_window_days": str(structured.get("time_window_days") or ""),
                    "label": str(raw.get("description") or structured.get("parameter") or ""),
                }
            )
    return [row for row in rows if row["field"] and row["operator"]]


class DynamoDBCriteriaRepository:
    """승인된 최신 Protocol Parser 결과만 반환하는 Criteria Source."""

    def __init__(self, *, table_name: str, region: str, table: Any | None = None) -> None:
        self._table_name = table_name
        self._region = region
        self._table = table

    def rows_for(self, trial_id: str) -> list[dict[str, str]]:
        from boto3.dynamodb.conditions import Key

        if self._table is None:
            import boto3

            self._table = boto3.resource("dynamodb", region_name=self._region).Table(
                self._table_name
            )
        response = self._table.query(
            KeyConditionExpression=Key("trial_id").eq(trial_id)
        )
        approved = [
            item
            for item in response.get("Items", [])
            if str(item.get("status", "")).strip().lower() in {"approved", "active"}
        ]
        if not approved:
            return []
        latest = max(approved, key=lambda item: int(item.get("updated_at") or 0))
        return flatten_protocol_item(latest)

    def _approved_item(self, trial_id: str) -> dict[str, Any] | None:
        from boto3.dynamodb.conditions import Key

        table = self._get_table()
        response = table.query(KeyConditionExpression=Key("trial_id").eq(trial_id))
        approved = [
            item
            for item in response.get("Items", [])
            if str(item.get("status", "")).strip().lower() in {"approved", "active"}
        ]
        return max(approved, key=lambda item: int(item.get("updated_at") or 0)) if approved else None

    def _get_table(self) -> Any:
        if self._table is None:
            import boto3

            self._table = boto3.resource("dynamodb", region_name=self._region).Table(
                self._table_name
            )
        return self._table

    def has_trial(self, trial_id: str) -> bool:
        return self._approved_item(trial_id) is not None

    def get_trial(self, trial_id: str) -> dict[str, Any] | None:
        item = self._approved_item(trial_id)
        return self._summary(item) if item else None

    def list_trials(self) -> list[dict[str, Any]]:
        """승인 공고 전체를 페이지 끝까지 읽고 시험별 최신 버전만 반환한다."""
        table = self._get_table()
        items: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {}
        while True:
            response = table.scan(**kwargs)
            items.extend(response.get("Items", []))
            key = response.get("LastEvaluatedKey")
            if not key:
                break
            kwargs["ExclusiveStartKey"] = key
        latest: dict[str, dict[str, Any]] = {}
        for item in items:
            if str(item.get("status", "")).lower() not in {"approved", "active"}:
                continue
            identity = _source_identity(item)
            previous = latest.get(identity)
            quality = (len(flatten_protocol_item(item)), int(item.get("updated_at") or 0))
            previous_quality = (
                len(flatten_protocol_item(previous)),
                int(previous.get("updated_at") or 0),
            ) if previous is not None else (-1, -1)
            if identity and quality > previous_quality:
                latest[identity] = item
        return [
            self._summary(item)
            for item in sorted(latest.values(), key=lambda value: str(value.get("trial_id") or ""))
        ]

    def trial_catalog(self, trial_ids: list[str]) -> dict[str, dict[str, Any]]:
        return {
            trial_id: trial
            for trial_id in trial_ids
            if (trial := self.get_trial(trial_id)) is not None
        }

    def trial_criteria(self, trial_id: str) -> list[dict[str, Any]]:
        return flatten_protocol_item(self._approved_item(trial_id) or {})

    def list_for_review(self, *, status: str = "pending_review", limit: int = 100) -> list[dict[str, Any]]:
        """상태 인덱스로 관리자 검토 대상을 최신순 조회한다."""
        from boto3.dynamodb.conditions import Key

        response = self._get_table().query(
            IndexName="status-index",
            KeyConditionExpression=Key("status").eq(status),
            ScanIndexForward=False,
            Limit=limit,
        )
        return [self._review_item(item) for item in response.get("Items", [])]

    def decide_review(
        self,
        *,
        trial_id: str,
        source_key: str,
        decision: str,
        reviewed_by: str,
        note: str = "",
    ) -> dict[str, Any] | None:
        """대기 중인 정확한 버전 하나만 원자적으로 승인 또는 반려한다."""
        table = self._get_table()
        current = table.get_item(
            Key={"trial_id": trial_id, "source_key": source_key},
            ConsistentRead=True,
        ).get("Item")
        if current is None:
            return None
        if str(current.get("status") or "").lower() != "pending_review":
            raise ReviewAlreadyDecided("이미 검토가 완료된 공고입니다.")
        if decision == "approved":
            if str(current.get("trial_title") or "").strip().upper() in {"", "UNKNOWN"}:
                raise InvalidApproval("공고 제목이 확인되지 않아 승인할 수 없습니다.")
            rows = flatten_protocol_item(current)
            if not rows:
                raise InvalidApproval("구조화된 선정·제외 기준이 없어 승인할 수 없습니다.")
            if len(rows) < 3:
                raise InvalidApproval(
                    "구조화 기준이 3개 미만입니다. 상세 기준을 다시 수집한 뒤 승인해 주세요."
                )

        now = int(time.time())
        try:
            response = table.update_item(
                Key={"trial_id": trial_id, "source_key": source_key},
                UpdateExpression=(
                    "SET #status = :decision, reviewed_at = :now, reviewed_by = :actor, "
                    "review_note = :note, updated_at = :now"
                ),
                ConditionExpression="#status = :pending",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":decision": decision,
                    ":pending": "pending_review",
                    ":now": now,
                    ":actor": reviewed_by,
                    ":note": note,
                },
                ReturnValues="ALL_NEW",
            )
        except Exception as exc:  # boto3 예외 타입은 선택 의존성이라 런타임 판별
            if getattr(exc, "response", {}).get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise ReviewAlreadyDecided("이미 검토가 완료된 공고입니다.") from exc
            raise
        return self._review_item(response["Attributes"])

    @staticmethod
    def _review_item(item: dict[str, Any]) -> dict[str, Any]:
        rows = flatten_protocol_item(item)
        raw_quality = item.get("quality_report")
        if isinstance(raw_quality, str):
            try:
                quality_report = json.loads(raw_quality)
            except json.JSONDecodeError:
                quality_report = {}
        elif isinstance(raw_quality, dict):
            quality_report = raw_quality
        else:
            quality_report = {}
        return {
            "trial_id": str(item.get("trial_id") or ""),
            "source_key": str(item.get("source_key") or ""),
            "status": str(item.get("status") or ""),
            "trial_title": str(item.get("trial_title") or ""),
            "condition": str(item.get("condition") or ""),
            "phase": str(item.get("phase") or ""),
            "intervention": str(item.get("intervention") or ""),
            "criteria": rows,
            "criteria_count": len(rows),
            "created_at": int(item.get("created_at") or 0),
            "updated_at": int(item.get("updated_at") or 0),
            "reviewed_at": int(item.get("reviewed_at") or 0) or None,
            "reviewed_by": str(item.get("reviewed_by") or "") or None,
            "review_note": str(item.get("review_note") or "") or None,
            "failure_reason": str(item.get("failure_reason") or "") or None,
            "quality_report": quality_report,
        }

    @staticmethod
    def _summary(item: dict[str, Any]) -> dict[str, Any]:
        rows = flatten_protocol_item(item)
        return {
            "trial_id": str(item.get("trial_id") or ""),
            "trial_name": str(item.get("trial_title") or item.get("trial_id") or ""),
            "description": str(item.get("condition") or ""),
            # 홈 필터는 약물명마다 칩을 만들지 않고 대상 질환/분야로 묶는다.
            "purpose": str(item.get("condition") or "미분류"),
            "synthetic_trial": False,
            "criteria_count": len(rows),
        }


class LocalTrialCatalog:
    """기존 CSV 데이터셋을 운영 공고 저장소와 같은 계약으로 감싼다."""

    def __init__(self, repository: Any) -> None:
        self._repository = repository

    def has_trial(self, trial_id: str) -> bool:
        return trial_id in self._repository.trials

    def get_trial(self, trial_id: str) -> dict[str, Any] | None:
        row = self._repository.trials.get(trial_id)
        return self._repository.trial_summary(row) if row else None

    def list_trials(self) -> list[dict[str, Any]]:
        return self._repository.list_trials()

    def trial_catalog(self, trial_ids: list[str]) -> dict[str, dict[str, Any]]:
        return {
            trial_id: self._repository.trials[trial_id]
            for trial_id in trial_ids
            if trial_id in self._repository.trials
        }

    def trial_criteria(self, trial_id: str) -> list[dict[str, Any]]:
        return self._repository.trial_criteria(trial_id)


class InvalidApproval(ValueError):
    pass


class ReviewAlreadyDecided(RuntimeError):
    pass


__all__ = [
    "DynamoDBCriteriaRepository",
    "LocalTrialCatalog",
    "flatten_protocol_item",
    "normalize_operator",
    "InvalidApproval",
    "ReviewAlreadyDecided",
]
