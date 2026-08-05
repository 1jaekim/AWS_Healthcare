"""LLM 판단: 기준 한 건에 대한 OK / NOT_OK / UNKNOWN 제안.

MATCHING_MODEL_V2 의 `LLM 판단` 단계다. 모델은 상태를 확정하지 않고 제안만 하며,
그 제안은 곧바로 결정론적 `JudgmentVerifier` 와 `RuleAggregator` 로 넘어간다.

이 모듈이 지키는 두 가지 계약:

- 입력은 `evaluate_trial_criterion` 페이로드다. 기준의 연산자·값·단위·기간과
  근거 목록(`evidence_id` 포함)만 넣는다. 직접 식별자는 넣지 않는다.
- 출력은 `CriterionJudgment` 다. 모델 호출이 실패하거나 형식을 벗어나면 규칙
  계산 결과에서 판단을 승계한다. 판단이 조용히 비어버리지 않게 하는 장치다.

모델이 없으면(`model=None`) 규칙 승계 경로만 돈다. 자격 증명 없는 환경에서도
Verifier 와 Rule Aggregator 가 같은 입력 형태를 받도록 하기 위한 설계다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .contracts import CriterionStatus, EvidenceBundle, Tracer, Verification
from .model import Conversation, ModelClient, ModelError
from .prompts import CRITERION_JUDGE

ALLOWED_STATUS: tuple[str, ...] = ("OK", "NOT_OK", "UNKNOWN")
"""모델에게 허용하는 상태 어휘. v2 판단 모델의 3값 계약."""

_MAX_EVIDENCE = 8
"""한 기준에 실어 보내는 근거 상한. 프롬프트가 무한히 길어지지 않게 묶어둔다."""

_MAX_REASON = 400

#: 규칙 판정을 3값으로 투사한다. Rule Aggregator 도 같은 표를 쓴다.
RULE_STATUS_PROJECTION: dict[CriterionStatus, str] = {
    CriterionStatus.EVIDENCE_FOUND: "OK",
    CriterionStatus.CONTRADICTED: "NOT_OK",
    CriterionStatus.UNKNOWN: "UNKNOWN",
    CriterionStatus.CONFLICTING: "UNKNOWN",
    CriterionStatus.REVIEW_REQUIRED: "UNKNOWN",
}


@dataclass(frozen=True)
class CriterionJudgment:
    """LLM 판단 한 건. 상태는 제안이며 확정이 아니다."""

    criterion_id: str
    proposed_status: str
    confidence: float
    reason: str
    used_evidence_ids: tuple[str, ...] = ()
    missing_information: tuple[str, ...] = ()
    needs_a2a: bool = False
    origin: str = "model"
    """`model` 이면 모델 응답, `rule` 이면 규칙 계산에서 승계한 판단."""

    error: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def model_backed(self) -> bool:
        return self.origin == "model"

    def to_dict(self) -> dict[str, Any]:
        return {
            "criterion_id": self.criterion_id,
            "proposed_status": self.proposed_status,
            "confidence": self.confidence,
            "reason": self.reason,
            "used_evidence_ids": list(self.used_evidence_ids),
            "missing_information": list(self.missing_information),
            "needs_a2a": self.needs_a2a,
            "origin": self.origin,
            "error": self.error,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }


class CriterionJudgeAgent:
    """기준 한 건을 모델에게 판단시키고 규칙 승계로 폴백한다."""

    def __init__(
        self,
        *,
        model: ModelClient | None,
        trace: Tracer,
        max_evidence: int = _MAX_EVIDENCE,
    ) -> None:
        self._model = model
        self._trace = trace
        self._max_evidence = max(1, max_evidence)

    @property
    def mode(self) -> str:
        return getattr(self._model, "mode", "rule") if self._model else "rule"

    def judge(
        self,
        bundle: EvidenceBundle,
        *,
        rule_verification: Verification,
        run_id: str | None = None,
        patient_key: str | None = None,
        trial_title: str | None = None,
        index_date: str | None = None,
    ) -> CriterionJudgment:
        """기준 한 건에 대한 상태 제안을 만든다.

        `rule_verification` 은 규칙 검증기가 이미 낸 판정이다. 모델을 부르지 못하는
        경우 이 판정을 그대로 승계한다.
        """
        evidence = self.evidence_payload(bundle, limit=self._max_evidence)

        # 근거가 하나도 없으면 모델이 만들어낼 수 있는 것도 없다. 호출을 아낀다.
        if self._model is None or not evidence:
            return self.from_rule(
                bundle,
                rule_verification,
                reason_suffix=(
                    None if self._model is not None else "모델이 비활성입니다."
                ),
            )

        payload = self.request_payload(
            bundle,
            evidence=evidence,
            patient_key=patient_key,
            trial_title=trial_title,
            index_date=index_date,
        )
        conversation = Conversation()
        conversation.user_text(
            "다음 기준과 근거로 상태를 제안하라.\n\n"
            + json.dumps(payload, ensure_ascii=False, indent=2)
        )

        try:
            with self._trace.span(
                run_id or "unattached",
                "model:criterion_judge",
                "MODEL",
                criterion_id=bundle.rule.criterion_id,
                mode=self.mode,
                prompt=CRITERION_JUDGE.label,
                prompt_checksum=CRITERION_JUDGE.checksum,
            ) as attributes:
                response = self._model.converse(
                    conversation=conversation, system=CRITERION_JUDGE
                )
                attributes["input_tokens"] = response.input_tokens
                attributes["output_tokens"] = response.output_tokens
        except ModelError as exc:
            return self.from_rule(
                bundle,
                rule_verification,
                error=str(exc),
                reason_suffix="모델 판단 실패로 규칙 판정을 승계했습니다.",
            )

        parsed = response.json_payload()
        if parsed is None:
            return self.from_rule(
                bundle,
                rule_verification,
                error="invalid_response",
                reason_suffix="모델 응답 형식이 올바르지 않아 규칙 판정을 승계했습니다.",
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
            )

        status = str(parsed.get("proposed_status", "")).strip().upper()
        if status not in ALLOWED_STATUS:
            return self.from_rule(
                bundle,
                rule_verification,
                error="unknown_status",
                reason_suffix="모델이 허용되지 않은 상태를 반환해 규칙 판정을 승계했습니다.",
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
            )

        return CriterionJudgment(
            criterion_id=bundle.rule.criterion_id,
            proposed_status=status,
            confidence=_clamp(parsed.get("confidence"), rule_verification.confidence),
            reason=str(parsed.get("reason", "")).strip()[:_MAX_REASON],
            used_evidence_ids=_strings(parsed.get("used_evidence_ids")),
            missing_information=_strings(parsed.get("missing_information")),
            needs_a2a=bool(parsed.get("needs_a2a", False)),
            origin="model",
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
        )

    def judge_many(
        self,
        items: list[tuple[EvidenceBundle, Verification]],
        *,
        run_id: str | None = None,
        patient_key: str | None = None,
        trial_title: str | None = None,
        index_date: str | None = None,
    ) -> list[CriterionJudgment]:
        """한 공고의 근거 있는 기준을 모델 한 번으로 묶어 판단한다."""
        results: list[CriterionJudgment | None] = [None] * len(items)
        requests: list[dict[str, Any]] = []
        request_indexes: list[int] = []

        for index, (bundle, verification) in enumerate(items):
            evidence = self.evidence_payload(bundle, limit=self._max_evidence)
            if self._model is None or not evidence:
                results[index] = self.from_rule(
                    bundle,
                    verification,
                    reason_suffix=(
                        None if self._model is not None else "모델이 비활성입니다."
                    ),
                )
                continue
            requests.append(
                self.request_payload(
                    bundle,
                    evidence=evidence,
                    patient_key=patient_key,
                    trial_title=trial_title,
                    index_date=index_date,
                )
            )
            request_indexes.append(index)

        if not requests:
            return [item for item in results if item is not None]

        conversation = Conversation()
        conversation.user_text(
            "다음 기준들을 각각 판단하라. 입력 순서와 관계없이 criterion_id로 "
            "대응하고 JSON 객체만 반환하라.\n\n"
            + json.dumps(
                {"task": "evaluate_trial_criteria_batch", "items": requests},
                ensure_ascii=False,
                indent=2,
            )
        )
        try:
            with self._trace.span(
                run_id or "unattached",
                "model:criterion_judge_batch",
                "MODEL",
                criterion_count=len(requests),
                mode=self.mode,
                prompt=CRITERION_JUDGE.label,
                prompt_checksum=CRITERION_JUDGE.checksum,
            ) as attributes:
                response = self._model.converse(
                    conversation=conversation, system=CRITERION_JUDGE
                )
                attributes["input_tokens"] = response.input_tokens
                attributes["output_tokens"] = response.output_tokens
        except ModelError as exc:
            return [
                self.from_rule(
                    bundle,
                    verification,
                    error=str(exc),
                    reason_suffix="모델 일괄 판단 실패로 규칙 판정을 승계했습니다.",
                )
                for bundle, verification in items
            ]

        parsed = response.json_payload() or {}
        raw_judgments = parsed.get("judgments")
        by_id = {
            str(item.get("criterion_id", "")): item
            for item in raw_judgments or []
            if isinstance(item, dict)
        }
        tokens_recorded = False
        for index in request_indexes:
            bundle, verification = items[index]
            raw = by_id.get(bundle.rule.criterion_id)
            status = str((raw or {}).get("proposed_status", "")).strip().upper()
            if raw is None or status not in ALLOWED_STATUS:
                results[index] = self.from_rule(
                    bundle,
                    verification,
                    error="missing_or_invalid_batch_judgment",
                    reason_suffix="일괄 판단 결과가 없어 규칙 판정을 승계했습니다.",
                    input_tokens=response.input_tokens if not tokens_recorded else 0,
                    output_tokens=response.output_tokens if not tokens_recorded else 0,
                )
                tokens_recorded = True
                continue
            results[index] = CriterionJudgment(
                criterion_id=bundle.rule.criterion_id,
                proposed_status=status,
                confidence=_clamp(raw.get("confidence"), verification.confidence),
                reason=str(raw.get("reason", "")).strip()[:_MAX_REASON],
                used_evidence_ids=_strings(raw.get("used_evidence_ids")),
                missing_information=_strings(raw.get("missing_information")),
                needs_a2a=bool(raw.get("needs_a2a", False)),
                origin="model",
                input_tokens=response.input_tokens if not tokens_recorded else 0,
                output_tokens=response.output_tokens if not tokens_recorded else 0,
            )
            tokens_recorded = True
        return [item for item in results if item is not None]

    # -- 규칙 승계 --------------------------------------------------------

    @staticmethod
    def from_rule(
        bundle: EvidenceBundle,
        rule_verification: Verification,
        *,
        error: str | None = None,
        reason_suffix: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> CriterionJudgment:
        """규칙 검증 결과를 3값 판단으로 승계한다.

        모델이 없거나 실패한 경로다. 규칙이 이미 낸 판정을 그대로 옮기므로 이후
        Verifier 검사에서 규칙과 충돌하지 않는다.
        """
        status = RULE_STATUS_PROJECTION.get(
            rule_verification.proposed_status, "UNKNOWN"
        )
        outcome = bundle.outcome
        reasons = [outcome.explanation] if outcome and outcome.explanation else []
        reasons.extend(rule_verification.notes)
        if reason_suffix:
            reasons.append(reason_suffix)
        missing: list[str] = []
        if status == "UNKNOWN":
            missing.extend(rule_verification.notes or ("판정 근거가 부족합니다.",))
        return CriterionJudgment(
            criterion_id=bundle.rule.criterion_id,
            proposed_status=status,
            confidence=rule_verification.confidence,
            reason=" ".join(reasons)[:_MAX_REASON],
            used_evidence_ids=tuple(dict.fromkeys(bundle.source_ids()))
            if status in {"OK", "NOT_OK"}
            else (),
            missing_information=tuple(dict.fromkeys(missing)),
            needs_a2a=bool(rule_verification.conflicts),
            origin="rule",
            error=error,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    # -- 페이로드 --------------------------------------------------------

    @staticmethod
    def evidence_payload(
        bundle: EvidenceBundle, *, limit: int = _MAX_EVIDENCE
    ) -> list[dict[str, Any]]:
        """근거 목록을 `evidence_id` 가 붙은 형태로 만든다.

        구조화 관찰값과 자유서술을 같은 형태로 싣는다. 모델이 인용할 수 있는
        식별자를 여기서만 제공하므로, Verifier 는 이 목록으로 인용을 검사한다.
        """
        items: list[dict[str, Any]] = []
        for observation in bundle.observations:
            if observation.value is None:
                continue
            items.append(
                {
                    "evidence_id": observation.source_id,
                    "source_type": observation.source,
                    "observed_at": observation.observed_at,
                    "text": observation.detail or "",
                    "structured_value": observation.value,
                    "unit": observation.unit,
                }
            )
        for snippet in bundle.narrative:
            items.append(
                {
                    "evidence_id": snippet.note_id,
                    "source_type": "NARRATIVE_EMR",
                    "observed_at": snippet.note_date,
                    "text": snippet.snippet,
                    "structured_value": None,
                    "unit": None,
                    "score": snippet.score,
                }
            )
        return items[:limit]

    @staticmethod
    def request_payload(
        bundle: EvidenceBundle,
        *,
        evidence: list[dict[str, Any]],
        patient_key: str | None,
        trial_title: str | None,
        index_date: str | None,
    ) -> dict[str, Any]:
        """`evaluate_trial_criterion` 입력을 만든다."""
        rule = bundle.rule
        outcome = bundle.outcome
        return {
            "task": "evaluate_trial_criterion",
            "patient_key": patient_key,
            "trial": {
                "trial_id": getattr(rule, "trial_id", None),
                "title": trial_title,
            },
            "criterion": {
                "criterion_id": rule.criterion_id,
                "type": rule.criterion_type,
                "label": rule.label,
                "field": rule.field_name,
                "operator": rule.operator,
                "value": rule.value_low,
                "value_high": rule.value_high,
                "unit": rule.unit,
                "time_window_days": rule.time_window_days,
                "condition": rule.expected_repr(),
            },
            "index_date": index_date,
            "evidence": evidence,
            "rule_outcome": {
                "satisfied": outcome.satisfied if outcome else None,
                "explanation": outcome.explanation if outcome else None,
            },
            "allowed_status": list(ALLOWED_STATUS),
        }


def _clamp(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return round(max(0.0, min(1.0, number)), 3)


def _strings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(
        str(item).strip() for item in value if str(item).strip()
    )


__all__ = [
    "ALLOWED_STATUS",
    "RULE_STATUS_PROJECTION",
    "CriterionJudgeAgent",
    "CriterionJudgment",
]
