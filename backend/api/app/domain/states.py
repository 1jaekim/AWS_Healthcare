"""아키텍처 7계층: 기준별 결과 상태 모델.

Deterministic Aggregator 가 산출하는 상태는 다섯 가지로 고정된다.
동일 입력에 대해 항상 동일한 상태가 나와야 하므로 판정 규칙을 여기에 모은다.
"""

from __future__ import annotations

from enum import StrEnum


class CriterionStatus(StrEnum):
    """기준 한 건에 대한 판정 상태."""

    EVIDENCE_FOUND = "EVIDENCE_FOUND"
    """근거가 확인되어 조건을 충족한다."""

    CONTRADICTED = "CONTRADICTED"
    """근거가 확인되었으나 조건과 충돌한다."""

    UNKNOWN = "UNKNOWN"
    """판정에 필요한 관찰값이 없다."""

    CONFLICTING = "CONFLICTING"
    """기록 간 값이 서로 어긋난다."""

    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    """자동 판정 신뢰도가 낮아 사람 검토가 필요하다."""


class EligibilityStatus(StrEnum):
    """실행 단위(환자 x 시험) 종합 상태."""

    ELIGIBLE = "ELIGIBLE"
    INELIGIBLE = "INELIGIBLE"
    NEEDS_MORE_EVIDENCE = "NEEDS_MORE_EVIDENCE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class CriterionKind(StrEnum):
    """Criterion Router 가 실행 계획을 세울 때 쓰는 조건 유형."""

    NUMERIC_POINT = "NUMERIC_POINT"
    """단일 시점 수치 비교. 예: HbA1c, eGFR, BMI"""

    TEMPORAL_WINDOW = "TEMPORAL_WINDOW"
    """기간·횟수 계산이 필요. 예: 최근 365일 HbA1c 횟수"""

    CATEGORICAL = "CATEGORICAL"
    """범주 일치 비교. 예: diabetes_status"""

    DERIVED_BOOLEAN = "DERIVED_BOOLEAN"
    """구조화 값과 자유서술을 함께 봐야 하는 파생 불리언. 예: 임신 여부"""

    NARRATIVE = "NARRATIVE"
    """구조화 필드가 없어 자유서술 검색에만 의존."""


TERMINAL_FAILURE_STATES = frozenset(
    {CriterionStatus.CONTRADICTED}
)
"""이 상태가 하나라도 있으면 종합 판정은 부적합으로 확정된다."""

OPEN_STATES = frozenset(
    {CriterionStatus.UNKNOWN, CriterionStatus.CONFLICTING}
)
"""추가 근거 수집으로 해소 가능한 상태."""
