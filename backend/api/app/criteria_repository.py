"""Protocol Parser가 저장한 DynamoDB 기준을 런타임 행 계약으로 변환한다."""

from __future__ import annotations

import json
from typing import Any


_OPERATOR_ALIASES = {"==": "=", "eq": "=", "gte": ">=", "lte": "<="}


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
    parts = [part.strip() for part in str(value or "").replace("~", ",").split(",")]
    return (parts[0], parts[1]) if len(parts) >= 2 else ("", "")


def flatten_protocol_item(item: dict[str, Any]) -> list[dict[str, str]]:
    """신규 `criteria` 또는 기존 inclusion/exclusion blob을 동일 행으로 만든다."""
    if item.get("criteria"):
        rows = _decoded(item["criteria"])
        normalized: list[dict[str, str]] = []
        for row in rows:
            normalized.append(
                {
                    "criterion_id": str(row.get("criterion_id") or ""),
                    "criterion_type": str(row.get("criterion_type") or "").upper(),
                    "field": str(row.get("field") or "").strip().lower(),
                    "operator": normalize_operator(row.get("operator")),
                    "value_low": str(row.get("value_low") or ""),
                    "value_high": str(row.get("value_high") or ""),
                    "unit": str(row.get("unit") or ""),
                    "time_window_days": str(row.get("time_window_days") or ""),
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
            operator = normalize_operator(structured.get("operator"))
            low, high = _bounds(structured.get("value"), operator)
            rows.append(
                {
                    "criterion_id": str(raw.get("id") or f"{criterion_type[:3]}-{index:03d}"),
                    "criterion_type": criterion_type,
                    "field": str(structured.get("parameter") or "").strip().lower().replace(" ", "_"),
                    "operator": operator,
                    "value_low": low,
                    "value_high": high,
                    "unit": str(structured.get("unit") or ""),
                    "time_window_days": str(structured.get("time_window_days") or ""),
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
            trial_id = str(item.get("trial_id") or "")
            previous = latest.get(trial_id)
            if trial_id and (
                previous is None
                or int(item.get("updated_at") or 0) > int(previous.get("updated_at") or 0)
            ):
                latest[trial_id] = item
        return [self._summary(latest[key]) for key in sorted(latest)]

    def trial_catalog(self, trial_ids: list[str]) -> dict[str, dict[str, Any]]:
        return {
            trial_id: trial
            for trial_id in trial_ids
            if (trial := self.get_trial(trial_id)) is not None
        }

    def trial_criteria(self, trial_id: str) -> list[dict[str, Any]]:
        return flatten_protocol_item(self._approved_item(trial_id) or {})

    @staticmethod
    def _summary(item: dict[str, Any]) -> dict[str, Any]:
        rows = flatten_protocol_item(item)
        return {
            "trial_id": str(item.get("trial_id") or ""),
            "trial_name": str(item.get("trial_title") or item.get("trial_id") or ""),
            "description": str(item.get("condition") or ""),
            "purpose": str(item.get("intervention") or item.get("phase") or ""),
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


__all__ = [
    "DynamoDBCriteriaRepository",
    "LocalTrialCatalog",
    "flatten_protocol_item",
    "normalize_operator",
]
