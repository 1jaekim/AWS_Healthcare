"""Timeline Graph Tool: 방문·검사·약물·기간을 시간축에서 조회한다.

Amazon Neptune 어댑터로 교체할 지점. 읽기 전용 권한만 갖는다.
반환하는 모든 값은 Observation 으로 감싸 출처 ID(encounter_id)와 측정일을 동반한다.
"""

from __future__ import annotations

from typing import Any

from ..domain.models import Observation
from ..repository import DatasetRepository
from .base import Action, BaseTool, DataStore, Permission, ToolContext

_MEASUREMENT_COLUMNS: dict[str, str] = {
    "hba1c": "hba1c_pct",
    "bmi": "bmi_kg_m2",
    "egfr": "egfr_ml_min_1_73m2",
    "uacr": "uacr_mg_g",
    "fasting_glucose": "fasting_glucose_mg_dl",
    "age": "age",
    "t2d_duration_days": "t2d_duration_days",
    "hba1c_count_365d": "hba1c_count_365d",
    "stable_regimen_days": "stable_regimen_days",
}

_UNCONTROLLED_SYSTOLIC = 160.0
_UNCONTROLLED_DIASTOLIC = 100.0


def _to_float(value: str | None) -> float | None:
    if value is None or value.strip() == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


class TimelineGraphTool(BaseTool):
    """환자 종단 그래프에서 관찰값을 조회한다."""

    name = "timeline_graph_tool"
    permissions = (Permission(DataStore.TIMELINE_GRAPH, Action.READ),)

    def __init__(self, repository: DatasetRepository) -> None:
        self._repository = repository

    def invoke(
        self, context: ToolContext, /, **kwargs: Any
    ) -> dict[str, Observation]:
        """요청된 필드의 관찰값을 인덱스 방문 기준으로 반환한다."""
        self.assert_allowed(Permission(DataStore.TIMELINE_GRAPH, Action.READ))

        fields: list[str] = list(kwargs.get("fields", []))
        row = self._index_row(context)
        if row is None:
            return {}

        observations: dict[str, Observation] = {}
        for field_name in fields:
            observation = self._resolve(field_name, row, context)
            if observation is not None:
                observations[field_name] = observation
        return observations

    def _index_row(self, context: ToolContext) -> dict[str, str] | None:
        """인덱스 방문 레코드를 찾는다."""
        for row in self._repository.timelines.get(context.person_id, []):
            if row["encounter_id"] == context.index_encounter_id:
                return row
        return None

    def _resolve(
        self, field_name: str, row: dict[str, str], context: ToolContext
    ) -> Observation | None:
        """필드별 조회 규칙을 적용한다."""
        encounter_id = row["encounter_id"]
        observed_at = row["encounter_date"]

        if field_name == "diabetes_status":
            return Observation(
                field_name=field_name,
                value=row.get("diabetes_status") or None,
                unit="category",
                observed_at=observed_at,
                source="TIMELINE_GRAPH",
                source_id=encounter_id,
            )

        if field_name == "active_pregnancy":
            raw = (row.get("active_pregnancy") or "").strip()
            value = raw in {"1", "true", "True"} if raw else None
            return Observation(
                field_name=field_name,
                value=value,
                unit="boolean",
                observed_at=observed_at,
                source="TIMELINE_GRAPH",
                source_id=encounter_id,
                detail="구조화 임신 상태 플래그",
            )

        if field_name == "uncontrolled_bp":
            return self._uncontrolled_bp(row, encounter_id, observed_at)

        column = _MEASUREMENT_COLUMNS.get(field_name)
        if column is None:
            return None
        return Observation(
            field_name=field_name,
            value=_to_float(row.get(column)),
            unit=None,
            observed_at=observed_at,
            source="TIMELINE_GRAPH",
            source_id=encounter_id,
        )

    def _uncontrolled_bp(
        self, row: dict[str, str], encounter_id: str, observed_at: str
    ) -> Observation:
        """혈압 측정값에서 조절되지 않는 고혈압 여부를 파생한다."""
        systolic = _to_float(row.get("systolic_bp_mmhg"))
        diastolic = _to_float(row.get("diastolic_bp_mmhg"))
        if systolic is None and diastolic is None:
            value: bool | None = None
            detail = "혈압 측정값 없음"
        else:
            value = (systolic or 0) >= _UNCONTROLLED_SYSTOLIC or (
                diastolic or 0
            ) >= _UNCONTROLLED_DIASTOLIC
            detail = (
                f"{systolic:.0f}/{diastolic:.0f} mmHg 기준 "
                f"(임계 {_UNCONTROLLED_SYSTOLIC:.0f}/{_UNCONTROLLED_DIASTOLIC:.0f})"
                if systolic is not None and diastolic is not None
                else "혈압 일부 값 누락"
            )
        return Observation(
            field_name="uncontrolled_bp",
            value=value,
            unit="boolean",
            observed_at=observed_at,
            source="TIMELINE_GRAPH",
            source_id=encounter_id,
            detail=detail,
        )

    def index_encounter(self, person_id: int) -> dict[str, str] | None:
        """환자의 최신 방문을 인덱스 방문으로 사용한다."""
        events = self._repository.timelines.get(person_id, [])
        return events[-1] if events else None
