"""기준 필드 카탈로그와 조건 유형 분류.

Criterion Router(3계층) 가 조건을 어떤 Tool 조합으로 처리할지 결정할 때 참조한다.
새 기준 필드가 추가되면 FIELD_CATALOG 에만 등록하면 라우팅이 자동으로 따라온다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .states import CriterionKind


@dataclass(frozen=True)
class FieldSpec:
    """기준 필드 하나의 처리 방법 정의."""

    name: str
    kind: CriterionKind
    label: str
    unit: str | None = None
    window_days: int | None = None
    """TEMPORAL_WINDOW 인 경우 계산 창 크기."""
    narrative_terms: tuple[str, ...] = field(default_factory=tuple)
    """자유서술 검색 시 사용할 질의어. DERIVED_BOOLEAN / NARRATIVE 에서 사용."""


FIELD_CATALOG: dict[str, FieldSpec] = {
    "age": FieldSpec("age", CriterionKind.NUMERIC_POINT, "연령", "years"),
    "hba1c": FieldSpec("hba1c", CriterionKind.NUMERIC_POINT, "HbA1c", "%"),
    "bmi": FieldSpec("bmi", CriterionKind.NUMERIC_POINT, "BMI", "kg/m2"),
    "egfr": FieldSpec(
        "egfr", CriterionKind.NUMERIC_POINT, "eGFR", "mL/min/1.73m2"
    ),
    "uacr": FieldSpec("uacr", CriterionKind.NUMERIC_POINT, "UACR", "mg/g"),
    "fasting_glucose": FieldSpec(
        "fasting_glucose", CriterionKind.NUMERIC_POINT, "공복혈당", "mg/dL"
    ),
    "t2d_duration_days": FieldSpec(
        "t2d_duration_days",
        CriterionKind.TEMPORAL_WINDOW,
        "제2형 당뇨 진단 기간",
        "days",
    ),
    "hba1c_count_365d": FieldSpec(
        "hba1c_count_365d",
        CriterionKind.TEMPORAL_WINDOW,
        "최근 365일 HbA1c 측정 횟수",
        "count",
        window_days=365,
    ),
    "stable_regimen_days": FieldSpec(
        "stable_regimen_days",
        CriterionKind.TEMPORAL_WINDOW,
        "안정 치료 기간",
        "days",
    ),
    "diabetes_status": FieldSpec(
        "diabetes_status", CriterionKind.CATEGORICAL, "당뇨 상태", "category"
    ),
    "uncontrolled_bp": FieldSpec(
        "uncontrolled_bp",
        CriterionKind.DERIVED_BOOLEAN,
        "조절되지 않는 고혈압",
        "boolean",
        narrative_terms=(
            "조절되지 않는 고혈압",
            "혈압 조절 불량",
            "고혈압 미조절",
            "저항성 고혈압",
        ),
    ),
    "active_pregnancy": FieldSpec(
        "active_pregnancy",
        CriterionKind.DERIVED_BOOLEAN,
        "임신 여부",
        "boolean",
        narrative_terms=("임신 중", "임신부", "수유 중", "임신 확인"),
    ),
}


_NARRATIVE_FALLBACK = FieldSpec(
    "unknown", CriterionKind.NARRATIVE, "미등록 기준 필드"
)


def spec_for(field_name: str) -> FieldSpec:
    """필드 정의를 조회한다. 카탈로그에 없으면 자유서술 검색으로 폴백한다."""
    known = FIELD_CATALOG.get(field_name)
    if known is not None:
        return known
    return FieldSpec(
        name=field_name,
        kind=_NARRATIVE_FALLBACK.kind,
        label=field_name,
        narrative_terms=(field_name,),
    )


def requires_narrative_lookup(spec: FieldSpec) -> bool:
    """자유서술 EMR 검색이 필요한 조건인지 판단한다."""
    return spec.kind in (CriterionKind.DERIVED_BOOLEAN, CriterionKind.NARRATIVE)


def requires_graph_lookup(spec: FieldSpec) -> bool:
    """타임라인 그래프 조회가 필요한 조건인지 판단한다."""
    return spec.kind is not CriterionKind.NARRATIVE
