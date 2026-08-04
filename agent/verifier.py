"""FM 기반 Evidence Verifier.

모델이 NLI 로 근거와 조건의 관계를 판정한다. 다만 모델 출력은 '제안'이며,
최종 상태는 Deterministic Aggregator 가 확정한다.

모델 호출이 실패하거나 응답이 형식을 벗어나면 로컬 규칙 검증기로 폴백한다.
판정이 조용히 비어버리는 상황을 만들지 않기 위한 장치다.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from .contracts import (
    CriterionStatus,
    EvidenceBundle,
    EvidenceVerifier,
    Tracer,
    Verification,
)
from .model import Conversation, ModelClient, ModelError
from .prompts import NLI_VERIFIER

_ENTAILMENT_MAP: dict[str, CriterionStatus] = {
    "SUPPORTED": CriterionStatus.EVIDENCE_FOUND,
    "CONTRADICTED": CriterionStatus.CONTRADICTED,
    "NOT_ENOUGH_INFO": CriterionStatus.UNKNOWN,
    "CONFLICTING": CriterionStatus.CONFLICTING,
}

_LOW_CONFIDENCE = 0.55


class ModelEvidenceVerifier:
    """모델 NLI 검증기. 실패 시 로컬 규칙 검증기로 폴백한다."""

    def __init__(
        self,
        *,
        model: ModelClient,
        fallback: EvidenceVerifier,
        trace: Tracer,
    ) -> None:
        self._model = model
        self._fallback = fallback
        self._trace = trace

    def verify(
        self, bundle: EvidenceBundle, *, run_id: str | None = None
    ) -> Verification:
        rule = bundle.rule
        local = self._fallback.verify(bundle)

        # 관찰값도 자유서술도 없으면 모델을 부를 이유가 없다.
        if bundle.primary_observation is None and not bundle.narrative:
            return local

        payload = self._payload(bundle)
        conversation = Conversation()
        conversation.user_text(
            "다음 기준과 근거의 관계를 판정하라.\n\n"
            + json.dumps(payload, ensure_ascii=False, indent=2)
        )

        span_run = run_id or "unattached"
        try:
            with self._trace.span(
                span_run,
                "model:nli_verifier",
                "MODEL",
                criterion_id=rule.criterion_id,
                mode=getattr(self._model, "mode", "unknown"),
                prompt=NLI_VERIFIER.label,
                prompt_checksum=NLI_VERIFIER.checksum,
            ) as attributes:
                response = self._model.converse(
                    conversation=conversation, system=NLI_VERIFIER
                )
                attributes["input_tokens"] = response.input_tokens
                attributes["output_tokens"] = response.output_tokens
        except ModelError:
            return self._with_note(local, "모델 검증 실패로 규칙 판정을 사용했습니다.")

        parsed = response.json_payload()
        if parsed is None:
            return self._with_note(
                local, "모델 응답 형식이 올바르지 않아 규칙 판정을 사용했습니다."
            )

        proposed = self._map_status(parsed)
        if proposed is None:
            return self._with_note(
                local, "모델이 알 수 없는 판정을 반환해 규칙 판정을 사용했습니다."
            )

        confidence = self._clamp(parsed.get("confidence"), local.confidence)
        conflicts = self._strings(parsed.get("conflicts"))
        rationale = str(parsed.get("rationale", "")).strip()

        notes: list[str] = list(local.notes)
        if rationale:
            notes.append(rationale)

        # 모델이 규칙 계산과 다른 결론을 내면 사람 검토로 올린다.
        divergent = self._diverges(local.proposed_status, proposed)
        if divergent:
            notes.append(
                f"모델 판정({proposed})과 규칙 판정({local.proposed_status})이 "
                "달라 검토가 필요합니다."
            )
            # 폴백 결과를 복사해 필드만 바꾼다. 구체 타입을 알 필요가 없다.
            return replace(
                local,
                proposed_status=CriterionStatus.REVIEW_REQUIRED,
                confidence=min(confidence, 0.5),
                conflicts=tuple(conflicts),
                notes=tuple(notes),
            )

        if confidence < _LOW_CONFIDENCE and proposed in (
            CriterionStatus.EVIDENCE_FOUND,
            CriterionStatus.CONTRADICTED,
        ):
            proposed = CriterionStatus.REVIEW_REQUIRED

        return replace(
            local,
            proposed_status=proposed,
            confidence=confidence,
            conflicts=tuple(conflicts) or local.conflicts,
            notes=tuple(notes),
        )

    # -- 내부 ------------------------------------------------------------

    @staticmethod
    def _payload(bundle: EvidenceBundle) -> dict[str, Any]:
        """모델에 넘길 근거 묶음. 출처 ID를 함께 보내 grounding 을 강제한다."""
        rule = bundle.rule
        observation = bundle.primary_observation
        outcome = bundle.outcome
        return {
            "task": "nli",
            "criterion": {
                "criterion_id": rule.criterion_id,
                "type": rule.criterion_type,
                "label": rule.label,
                "field": rule.field_name,
                "condition": rule.expected_repr(),
                "unit": rule.unit,
            },
            "has_observation": observation is not None
            and observation.value is not None,
            "observation": (
                {
                    "value": observation.value,
                    "unit": observation.unit,
                    "observed_at": observation.observed_at,
                    "source_id": observation.source_id,
                    "detail": observation.detail,
                }
                if observation is not None
                else None
            ),
            "rule_satisfied": outcome.satisfied if outcome else None,
            "rule_explanation": outcome.explanation if outcome else None,
            "narrative": [
                {
                    "note_id": item.note_id,
                    "note_date": item.note_date,
                    "snippet": item.snippet,
                    "score": item.score,
                }
                for item in bundle.narrative
            ],
        }

    @staticmethod
    def _map_status(parsed: dict[str, Any]) -> CriterionStatus | None:
        raw = str(parsed.get("entailment", "")).strip().upper()
        return _ENTAILMENT_MAP.get(raw)

    @staticmethod
    def _clamp(value: Any, default: float) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        return round(max(0.0, min(1.0, number)), 3)

    @staticmethod
    def _strings(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item) for item in value if str(item).strip()]

    @staticmethod
    def _diverges(local: CriterionStatus, model: CriterionStatus) -> bool:
        """규칙과 모델이 서로 반대 결론을 낸 경우만 발산으로 본다."""
        opposing = {
            (CriterionStatus.EVIDENCE_FOUND, CriterionStatus.CONTRADICTED),
            (CriterionStatus.CONTRADICTED, CriterionStatus.EVIDENCE_FOUND),
        }
        return (local, model) in opposing

    @staticmethod
    def _with_note(verification: Verification, note: str) -> Verification:
        return replace(verification, notes=(*verification.notes, note))
