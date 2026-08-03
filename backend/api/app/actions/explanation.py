"""Explanation Agent: 관리자·참여자별 설명을 만든다.

같은 판정을 두 톤으로 낸다. 관리자에게는 수치와 기준을, 참여자에게는 쉬운 문장을 준다.
모든 출력은 Guardrails 를 통과하며, 출처 ID가 없으면 설명을 생성하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..domain.models import CriterionResult
from ..domain.states import CriterionStatus, EligibilityStatus
from ..reasoning.aggregator import AggregateOutcome
from ..safety.guardrails import LocalGuardrail

_PATIENT_SUMMARY: dict[EligibilityStatus, str] = {
    EligibilityStatus.ELIGIBLE: (
        "확인된 기록을 기준으로 이 연구의 참여 조건에 해당할 가능성이 있습니다."
    ),
    EligibilityStatus.INELIGIBLE: (
        "확인된 기록을 기준으로는 이 연구의 참여 조건과 맞지 않는 항목이 있습니다."
    ),
    EligibilityStatus.NEEDS_MORE_EVIDENCE: (
        "판단에 필요한 정보가 일부 부족해 확인이 더 필요합니다."
    ),
    EligibilityStatus.REVIEW_REQUIRED: (
        "연구 담당자가 기록을 한 번 더 확인한 뒤 안내드릴 예정입니다."
    ),
}

_ADMIN_SUMMARY: dict[EligibilityStatus, str] = {
    EligibilityStatus.ELIGIBLE: "모든 기준에서 근거가 확인되었습니다.",
    EligibilityStatus.INELIGIBLE: "조건과 충돌하는 기준이 확인되었습니다.",
    EligibilityStatus.NEEDS_MORE_EVIDENCE: "미해소 기준이 남아 있어 판정이 보류되었습니다.",
    EligibilityStatus.REVIEW_REQUIRED: "자동 판정 신뢰도가 낮은 기준이 있어 검토가 필요합니다.",
}


@dataclass
class Explanation:
    """대상별 설명 묶음."""

    audience: str
    summary: str
    highlights: list[str] = field(default_factory=list)
    blocked: bool = False
    guardrail_findings: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "audience": self.audience,
            "summary": self.summary,
            "highlights": self.highlights,
            "blocked": self.blocked,
            "guardrail_findings": self.guardrail_findings,
        }


class ExplanationAgent:
    """판정 결과를 대상별 자연어 설명으로 변환한다."""

    def __init__(self, guardrail: LocalGuardrail | None = None) -> None:
        self._guardrail = guardrail or LocalGuardrail()

    def explain(
        self,
        *,
        outcome: AggregateOutcome,
        results: list[CriterionResult],
        audience: str,
        run_id: str | None = None,
    ) -> Explanation:
        """템플릿으로 설명을 만든다. run_id 는 모델 버전과 호출부를 맞추기 위해 받는다."""
        source_ids = [sid for item in results for sid in item.source_ids]
        base = (
            _PATIENT_SUMMARY[outcome.eligibility_status]
            if audience == "patient"
            else _ADMIN_SUMMARY[outcome.eligibility_status]
        )
        grounding = self._guardrail.check_grounding(base, source_ids=source_ids)
        if grounding.blocked:
            return Explanation(
                audience=audience,
                summary=grounding.text,
                blocked=True,
                guardrail_findings=[
                    {
                        "rule_id": item.rule_id,
                        "severity": item.severity,
                    }
                    for item in grounding.findings
                ],
            )

        highlights = (
            self._patient_highlights(results)
            if audience == "patient"
            else self._admin_highlights(results, outcome)
        )
        verdict = self._guardrail.review(base, audience=audience)
        reviewed_highlights: list[str] = []
        for line in highlights:
            checked = self._guardrail.review(line, audience=audience)
            if not checked.blocked:
                reviewed_highlights.append(checked.text)

        return Explanation(
            audience=audience,
            summary=verdict.text,
            highlights=reviewed_highlights,
            blocked=False,
            guardrail_findings=[
                {"rule_id": item.rule_id, "severity": item.severity}
                for item in verdict.findings
            ],
        )

    @staticmethod
    def _admin_highlights(
        results: list[CriterionResult], outcome: AggregateOutcome
    ) -> list[str]:
        """수치와 기준을 그대로 보여준다."""
        lines = [
            f"충족 {outcome.criteria_met}/{outcome.criteria_total} 기준",
        ]
        for item in results:
            if item.status is CriterionStatus.EVIDENCE_FOUND:
                continue
            observed = item.observed_value or "미확인"
            lines.append(
                f"[{item.criterion_id}] {item.label}: 관찰 {observed} / "
                f"기준 {item.expected_condition} · {item.status}"
            )
        return lines

    @staticmethod
    def _patient_highlights(results: list[CriterionResult]) -> list[str]:
        """수치 나열 대신 확인이 필요한 항목만 짚어준다."""
        lines: list[str] = []
        for item in results:
            if item.status is CriterionStatus.CONTRADICTED:
                lines.append(f"{item.label} 항목이 참여 조건과 맞지 않습니다.")
            elif item.status in (
                CriterionStatus.UNKNOWN,
                CriterionStatus.CONFLICTING,
            ):
                lines.append(f"{item.label} 항목은 추가 확인이 필요합니다.")
        if not lines:
            lines.append("확인된 항목에서 추가로 필요한 정보는 없습니다.")
        return lines

    def explain_both(
        self,
        *,
        outcome: AggregateOutcome,
        results: list[CriterionResult],
        run_id: str | None = None,
    ) -> dict[str, dict[str, Any]]:
        """관리자·참여자 설명을 동시에 만든다."""
        return {
            "admin": self.explain(
                outcome=outcome, results=results, audience="admin", run_id=run_id
            ).to_dict(),
            "patient": self.explain(
                outcome=outcome, results=results, audience="patient", run_id=run_id
            ).to_dict(),
        }
