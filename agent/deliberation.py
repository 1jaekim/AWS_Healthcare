"""미해소 기준에 대한 제한형 에이전트 토론.

두 모델 역할이 각각 한 번만 검토하고 결정론적 합의기가 추천을 만든다.
추천은 최종 판정을 바꾸지 않으며, 근거가 없거나 의견이 다르면 UNKNOWN을 유지한다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Sequence

from .contracts import CriterionResult, CriterionStatus, Tracer
from .model import Conversation, ModelClient, ModelError
from .prompts import UNKNOWN_DELIBERATION

_OPEN_STATUSES = frozenset(
    {
        CriterionStatus.UNKNOWN,
        CriterionStatus.CONFLICTING,
        CriterionStatus.REVIEW_REQUIRED,
    }
)
_RECOMMENDATIONS = frozenset({"OK", "NOT_OK", "UNKNOWN"})


def _evidence_text(criteria: list[dict[str, Any]]) -> str:
    """Guardrail 이 검사할 외부 유입 텍스트만 모은다.

    환자 근거 서술과 관찰값이 외부 입력이다. 블록이 비면 Bedrock 이 요청을
    거부하므로 비어 있을 때는 자리표시자를 넣는다.
    """
    parts: list[str] = []
    for item in criteria:
        for narrative in item.get("narratives") or []:
            text = str(narrative.get("text") or "").strip()
            if text:
                parts.append(text)
        observed = item.get("observed_value")
        if observed not in (None, ""):
            parts.append(str(observed))
    return "\n".join(parts) if parts else "(근거 서술 없음)"
_MAX_ROUNDS = 2
_MAX_CRITERIA = 5


@dataclass(frozen=True)
class DeliberationItem:
    criterion_id: str
    recommendation: str
    agreement: bool
    grounded: bool
    advocate: str
    skeptic: str
    source_ids: tuple[str, ...] = ()
    rationale: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "criterion_id": self.criterion_id,
            "recommendation": self.recommendation,
            "agreement": self.agreement,
            "grounded": self.grounded,
            "advocate": self.advocate,
            "skeptic": self.skeptic,
            "source_ids": list(self.source_ids),
            "rationale": list(self.rationale),
        }


@dataclass
class DeliberationResult:
    enabled: bool = True
    rounds: int = 0
    max_rounds: int = _MAX_ROUNDS
    stopped_reason: str = "nothing_to_review"
    items: list[DeliberationItem] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "rounds": self.rounds,
            "max_rounds": self.max_rounds,
            "stopped_reason": self.stopped_reason,
            "items": [item.to_dict() for item in self.items],
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "error": self.error,
        }


class UnknownDeliberationAgent:
    """검토자 1회 + 반론자 1회로 UNKNOWN 추천을 심의한다."""

    def __init__(
        self,
        *,
        model: ModelClient,
        trace: Tracer,
        max_criteria: int = 5,
    ) -> None:
        self._model = model
        self._trace = trace
        self._max_criteria = min(_MAX_CRITERIA, max(1, max_criteria))

    def deliberate(
        self,
        results: Sequence[CriterionResult],
        *,
        run_id: str | None = None,
    ) -> DeliberationResult:
        open_items = sorted(
            (item for item in results if item.status in _OPEN_STATUSES),
            key=self._review_priority,
        )
        selected = open_items[: self._max_criteria]
        result = DeliberationResult()
        if not selected:
            return result

        payload = [self._criterion_payload(item) for item in selected]
        first = self._review(
            role="evidence_reviewer",
            criteria=payload,
            run_id=run_id,
            round_number=1,
        )
        if first[0] is None:
            result.rounds = 1
            result.stopped_reason = "model_error"
            result.error = first[3]
            return result

        result.rounds = 1
        result.input_tokens += first[1]
        result.output_tokens += first[2]

        second = self._review(
            role="challenge_reviewer",
            criteria=payload,
            prior=first[0],
            run_id=run_id,
            round_number=2,
        )
        if second[0] is None:
            result.rounds = 2
            result.stopped_reason = "model_error"
            result.error = second[3]
            return result

        result.rounds = 2
        result.input_tokens += second[1]
        result.output_tokens += second[2]
        result.items = self._arbitrate(selected, first[0], second[0])
        result.stopped_reason = (
            "criteria_limit"
            if len(open_items) > self._max_criteria
            else "bounded_consensus"
        )
        return result

    @staticmethod
    def _review_priority(item: CriterionResult) -> tuple[int, str]:
        """충돌·사람 검토·제외 기준을 먼저 검토한다."""
        if item.status is CriterionStatus.REVIEW_REQUIRED:
            priority = 0
        elif item.status is CriterionStatus.CONFLICTING:
            priority = 1
        elif item.criterion_type == "EXCLUSION":
            priority = 2
        else:
            priority = 3
        return priority, item.criterion_id

    def _review(
        self,
        *,
        role: str,
        criteria: list[dict[str, Any]],
        run_id: str | None,
        round_number: int,
        prior: dict[str, dict[str, Any]] | None = None,
    ) -> tuple[dict[str, dict[str, Any]] | None, int, int, str | None]:
        request: dict[str, Any] = {"role": role, "criteria": criteria}
        if prior is not None:
            request["prior_review"] = list(prior.values())
        conversation = Conversation()
        # Guardrail 검사 범위를 외부 유입 텍스트로 좁힌다. 기준 JSON 과 역할
        # 지시문까지 평가되면 임상 서술이 오탐으로 막혀 판정이 사라진다.
        conversation.user_blocks(
            [
                {"text": json.dumps(request, ensure_ascii=False)},
                Conversation.guarded(_evidence_text(criteria)),
            ]
        )

        try:
            with self._trace.span(
                run_id or "unattached",
                "model:unknown_deliberation",
                "MODEL",
                role=role,
                round=round_number,
                prompt=UNKNOWN_DELIBERATION.label,
                prompt_checksum=UNKNOWN_DELIBERATION.checksum,
            ) as attributes:
                response = self._model.converse(
                    conversation=conversation,
                    system=UNKNOWN_DELIBERATION,
                )
                attributes["input_tokens"] = response.input_tokens
                attributes["output_tokens"] = response.output_tokens
        except ModelError as exc:
            return None, 0, 0, str(exc)

        parsed = response.json_payload()
        decisions = parsed.get("decisions") if isinstance(parsed, dict) else None
        if not isinstance(decisions, list):
            return None, response.input_tokens, response.output_tokens, "invalid_response"

        normalized: dict[str, dict[str, Any]] = {}
        for decision in decisions:
            if not isinstance(decision, dict):
                continue
            criterion_id = str(decision.get("criterion_id", "")).strip()
            recommendation = str(decision.get("recommendation", "UNKNOWN")).upper()
            if not criterion_id:
                continue
            if recommendation not in _RECOMMENDATIONS:
                recommendation = "UNKNOWN"
            source_ids = decision.get("source_ids")
            normalized[criterion_id] = {
                "criterion_id": criterion_id,
                "recommendation": recommendation,
                "source_ids": [
                    str(value) for value in source_ids or [] if str(value).strip()
                ],
                "rationale": str(decision.get("rationale", "")).strip(),
            }
        return normalized, response.input_tokens, response.output_tokens, None

    @staticmethod
    def _criterion_payload(item: CriterionResult) -> dict[str, Any]:
        return {
            "criterion_id": item.criterion_id,
            "criterion_type": item.criterion_type,
            "label": item.label,
            "expected_condition": item.expected_condition,
            "observed_value": item.observed_value,
            "observed_at": item.observed_at,
            "status": str(item.status),
            "confidence": item.confidence,
            "source_ids": list(item.source_ids),
            "narratives": [
                {
                    "source_id": snippet.note_id,
                    "observed_at": snippet.note_date,
                    "text": snippet.snippet,
                    "score": snippet.score,
                }
                for snippet in item.narrative[:3]
            ],
            "conflicts": list(item.conflicts),
        }

    @staticmethod
    def _arbitrate(
        selected: Sequence[CriterionResult],
        first: dict[str, dict[str, Any]],
        second: dict[str, dict[str, Any]],
    ) -> list[DeliberationItem]:
        items: list[DeliberationItem] = []
        for criterion in selected:
            left = first.get(criterion.criterion_id, {})
            right = second.get(criterion.criterion_id, {})
            left_rec = str(left.get("recommendation", "UNKNOWN"))
            right_rec = str(right.get("recommendation", "UNKNOWN"))
            allowed_sources = set(criterion.source_ids)
            left_sources = set(left.get("source_ids") or [])
            right_sources = set(right.get("source_ids") or [])
            cited = left_sources | right_sources
            grounded = bool(allowed_sources) and bool(cited) and cited <= allowed_sources
            agreement = left_rec == right_rec
            recommendation = (
                left_rec
                if agreement and grounded and left_rec in {"OK", "NOT_OK"}
                else "UNKNOWN"
            )
            rationale = tuple(
                value
                for value in (
                    str(left.get("rationale", "")).strip(),
                    str(right.get("rationale", "")).strip(),
                )
                if value
            )
            items.append(
                DeliberationItem(
                    criterion_id=criterion.criterion_id,
                    recommendation=recommendation,
                    agreement=agreement,
                    grounded=grounded,
                    advocate=left_rec,
                    skeptic=right_rec,
                    source_ids=tuple(sorted(cited & allowed_sources)),
                    rationale=rationale,
                )
            )
        return items


__all__ = [
    "DeliberationItem",
    "DeliberationResult",
    "UnknownDeliberationAgent",
]
