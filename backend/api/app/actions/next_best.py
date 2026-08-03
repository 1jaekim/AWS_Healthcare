"""Next-Best-Evidence Agent: 정보 가치가 높은 다음 질문을 고른다.

미해소 기준(UNKNOWN, CONFLICTING) 중 어떤 항목을 먼저 확인해야 판정이 가장 빨리
확정되는지 계산한다. 정보 가치는 '이 항목이 풀리면 남는 미해소 항목이 얼마나 줄어드는가'
와 '확인 난이도' 로 근사한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..domain.models import CriterionResult
from ..domain.states import CriterionKind, CriterionStatus
from ..safety.guardrails import GuardrailVerdict, LocalGuardrail

_EFFORT: dict[CriterionKind, float] = {
    CriterionKind.NUMERIC_POINT: 0.4,
    CriterionKind.TEMPORAL_WINDOW: 0.6,
    CriterionKind.CATEGORICAL: 0.3,
    CriterionKind.DERIVED_BOOLEAN: 0.2,
    CriterionKind.NARRATIVE: 0.5,
}

_QUESTION_TEMPLATES: dict[str, str] = {
    "hba1c": "최근 6개월 이내에 측정한 HbA1c 결과가 있으신가요?",
    "hba1c_count_365d": "지난 1년 동안 HbA1c 검사를 몇 번 받으셨나요?",
    "egfr": "최근 신장기능 검사(eGFR 또는 크레아티닌) 결과가 있으신가요?",
    "uacr": "최근 소변 알부민(UACR) 검사를 받으신 적이 있나요?",
    "bmi": "현재 키와 체중을 알려주실 수 있나요?",
    "age": "생년월일을 확인해 주실 수 있나요?",
    "t2d_duration_days": "당뇨 진단을 처음 받은 시기가 언제인가요?",
    "stable_regimen_days": "현재 복용 중인 당뇨 약을 언제부터 유지하고 계신가요?",
    "diabetes_status": "진단받은 당뇨 종류를 알고 계신가요?",
    "active_pregnancy": "현재 임신 중이거나 임신 계획이 있으신가요?",
    "uncontrolled_bp": "최근 혈압 측정값을 알고 계신가요?",
}

_CONFLICT_TEMPLATES: dict[str, str] = {
    "active_pregnancy": "기록 간 임신 관련 내용이 달라 다시 확인이 필요합니다. 현재 상태를 알려주실 수 있나요?",
    "uncontrolled_bp": "혈압 관련 기록이 서로 달라 확인이 필요합니다. 최근 측정값을 알려주실 수 있나요?",
}


@dataclass(frozen=True)
class EvidenceRequest:
    """참여자 또는 연구자에게 보낼 확인 항목 한 건."""

    request_id: str
    criterion_id: str
    field_name: str
    label: str
    reason: str
    question: str
    information_value: float
    effort: float
    priority: int
    target: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "criterion_id": self.criterion_id,
            "field": self.field_name,
            "label": self.label,
            "reason": self.reason,
            "question": self.question,
            "information_value": self.information_value,
            "effort": self.effort,
            "priority": self.priority,
            "target": self.target,
        }


class NextBestEvidenceAgent:
    """미해소 기준을 정보 가치 순으로 정렬해 질문을 만든다."""

    def __init__(self, guardrail: LocalGuardrail | None = None) -> None:
        self._guardrail = guardrail or LocalGuardrail(attach_disclaimer=False)

    def propose(
        self, run_id: str, results: list[CriterionResult], *, limit: int = 5
    ) -> list[EvidenceRequest]:
        open_items = [
            item
            for item in results
            if item.status
            in (
                CriterionStatus.UNKNOWN,
                CriterionStatus.CONFLICTING,
                CriterionStatus.REVIEW_REQUIRED,
            )
        ]
        if not open_items:
            return []

        total_open = len(open_items)
        scored: list[EvidenceRequest] = []
        for index, item in enumerate(open_items, start=1):
            effort = _EFFORT.get(item.kind, 0.5)
            value = self._information_value(item, total_open)
            question = self._question(item)
            verdict: GuardrailVerdict = self._guardrail.review(
                question, audience="patient"
            )
            if verdict.blocked:
                continue
            scored.append(
                EvidenceRequest(
                    request_id=f"NBE-{run_id}-{index:02d}",
                    criterion_id=item.criterion_id,
                    field_name=item.field_name,
                    label=item.label,
                    reason=self._reason(item),
                    question=verdict.text,
                    information_value=round(value, 3),
                    effort=effort,
                    priority=0,
                    target=self._target(item),
                )
            )

        scored.sort(
            key=lambda req: (-req.information_value, req.effort, req.criterion_id)
        )
        return [
            EvidenceRequest(**{**req.__dict__, "priority": rank})
            for rank, req in enumerate(scored[:limit], start=1)
        ]

    @staticmethod
    def _information_value(item: CriterionResult, total_open: int) -> float:
        """미해소 비중과 상태 심각도로 정보 가치를 근사한다."""
        base = 1.0 / total_open
        weight = {
            CriterionStatus.CONFLICTING: 1.6,
            CriterionStatus.REVIEW_REQUIRED: 1.3,
            CriterionStatus.UNKNOWN: 1.0,
        }.get(item.status, 1.0)
        # 신뢰도가 낮을수록 확인 가치가 크다
        uncertainty = 1.0 - item.confidence
        inclusion_bonus = 1.15 if item.criterion_type == "INCLUSION" else 1.0
        return base * weight * (0.5 + uncertainty) * inclusion_bonus

    @staticmethod
    def _question(item: CriterionResult) -> str:
        if item.status is CriterionStatus.CONFLICTING:
            conflict_question = _CONFLICT_TEMPLATES.get(item.field_name)
            if conflict_question:
                return conflict_question
        template = _QUESTION_TEMPLATES.get(item.field_name)
        if template:
            return template
        return f"{item.label} 관련 최근 기록을 확인해 주실 수 있나요?"

    @staticmethod
    def _reason(item: CriterionResult) -> str:
        if item.status is CriterionStatus.CONFLICTING:
            return f"{item.label} 기록이 서로 어긋나 확인이 필요합니다."
        if item.status is CriterionStatus.REVIEW_REQUIRED:
            return f"{item.label} 자동 판정 신뢰도가 낮습니다."
        return f"{item.label} 관찰값이 확인되지 않았습니다."

    @staticmethod
    def _target(item: CriterionResult) -> str:
        """검토 필요는 연구자에게, 정보 부족은 참여자에게 묻는다."""
        if item.status is CriterionStatus.REVIEW_REQUIRED:
            return "researcher"
        return "participant"
