"""Criteria Tool: 시험 조건과 버전을 조회한다.

DynamoDB Criteria Store 로 교체할 지점. 읽기 전용 권한만 갖는다.
버전은 기준 내용 해시로 산출해, 기준이 바뀌면 실행 결과의 재현 조건도 달라지도록 한다.
"""

from __future__ import annotations

import hashlib
from typing import Any

from ..domain.criteria import FieldSpec, spec_for
from ..domain.models import CriterionRule
from ..repository import DatasetRepository
from .base import Action, BaseTool, DataStore, Permission, ToolContext


def _window_days(row: dict[str, str], spec: FieldSpec) -> int | None:
    """기준의 관찰 기간을 정한다.

    공고에서 기간이 명시되면 그 값을 쓰고, 없으면 필드 카탈로그의 기본 창을 쓴다.
    카탈로그 값은 데이터가 아니라 코드라서 `criteria_version` 해시에는 넣지 않는다.
    기준 행에 기간이 실려 오면 그때 해시에 포함된다.
    """
    raw = (row.get("time_window_days") or "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            return spec.window_days
    return spec.window_days


def _criteria_version(rows: list[dict[str, str]]) -> str:
    """기준 집합의 내용 해시를 버전 식별자로 사용한다."""
    payload = "|".join(
        ":".join(
            (
                row["criterion_id"],
                row["criterion_type"],
                row["field"],
                row["operator"],
                row.get("value_low", ""),
                row.get("value_high", ""),
                row.get("unit", ""),
                # 기간이 실려 오는 기준만 해시에 포함한다. 기간 컬럼이 없는 기존
                # 기준의 버전 값을 흔들지 않기 위해서다.
                *(
                    (row["time_window_days"],)
                    if row.get("time_window_days")
                    else ()
                ),
            )
        )
        for row in sorted(rows, key=lambda item: item["criterion_id"])
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:10]
    return f"v1-{digest}"


class CriteriaTool(BaseTool):
    """시험 조건 조회 전용 Tool."""

    name = "criteria_tool"
    permissions = (Permission(DataStore.CRITERIA_TABLE, Action.READ),)

    def __init__(
        self, repository: DatasetRepository, *, criteria_source: Any | None = None
    ) -> None:
        self._repository = repository
        self._criteria_source = criteria_source or repository
        self._version_cache: dict[str, str] = {}

    def _rows(self, trial_id: str) -> list[dict[str, str]]:
        if hasattr(self._criteria_source, "rows_for"):
            return self._criteria_source.rows_for(trial_id)
        return self._criteria_source.criteria.get(trial_id, [])

    def version_for(self, trial_id: str) -> str:
        """시험의 현재 기준 버전을 반환한다."""
        cached = self._version_cache.get(trial_id)
        if cached is not None:
            return cached
        rows = self._rows(trial_id)
        version = _criteria_version(rows)
        self._version_cache[trial_id] = version
        return version

    def invoke(self, context: ToolContext, /, **kwargs: Any) -> list[CriterionRule]:
        """시험의 모든 기준을 실행 규칙 형태로 반환한다."""
        self.assert_allowed(Permission(DataStore.CRITERIA_TABLE, Action.READ))

        trial_id = kwargs.get("trial_id", context.trial_id)
        rows = self._rows(trial_id)
        version = _criteria_version(rows)
        self._version_cache[trial_id] = version

        rules: list[CriterionRule] = []
        for row in rows:
            spec = spec_for(row["field"])
            rules.append(
                CriterionRule(
                    criterion_id=row["criterion_id"],
                    criterion_type=row["criterion_type"],  # type: ignore[arg-type]
                    field_name=row["field"],
                    operator=row["operator"],
                    value_low=row.get("value_low") or None,
                    value_high=row.get("value_high") or None,
                    unit=row.get("unit") or spec.unit,
                    label=spec.label,
                    kind=spec.kind,
                    trial_id=trial_id,
                    criteria_version=version,
                    time_window_days=_window_days(row, spec),
                )
            )
        rules.sort(key=lambda rule: rule.criterion_id)
        return rules
