"""Rule Aggregator: LLM 판단과 규칙 판정을 합쳐 상태를 확정한다.

MATCHING_MODEL_V2 의 `최종 상태 확정 규칙` 표를 그대로 구현한다.

| 상황 | 최종 상태 |
|------|-----------|
| LLM 제안이 `OK` 이고 규칙 검증도 통과 | `OK` |
| LLM 제안이 `NOT_OK` 이고 충돌 근거가 명확 | `NOT_OK` |
| 근거가 없거나 날짜·단위가 부족 | `UNKNOWN` |
| LLM 제안과 규칙 결과가 충돌 | A2A 토론 대상 |
| 제외 기준 가능성이 있는데 근거 부족 | Human Review |

FM 을 호출하지 않는다. 입력은 규칙 검증 결과, LLM 판단, 검증 항목 결과 세 가지이며
모두 결정론적이라 같은 입력에서 같은 상태가 나온다.

`RuleAggregator` 는 두 가지를 함께 만든다.

- `CriterionDecision`: v2 계약의 3값 상태와 후속 경로(`DECIDED`/`A2A`/`HUMAN_REVIEW`)
- `Verification`: 기존 `DeterministicAggregator` 가 기준별 결과로 환원할 수 있는 형태

규칙 판정이 판정 원본이라는 원칙은 유지한다. LLM 이 규칙보다 강한 결론을 내려도
상태를 올리지 않고, 충돌이면 토론이나 사람 검토로 넘긴다.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from agent.judge import RULE_STATUS_PROJECTION, CriterionJudgment

from ..domain.models import EvidenceBundle, Verification
from ..domain.states import CriterionStatus
from .judgment import JudgmentVerification

DECIDED = "DECIDED"
"""추가 절차 없이 확정된 기준."""

A2A = "A2A"
"""제한 라운드 교차 검토로 넘기는 기준."""

HUMAN_REVIEW = "HUMAN_REVIEW"
"""사람 검토 큐로 넘기는 기준."""

_TRUST_FAILURES = frozenset({"V-EVIDENCE", "V-PII", "V-TYPE", "V-OPERATOR"})
"""모델 출력 자체를 믿을 수 없게 만드는 검사. 사람 검토로 보낸다."""

_QUALITY_FAILURES = frozenset({"V-WINDOW", "V-UNIT", "V-CONFIDENCE"})
"""근거 품질이 모자란 검사. 추가 정보로 해소할 수 있어 `UNKNOWN` 으로 남긴다."""

_ESCALATED_CONFIDENCE = 0.5
"""토론·검토로 넘어간 기준의 확신도 상한."""

_SOFT_DIVERGENCE_CONFIDENCE = 0.7
"""규칙은 판정했으나 모델이 보류한 경우의 확신도 상한."""

#: 3값 상태를 기준별 내부 상태로 옮기는 기본 표.
_DECISIVE_STATUS: dict[str, CriterionStatus] = {
    "OK": CriterionStatus.EVIDENCE_FOUND,
    "NOT_OK": CriterionStatus.CONTRADICTED,
}

_OPEN_STATUSES = frozenset(
    {
        CriterionStatus.UNKNOWN,
        CriterionStatus.CONFLICTING,
        CriterionStatus.REVIEW_REQUIRED,
    }
)


@dataclass(frozen=True)
class CriterionDecision:
    """기준 한 건의 확정 결과와 후속 경로."""

    criterion_id: str
    criterion_type: str
    status: str
    """v2 3값 상태. `OK`, `NOT_OK`, `UNKNOWN`."""

    route: str
    """`DECIDED`, `A2A`, `HUMAN_REVIEW`."""

    verification: Verification
    """기존 취합기로 넘기는 검증 결과."""

    judgment: CriterionJudgment
    report: JudgmentVerification
    rule_status: CriterionStatus
    """규칙 검증기가 제안한 상태. 무엇이 판정 원본이었는지 남긴다."""

    reason: str = ""

    @property
    def criterion_status(self) -> CriterionStatus:
        return self.verification.proposed_status

    @property
    def needs_deliberation(self) -> bool:
        return self.route == A2A

    def to_dict(self) -> dict[str, Any]:
        return {
            "criterion_id": self.criterion_id,
            "criterion_type": self.criterion_type,
            "status": self.status,
            "route": self.route,
            "criterion_status": str(self.criterion_status),
            "rule_status": str(self.rule_status),
            "confidence": self.verification.confidence,
            "reason": self.reason,
            "judgment": self.judgment.to_dict(),
            "verification": self.report.to_dict(),
        }


class RuleAggregator:
    """LLM 판단·규칙 판정·검증 결과를 하나의 확정 상태로 합친다."""

    def confirm(
        self,
        bundle: EvidenceBundle,
        *,
        judgment: CriterionJudgment,
        report: JudgmentVerification,
        rule_verification: Verification,
    ) -> CriterionDecision:
        """최종 상태 확정 규칙 표를 적용한다."""
        rule_status = rule_verification.proposed_status
        rule_projection = RULE_STATUS_PROJECTION.get(rule_status, "UNKNOWN")
        judged = judgment.proposed_status
        is_exclusion = bundle.rule.criterion_type == "EXCLUSION"

        notes = list(rule_verification.notes)
        conflicts = list(rule_verification.conflicts)
        notes.extend(report.notes())

        # 1. LLM 제안과 규칙 결과가 정면으로 충돌하면 토론 대상이다.
        if {judged, rule_projection} == {"OK", "NOT_OK"}:
            conflicts.append(
                f"LLM 판단({judged})과 규칙 판정({rule_projection})이 충돌합니다."
            )
            return self._decision(
                bundle,
                judgment=judgment,
                report=report,
                rule_verification=rule_verification,
                status="UNKNOWN",
                route=A2A,
                criterion_status=CriterionStatus.CONFLICTING,
                confidence=min(
                    rule_verification.confidence, _ESCALATED_CONFIDENCE
                ),
                notes=notes,
                conflicts=conflicts,
                reason="LLM 판단과 규칙 판정이 충돌해 교차 검토로 넘겼습니다.",
            )

        # 2. 검증에서 확정을 막는 실패가 있으면 상태를 확정하지 않는다.
        blocking = {item.check_id for item in report.blocking_failures}
        if blocking:
            trust_broken = bool(blocking & _TRUST_FAILURES)
            # 제외 기준은 근거가 모자라면 곧바로 사람 검토로 보낸다.
            # 놓친 제외 기준은 되돌릴 수 없는 위험이라 질문으로 미루지 않는다.
            to_human = trust_broken or is_exclusion
            return self._decision(
                bundle,
                judgment=judgment,
                report=report,
                rule_verification=rule_verification,
                status="UNKNOWN",
                route=HUMAN_REVIEW if to_human else DECIDED,
                criterion_status=(
                    CriterionStatus.REVIEW_REQUIRED
                    if to_human
                    else self._open_status(rule_status)
                ),
                confidence=min(
                    rule_verification.confidence, _ESCALATED_CONFIDENCE
                ),
                notes=notes,
                conflicts=conflicts,
                reason=(
                    "검증 실패로 사람 검토가 필요합니다: "
                    if to_human
                    else "근거가 부족해 정보 부족으로 남겼습니다: "
                )
                + ", ".join(sorted(blocking)),
            )

        # 3. 규칙이 판정하고 모델도 같은 방향이면 확정한다.
        if judged == rule_projection and judged in _DECISIVE_STATUS:
            return self._decision(
                bundle,
                judgment=judgment,
                report=report,
                rule_verification=rule_verification,
                status=judged,
                route=DECIDED,
                criterion_status=_DECISIVE_STATUS[judged],
                confidence=min(
                    judgment.confidence, rule_verification.confidence
                ),
                notes=notes,
                conflicts=conflicts,
                reason="규칙 판정과 LLM 판단이 일치해 확정했습니다.",
            )

        # 4. 규칙은 판정했는데 모델이 보류했다. 규칙 판정을 유지하고 확신도만 낮춘다.
        if judged == "UNKNOWN" and rule_projection in _DECISIVE_STATUS:
            if judgment.missing_information:
                notes.append(
                    "LLM 이 부족하다고 본 항목: "
                    + ", ".join(judgment.missing_information)
                )
            return self._decision(
                bundle,
                judgment=judgment,
                report=report,
                rule_verification=rule_verification,
                status=rule_projection,
                route=A2A if judgment.needs_a2a else DECIDED,
                criterion_status=_DECISIVE_STATUS[rule_projection],
                confidence=min(
                    rule_verification.confidence, _SOFT_DIVERGENCE_CONFIDENCE
                ),
                notes=notes,
                conflicts=conflicts,
                reason="규칙은 판정했으나 LLM 이 보류해 확신도를 낮췄습니다.",
            )

        # 5. 규칙이 판정하지 못한 기준. 모델 제안만으로 상태를 올리지 않는다.
        escalate = judged in _DECISIVE_STATUS or judgment.needs_a2a
        return self._decision(
            bundle,
            judgment=judgment,
            report=report,
            rule_verification=rule_verification,
            status="UNKNOWN",
            route=A2A if escalate else DECIDED,
            criterion_status=self._open_status(rule_status),
            confidence=(
                min(rule_verification.confidence, _ESCALATED_CONFIDENCE)
                if escalate
                else rule_verification.confidence
            ),
            notes=notes,
            conflicts=conflicts,
            reason=(
                "규칙이 판정하지 못해 LLM 제안만으로 확정하지 않았습니다."
                if escalate
                else "판정에 필요한 근거가 부족합니다."
            ),
        )

    # -- 내부 ------------------------------------------------------------

    @staticmethod
    def _open_status(rule_status: CriterionStatus) -> CriterionStatus:
        """규칙이 이미 미해소로 본 상태는 그대로 살린다."""
        return rule_status if rule_status in _OPEN_STATUSES else CriterionStatus.UNKNOWN

    @staticmethod
    def _decision(
        bundle: EvidenceBundle,
        *,
        judgment: CriterionJudgment,
        report: JudgmentVerification,
        rule_verification: Verification,
        status: str,
        route: str,
        criterion_status: CriterionStatus,
        confidence: float,
        notes: list[str],
        conflicts: list[str],
        reason: str,
    ) -> CriterionDecision:
        # 규칙에서 승계한 판단의 문구는 이미 규칙 설명과 같다. 모델이 직접 쓴
        # 이유만 설명에 덧붙여 같은 문장이 두 번 나오지 않게 한다.
        model_reason = judgment.reason.strip() if judgment.model_backed else ""
        if model_reason and model_reason not in notes:
            notes.append(model_reason)
        verification = replace(
            rule_verification,
            proposed_status=criterion_status,
            confidence=round(max(0.0, min(1.0, confidence)), 3),
            grounded=report.grounded or rule_verification.grounded,
            conflicts=tuple(dict.fromkeys(conflicts)),
            notes=tuple(dict.fromkeys(notes)),
        )
        return CriterionDecision(
            criterion_id=bundle.rule.criterion_id,
            criterion_type=bundle.rule.criterion_type,
            status=status,
            route=route,
            verification=verification,
            judgment=judgment,
            report=report,
            rule_status=rule_verification.proposed_status,
            reason=reason,
        )


__all__ = [
    "A2A",
    "DECIDED",
    "HUMAN_REVIEW",
    "CriterionDecision",
    "RuleAggregator",
]
