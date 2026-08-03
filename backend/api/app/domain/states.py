"""아키텍처 7계층: 기준별 결과 상태 모델.

Deterministic Aggregator 가 산출하는 상태는 다섯 가지로 고정된다.
동일 입력에 대해 항상 동일한 상태가 나와야 하므로 판정 규칙을 여기에 모은다.
"""

from __future__ import annotations

from enum import StrEnum

# CriterionStatus 는 에이전트 계층과 공유하는 어휘라 agent/contracts.py 가 정의한다.
# 에이전트가 만든 상태값과 여기서 비교하는 값이 같은 enum 객체여야 하기 때문이다.
# backend 코드는 계속 이 모듈에서 가져다 쓴다.
from agent.contracts import CriterionStatus as CriterionStatus


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

__all__ = [
    "CriterionKind",
    "CriterionStatus",
    "EligibilityStatus",
    "OPEN_STATES",
    "TERMINAL_FAILURE_STATES",
]
