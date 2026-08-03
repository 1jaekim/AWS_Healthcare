"""Tool-use 에이전트 루프.

모델이 추가 근거 수집을 판단하고 도구를 호출한다. 도구 호출은 반드시 Gateway 를
경유하므로 권한 검사와 호출 로깅이 유지된다. 반복 상한과 도구 화이트리스트로
비용과 권한 범위를 묶는다.

이 루프는 근거를 '보강'만 한다. 판정 상태는 만들지 않는다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .contracts import (
    ExecutionPlan,
    NarrativeSnippet,
    Observation,
    ToolContext,
    ToolGateway,
    ToolPermissionDenied,
    Tracer,
)
from .model import Conversation, ModelClient, ModelError
from .prompts import EVIDENCE_PLANNER
from .toolspec import AGENT_TOOLS, ALLOWED_TOOL_NAMES


@dataclass
class GatheringResult:
    """루프가 보강한 근거."""

    narratives: dict[str, list[NarrativeSnippet]] = field(default_factory=dict)
    observations: dict[str, Observation] = field(default_factory=dict)
    iterations: int = 0
    tool_calls: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    stopped_reason: str = "no_model"
    error: str | None = None
    attempted: set[str] = field(default_factory=set)
    """검색을 이미 시도한 기준. 결과가 비어도 다시 묻지 않게 한다."""
    attempted_fields: set[str] = field(default_factory=set)
    """조회를 이미 시도한 필드. 데이터에 값이 없어도 다시 묻지 않게 한다."""


class EvidenceGatheringAgent:
    """모델이 주도하는 근거 보강 루프."""

    def __init__(
        self,
        *,
        model: ModelClient,
        gateway: ToolGateway,
        trace: Tracer,
        max_iterations: int = 4,
    ) -> None:
        self._model = model
        self._gateway = gateway
        self._trace = trace
        self._max_iterations = max_iterations
        self._term_index: dict[str, tuple[str, ...]] = {}

    def gather(
        self,
        context: ToolContext,
        plan: ExecutionPlan,
        *,
        known_narratives: dict[str, list[NarrativeSnippet]],
        known_observations: dict[str, Observation],
    ) -> GatheringResult:
        """미해소 근거가 있으면 모델에게 수집 계획을 묻고 도구를 호출한다."""
        result = GatheringResult(
            narratives={k: list(v) for k, v in known_narratives.items()},
            observations=dict(known_observations),
        )

        pending = self._pending_narrative(plan, result)
        missing = self._missing_observations(plan, result)
        if not pending and not missing:
            result.stopped_reason = "nothing_to_gather"
            return result

        conversation = Conversation()
        conversation.user_text(
            self._state_prompt(context, plan, pending, missing)
        )

        for iteration in range(1, self._max_iterations + 1):
            result.iterations = iteration
            try:
                with self._trace.span(
                    context.run_id,
                    "model:evidence_planner",
                    "MODEL",
                    iteration=iteration,
                    mode=getattr(self._model, "mode", "unknown"),
                ) as attributes:
                    response = self._model.converse(
                        conversation=conversation,
                        system=EVIDENCE_PLANNER,
                        tools=AGENT_TOOLS,
                    )
                    attributes["stop_reason"] = response.stop_reason
                    attributes["tool_use_count"] = len(response.tool_uses)
                    attributes["input_tokens"] = response.input_tokens
                    attributes["output_tokens"] = response.output_tokens
            except ModelError as exc:
                result.error = str(exc)
                result.stopped_reason = "model_error"
                return result

            result.input_tokens += response.input_tokens
            result.output_tokens += response.output_tokens

            if not response.wants_tools:
                result.stopped_reason = "model_done"
                return result

            # 도구 결과를 붙이기 전에 어시스턴트 턴을 확정한다.
            conversation.ensure_assistant(response)
            outcomes = self._run_tools(context, response.tool_uses, result)

            # 도구 결과를 반영해 남은 항목을 다시 계산한다.
            # 갱신하지 않으면 모델이 낡은 목록을 보고 같은 요청을 되풀이한다.
            pending = self._pending_narrative(plan, result)
            missing = self._missing_observations(plan, result)
            if not pending and not missing:
                conversation.tool_results(outcomes)
                result.stopped_reason = "gathered_all"
                return result

            conversation.tool_results(
                outcomes,
                trailing_text=self._state_prompt(context, plan, pending, missing),
            )

        result.stopped_reason = "max_iterations"
        return result

    # -- 내부 ------------------------------------------------------------

    def _run_tools(
        self, context: ToolContext, tool_uses, result: GatheringResult
    ) -> list[tuple[str, Any, bool]]:
        """모델이 요청한 도구를 Gateway 로 호출한다."""
        outcomes: list[tuple[str, Any, bool]] = []
        for use in tool_uses:
            if use.name not in ALLOWED_TOOL_NAMES:
                outcomes.append(
                    (
                        use.tool_use_id,
                        {"error": f"허용되지 않은 도구입니다: {use.name}"},
                        False,
                    )
                )
                continue
            try:
                payload = self._invoke(context, use, result)
                outcomes.append((use.tool_use_id, payload, True))
                result.tool_calls.append(use.name)
            except ToolPermissionDenied as exc:
                outcomes.append((use.tool_use_id, {"error": str(exc)}, False))
            except Exception as exc:  # noqa: BLE001 - 루프를 죽이지 않는다
                outcomes.append(
                    (use.tool_use_id, {"error": f"{type(exc).__name__}"}, False)
                )
        return outcomes

    def _invoke(
        self, context: ToolContext, use, result: GatheringResult
    ) -> dict[str, Any]:
        """도구 결과를 모델에게 돌려줄 형태로 정리하고 결과에 병합한다."""
        if use.name == "evidence_retrieval_tool":
            terms = tuple(str(item) for item in use.arguments.get("terms", []))
            top_k = int(use.arguments.get("top_k", 3) or 3)
            snippets: list[NarrativeSnippet] = self._gateway.invoke(
                "evidence_retrieval_tool", context, terms=terms, top_k=top_k
            )
            key = self._criterion_for_terms(terms)
            if key:
                result.attempted.add(key)
                if snippets:
                    result.narratives.setdefault(key, []).extend(snippets)
            else:
                # 어떤 기준을 위한 검색인지 되짚지 못하면 남은 항목 전체를
                # 시도한 것으로 본다. 같은 검색이 무한히 반복되지 않게 한다.
                result.attempted.update(self._term_index)
            return {
                "matches": [
                    {
                        "note_id": item.note_id,
                        "note_date": item.note_date,
                        "snippet": item.snippet,
                        "score": item.score,
                    }
                    for item in snippets
                ],
                "match_count": len(snippets),
            }

        fields = [str(item) for item in use.arguments.get("fields", [])]
        observations: dict[str, Observation] = self._gateway.invoke(
            "timeline_graph_tool", context, fields=fields
        )
        result.attempted_fields.update(fields)
        for name, observation in observations.items():
            existing = result.observations.get(name)
            if existing is None or existing.value is None:
                result.observations[name] = observation
        return {
            "observations": {
                name: {
                    "value": item.value,
                    "observed_at": item.observed_at,
                    "source_id": item.source_id,
                }
                for name, item in observations.items()
            }
        }

    def _criterion_for_terms(self, terms: tuple[str, ...]) -> str | None:
        """검색어를 요청한 기준을 되짚는다. 근거를 올바른 기준에 붙이기 위함."""
        for criterion_id, requested in self._term_index.items():
            if set(terms) & set(requested):
                return criterion_id
        return None

    def _state_prompt(
        self,
        context: ToolContext,
        plan: ExecutionPlan,
        pending: list[dict[str, Any]],
        missing: list[dict[str, Any]],
    ) -> str:
        """현재 수집 상태를 모델에게 전달한다."""
        for item in pending:
            self._term_index[item["criterion_id"]] = tuple(item["terms"])
        payload = {
            "task": "plan",
            "person_id": context.person_id,
            "trial_id": context.trial_id,
            "index_date": context.index_date,
            "pending_narrative": pending,
            "missing_observations": missing,
        }
        return (
            "다음은 현재 근거 수집 상태다. 추가로 필요한 근거가 있으면 도구를 "
            "호출하고, 없으면 done 만 반환하라.\n\n"
            + json.dumps(payload, ensure_ascii=False, indent=2)
        )

    @staticmethod
    def _pending_narrative(
        plan: ExecutionPlan, result: GatheringResult
    ) -> list[dict[str, Any]]:
        """자유서술 근거가 아직 없고 검색도 시도하지 않은 기준."""
        pending: list[dict[str, Any]] = []
        for item in plan.plans:
            if not item.needs_narrative:
                continue
            if result.narratives.get(item.criterion_id):
                continue
            if item.criterion_id in result.attempted:
                continue
            pending.append(
                {
                    "criterion_id": item.criterion_id,
                    "label": item.rule.label,
                    "field": item.rule.field_name,
                    "terms": list(item.narrative_terms),
                }
            )
        return pending

    @staticmethod
    def _missing_observations(
        plan: ExecutionPlan, result: GatheringResult
    ) -> list[dict[str, Any]]:
        """관찰값이 비어 있고 조회도 시도하지 않은 기준."""
        missing: list[dict[str, Any]] = []
        for item in plan.plans:
            if not item.needs_graph:
                continue
            field_name = item.rule.field_name
            found = result.observations.get(field_name)
            if found is not None and found.value is not None:
                continue
            if field_name in result.attempted_fields:
                continue
            missing.append(
                {
                    "criterion_id": item.criterion_id,
                    "label": item.rule.label,
                    "field": field_name,
                }
            )
        return missing
