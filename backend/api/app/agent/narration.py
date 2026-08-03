"""FM 기반 설명 생성과 질문 작성.

모델이 문장을 만들고, Guardrails 가 그 문장을 검사한다. 순서를 바꾸지 않는다.
모델 출력이 비거나 실패하면 템플릿 결과로 폴백한다.
"""

from __future__ import annotations

import json
from typing import Any

from ..actions.explanation import Explanation, ExplanationAgent
from ..actions.next_best import EvidenceRequest, NextBestEvidenceAgent
from ..domain.models import CriterionResult
from ..domain.states import CriterionStatus
from ..reasoning.aggregator import AggregateOutcome
from ..safety.guardrails import LocalGuardrail
from ..safety.observability import TraceCollector
from .model import Conversation, ModelClient, ModelError
from .prompts import EXPLANATION_ADMIN, EXPLANATION_PATIENT, QUESTION_WRITER

_MAX_ITEMS_IN_PROMPT = 12


class ModelExplanationAgent:
    """모델이 설명을 쓰고 Guardrails 가 검사한다."""

    def __init__(
        self,
        *,
        model: ModelClient,
        guardrail: LocalGuardrail,
        fallback: ExplanationAgent,
        trace: TraceCollector,
    ) -> None:
        self._model = model
        self._guardrail = guardrail
        self._fallback = fallback
        self._trace = trace

    def explain(
        self,
        *,
        outcome: AggregateOutcome,
        results: list[CriterionResult],
        audience: str,
        run_id: str | None = None,
    ) -> Explanation:
        source_ids = [sid for item in results for sid in item.source_ids]
        # 출처가 없으면 모델을 부르지 않는다. Contextual Grounding 선결 조건.
        grounding = self._guardrail.check_grounding(
            "설명 생성 요청", source_ids=source_ids
        )
        if grounding.blocked:
            return self._fallback.explain(
                outcome=outcome, results=results, audience=audience
            )

        payload = self._payload(outcome, results, audience)
        conversation = Conversation()
        conversation.user_text(
            "다음 판정 결과를 설명하라.\n\n"
            + json.dumps(payload, ensure_ascii=False, indent=2)
        )
        system = (
            EXPLANATION_PATIENT if audience == "patient" else EXPLANATION_ADMIN
        )

        try:
            with self._trace.span(
                run_id or "unattached",
                "model:explanation",
                "MODEL",
                audience=audience,
                mode=getattr(self._model, "mode", "unknown"),
            ) as attributes:
                response = self._model.converse(
                    conversation=conversation, system=system
                )
                attributes["input_tokens"] = response.input_tokens
                attributes["output_tokens"] = response.output_tokens
        except ModelError:
            return self._fallback.explain(
                outcome=outcome, results=results, audience=audience
            )

        parsed = response.json_payload()
        summary = str((parsed or {}).get("summary", "")).strip()
        if not summary:
            return self._fallback.explain(
                outcome=outcome, results=results, audience=audience
            )

        highlights = [
            str(item).strip()
            for item in (parsed or {}).get("highlights", [])
            if str(item).strip()
        ]
        if not highlights:
            # 모델이 항목별 설명을 비우면 템플릿 항목을 쓴다.
            highlights = self._fallback.explain(
                outcome=outcome, results=results, audience=audience
            ).highlights

        return self._review(summary, highlights, audience)

    def _review(
        self, summary: str, highlights: list[str], audience: str
    ) -> Explanation:
        """생성 문장을 Guardrails 로 검사한다."""
        verdict = self._guardrail.review(summary, audience=audience)
        if verdict.blocked:
            return Explanation(
                audience=audience,
                summary=verdict.text,
                blocked=True,
                guardrail_findings=[
                    {"rule_id": item.rule_id, "severity": item.severity}
                    for item in verdict.findings
                ],
            )

        reviewed: list[str] = []
        findings = [
            {"rule_id": item.rule_id, "severity": item.severity}
            for item in verdict.findings
        ]
        for line in highlights:
            checked = self._guardrail.review(line, audience=audience)
            if checked.blocked:
                continue
            reviewed.append(checked.text)
            findings.extend(
                {"rule_id": item.rule_id, "severity": item.severity}
                for item in checked.findings
            )

        return Explanation(
            audience=audience,
            summary=verdict.text,
            highlights=reviewed,
            blocked=False,
            guardrail_findings=findings,
        )

    @staticmethod
    def _payload(
        outcome: AggregateOutcome, results: list[CriterionResult], audience: str
    ) -> dict[str, Any]:
        return {
            "task": "explain",
            "audience": audience,
            "eligibility_status": str(outcome.eligibility_status),
            "decision_label": outcome.decision_label,
            "criteria_met": outcome.criteria_met,
            "criteria_total": outcome.criteria_total,
            "criteria": [
                {
                    "criterion_id": item.criterion_id,
                    "type": item.criterion_type,
                    "label": item.label,
                    "status": str(item.status),
                    "observed_value": item.observed_value,
                    "expected_condition": item.expected_condition,
                    "unit": item.unit,
                    "observed_at": item.observed_at,
                    "source_ids": list(item.source_ids),
                }
                for item in results[:_MAX_ITEMS_IN_PROMPT]
            ],
        }

    def explain_both(
        self,
        *,
        outcome: AggregateOutcome,
        results: list[CriterionResult],
        run_id: str | None = None,
    ) -> dict[str, dict[str, Any]]:
        return {
            "admin": self.explain(
                outcome=outcome, results=results, audience="admin", run_id=run_id
            ).to_dict(),
            "patient": self.explain(
                outcome=outcome,
                results=results,
                audience="patient",
                run_id=run_id,
            ).to_dict(),
        }


class ModelNextBestEvidenceAgent:
    """정보 가치 계산은 결정론적으로 두고, 질문 문장만 모델이 쓴다.

    우선순위가 모델 호출마다 흔들리면 검토 큐 순서가 불안정해진다.
    그래서 순위는 규칙으로 정하고 표현만 모델에 맡긴다.
    """

    def __init__(
        self,
        *,
        model: ModelClient,
        guardrail: LocalGuardrail,
        fallback: NextBestEvidenceAgent,
        trace: TraceCollector,
    ) -> None:
        self._model = model
        self._guardrail = guardrail
        self._fallback = fallback
        self._trace = trace

    def propose(
        self,
        run_id: str,
        results: list[CriterionResult],
        *,
        limit: int = 5,
    ) -> list[EvidenceRequest]:
        base = self._fallback.propose(run_id, results, limit=limit)
        if not base:
            return []

        by_id = {item.criterion_id: item for item in results}
        rewritten: list[EvidenceRequest] = []
        for request in base:
            question = self._write_question(run_id, request, by_id.get(request.criterion_id))
            rewritten.append(
                EvidenceRequest(
                    request_id=request.request_id,
                    criterion_id=request.criterion_id,
                    field_name=request.field_name,
                    label=request.label,
                    reason=request.reason,
                    question=question,
                    information_value=request.information_value,
                    effort=request.effort,
                    priority=request.priority,
                    target=request.target,
                )
            )
        return rewritten

    def _write_question(
        self,
        run_id: str,
        request: EvidenceRequest,
        result: CriterionResult | None,
    ) -> str:
        """모델에게 질문 한 문장을 받아 Guardrails 로 검사한다."""
        payload = {
            "task": "question",
            "label": request.label,
            "field": request.field_name,
            "status": str(result.status) if result else str(CriterionStatus.UNKNOWN),
            "reason": request.reason,
            "expected_condition": result.expected_condition if result else None,
            "target": request.target,
        }
        conversation = Conversation()
        conversation.user_text(
            "다음 항목을 확인하기 위한 질문을 한 문장으로 작성하라.\n\n"
            + json.dumps(payload, ensure_ascii=False, indent=2)
        )

        try:
            with self._trace.span(
                run_id,
                "model:question_writer",
                "MODEL",
                criterion_id=request.criterion_id,
                mode=getattr(self._model, "mode", "unknown"),
            ):
                response = self._model.converse(
                    conversation=conversation, system=QUESTION_WRITER
                )
        except ModelError:
            return request.question

        parsed = response.json_payload()
        question = str((parsed or {}).get("question", "")).strip()
        if not question:
            return request.question

        verdict = self._guardrail.review(question, audience="patient")
        if verdict.blocked:
            return request.question
        return verdict.text
