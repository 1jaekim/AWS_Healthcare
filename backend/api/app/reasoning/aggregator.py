"""Deterministic Aggregator: 재현 가능한 상태 취합.

FM 을 호출하지 않는다. Verifier 가 제안한 상태와 규칙 계산 결과만으로 최종 상태를
확정하므로, 동일 입력에 대해 항상 동일한 판정이 나온다.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..domain.models import CriterionResult, EvidenceBundle, Verification
from ..domain.states import CriterionStatus, EligibilityStatus


@dataclass(frozen=True)
class AggregateOutcome:
    """실행 단위 종합 판정."""

    eligibility_status: EligibilityStatus
    decision_label: str
    criteria_total: int
    criteria_met: int
    blocking_criteria: tuple[str, ...]
    open_criteria: tuple[str, ...]
    review_criteria: tuple[str, ...]

    @property
    def screening_decision(self) -> str:
        """외부 사용자에게 노출하는 3값 추천 코드."""
        if self.eligibility_status is EligibilityStatus.ELIGIBLE:
            return "OK"
        if self.eligibility_status is EligibilityStatus.INELIGIBLE:
            return "NOT_OK"
        return "UNKNOWN"


_DECISION_LABELS: dict[EligibilityStatus, str] = {
    EligibilityStatus.ELIGIBLE: "사전 적합",
    EligibilityStatus.INELIGIBLE: "사전 부적합",
    EligibilityStatus.NEEDS_MORE_EVIDENCE: "추가 근거 필요",
    EligibilityStatus.REVIEW_REQUIRED: "사람 검토 필요",
}


class DeterministicAggregator:
    """기준별 상태 확정과 종합 판정을 담당한다."""

    def finalize(
        self, bundle: EvidenceBundle, verification: Verification
    ) -> CriterionResult:
        """번들과 검증 결과를 하나의 확정 결과로 환원한다."""
        rule = bundle.rule
        observation = bundle.primary_observation
        outcome = bundle.outcome

        status = self._resolve_status(rule.criterion_type, verification)
        explanation = self._explanation(verification, outcome)

        return CriterionResult(
            criterion_id=rule.criterion_id,
            criterion_type=rule.criterion_type,
            label=rule.label,
            field_name=rule.field_name,
            kind=rule.kind,
            status=status,
            observed_value=outcome.observed_repr if outcome else None,
            expected_condition=rule.expected_repr(),
            unit=rule.unit,
            observed_at=observation.observed_at if observation else None,
            confidence=verification.confidence,
            explanation=explanation,
            source_ids=tuple(dict.fromkeys(bundle.source_ids())),
            narrative=tuple(bundle.narrative),
            conflicts=verification.conflicts,
            rule_satisfied=outcome.satisfied if outcome else None,
            patient_reported=bool(
                observation is not None
                and observation.source in {"PATIENT_REPORTED", "APPLICATION"}
            ),
        )

    def _resolve_status(
        self, criterion_type: str, verification: Verification
    ) -> CriterionStatus:
        """제외 기준은 충족되면 부적합 신호가 된다는 점을 반영한다.

        EXCLUSION 기준의 규칙은 '위험 없음'을 조건으로 기술되어 있으므로
        (예: active_pregnancy = false), 규칙 충족이 곧 통과다.
        따라서 상태 매핑 자체는 INCLUSION 과 동일하게 두고,
        종합 판정에서 CONTRADICTED 를 차단 신호로 사용한다.
        """
        return verification.proposed_status

    @staticmethod
    def _explanation(verification: Verification, outcome) -> str:
        parts: list[str] = []
        if outcome is not None and outcome.explanation:
            parts.append(outcome.explanation)
        parts.extend(verification.conflicts)
        parts.extend(verification.notes)
        if not parts:
            parts.append("판정 근거를 확인할 수 없습니다.")
        return " ".join(parts)

    def aggregate(self, results: list[CriterionResult]) -> AggregateOutcome:
        """기준별 결과를 종합해 실행 상태를 확정한다."""
        blocking = tuple(
            item.criterion_id for item in results if item.is_blocking()
        )
        open_items = tuple(
            item.criterion_id
            for item in results
            if item.status in (CriterionStatus.UNKNOWN, CriterionStatus.CONFLICTING)
        )
        review = tuple(
            item.criterion_id
            for item in results
            if item.status is CriterionStatus.REVIEW_REQUIRED
        )
        met = sum(
            1 for item in results if item.status is CriterionStatus.EVIDENCE_FOUND
        )

        status = self._overall(results, blocking, open_items, review)
        return AggregateOutcome(
            eligibility_status=status,
            decision_label=_DECISION_LABELS[status],
            criteria_total=len(results),
            criteria_met=met,
            blocking_criteria=blocking,
            open_criteria=open_items,
            review_criteria=review,
        )

    @staticmethod
    def _overall(
        results: list[CriterionResult],
        blocking: tuple[str, ...],
        open_items: tuple[str, ...],
        review: tuple[str, ...],
    ) -> EligibilityStatus:
        """판정 우선순위: 부적합 > 검토 필요 > 근거 부족 > 적합."""
        if not results:
            return EligibilityStatus.NEEDS_MORE_EVIDENCE
        if blocking:
            return EligibilityStatus.INELIGIBLE
        if review:
            return EligibilityStatus.REVIEW_REQUIRED
        if open_items:
            return EligibilityStatus.NEEDS_MORE_EVIDENCE
        return EligibilityStatus.ELIGIBLE
