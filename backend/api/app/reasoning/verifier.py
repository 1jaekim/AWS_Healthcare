"""Evidence Verifier: 근거·단위·날짜 검증과 충돌 탐지.

Amazon Bedrock FM(NLI) 로 교체할 지점. 지금은 같은 반환 계약을 규칙으로 구현한다.
Verifier 는 상태를 '제안'만 하고, 확정은 Deterministic Aggregator 가 한다.
이렇게 나눠야 FM 이 붙어도 최종 판정의 재현성이 유지된다.
"""

from __future__ import annotations

from typing import Protocol

from ..domain.models import EvidenceBundle, Verification
from ..domain.states import CriterionKind, CriterionStatus
from ..safety.guardrails import LocalGuardrail
from ..tools.evidence_retrieval import EvidenceRetrievalTool

_LOW_CONFIDENCE = 0.55
_NARRATIVE_MIN_SCORE = 0.34
_ASSERTION_MIN_SCORE = 0.5
"""자유서술이 조건을 '주장'한다고 볼 최소 일치도.

측정값 나열처럼 단어만 스친 문장이 충돌로 오판되지 않게 막는 문턱이다.
"""


class Verifier(Protocol):
    """Bedrock 어댑터가 만족해야 하는 계약."""

    def verify(
        self, bundle: EvidenceBundle, *, run_id: str | None = None
    ) -> Verification: ...


class LocalEvidenceVerifier:
    """규칙 기반 검증기."""

    def __init__(self, guardrail: LocalGuardrail | None = None) -> None:
        self._guardrail = guardrail or LocalGuardrail(attach_disclaimer=False)

    def verify(
        self, bundle: EvidenceBundle, *, run_id: str | None = None
    ) -> Verification:
        """규칙으로 근거를 검증한다. run_id 는 모델 검증기와 호출부를 맞추기 위해 받는다."""
        rule = bundle.rule
        observation = bundle.primary_observation
        outcome = bundle.outcome
        conflicts: list[str] = []
        notes: list[str] = []

        grounded = bool(bundle.source_ids())
        if not grounded:
            notes.append("출처 ID가 없는 근거입니다.")

        # 단위 정합성 확인
        if observation is not None and rule.unit and observation.unit:
            if observation.unit not in {rule.unit, "boolean", "category", "count"}:
                notes.append(
                    f"단위 표기가 다릅니다: 기준 {rule.unit} / 관찰 {observation.unit}"
                )

        # 날짜 확인
        if observation is not None and not observation.observed_at:
            notes.append("관찰 시점이 기록되지 않았습니다.")

        # 관찰값 자체가 없으면 정보 부족
        if observation is None or observation.value is None:
            if bundle.narrative:
                return self._from_narrative_only(bundle, notes)
            return Verification(
                criterion_id=rule.criterion_id,
                proposed_status=CriterionStatus.UNKNOWN,
                confidence=0.0,
                grounded=grounded,
                notes=tuple(notes),
            )

        # 규칙 계산이 판정 불가면 정보 부족으로 본다
        if outcome is None or outcome.satisfied is None:
            notes.append(
                outcome.explanation if outcome else "규칙 계산 결과가 없습니다."
            )
            return Verification(
                criterion_id=rule.criterion_id,
                proposed_status=CriterionStatus.UNKNOWN,
                confidence=0.2,
                grounded=grounded,
                notes=tuple(notes),
            )

        confidence = 0.9 if grounded else 0.5

        # 파생 불리언은 구조화 값과 자유서술이 어긋나는지 대조한다
        if rule.kind is CriterionKind.DERIVED_BOOLEAN:
            conflict = self._boolean_conflict(bundle)
            if conflict is not None:
                conflicts.append(conflict)
                return Verification(
                    criterion_id=rule.criterion_id,
                    proposed_status=CriterionStatus.CONFLICTING,
                    confidence=0.4,
                    grounded=grounded,
                    conflicts=tuple(conflicts),
                    notes=tuple(notes),
                )
            confidence = 0.8 if bundle.narrative else 0.65

        if notes:
            confidence -= 0.15 * len(notes)
        confidence = max(0.0, min(1.0, round(confidence, 3)))

        if confidence < _LOW_CONFIDENCE:
            return Verification(
                criterion_id=rule.criterion_id,
                proposed_status=CriterionStatus.REVIEW_REQUIRED,
                confidence=confidence,
                grounded=grounded,
                notes=tuple(notes),
            )

        status = (
            CriterionStatus.EVIDENCE_FOUND
            if outcome.satisfied
            else CriterionStatus.CONTRADICTED
        )
        return Verification(
            criterion_id=rule.criterion_id,
            proposed_status=status,
            confidence=confidence,
            grounded=grounded,
            notes=tuple(notes),
        )

    def _from_narrative_only(
        self, bundle: EvidenceBundle, notes: list[str]
    ) -> Verification:
        """구조화 값이 없고 자유서술만 있는 경우."""
        best = max(bundle.narrative, key=lambda item: item.score)
        if best.score < _NARRATIVE_MIN_SCORE:
            return Verification(
                criterion_id=bundle.rule.criterion_id,
                proposed_status=CriterionStatus.UNKNOWN,
                confidence=round(best.score, 3),
                grounded=True,
                notes=(*notes, "자유서술 일치도가 낮습니다."),
            )
        return Verification(
            criterion_id=bundle.rule.criterion_id,
            proposed_status=CriterionStatus.REVIEW_REQUIRED,
            confidence=round(best.score, 3),
            grounded=True,
            notes=(*notes, "구조화 값 없이 자유서술 근거만 확인되었습니다."),
        )

    @staticmethod
    def _boolean_conflict(bundle: EvidenceBundle) -> str | None:
        """구조화 불리언과 자유서술 진술이 충돌하는지 확인한다."""
        observation = bundle.primary_observation
        if observation is None or not isinstance(observation.value, bool):
            return None
        # 조건을 실제로 주장하는 문장만 대조 대상으로 삼는다.
        assertive = [
            item
            for item in bundle.narrative
            if item.score >= _ASSERTION_MIN_SCORE
        ]
        if not assertive:
            return None
        narrative_negative = EvidenceRetrievalTool.implies_negative(assertive)
        if narrative_negative is None:
            return None
        narrative_positive = not narrative_negative
        if observation.value == narrative_positive:
            return None
        structured = "해당" if observation.value else "비해당"
        narrated = "해당" if narrative_positive else "비해당"
        return (
            f"구조화 기록은 '{structured}' 이나 자유서술은 '{narrated}' 로 읽힙니다."
        )
