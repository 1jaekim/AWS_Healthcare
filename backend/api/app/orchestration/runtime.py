"""AgentCore Runtime: Screening Orchestrator.

3계층에서 시작해 4 → 6 → 7 → 8 → 9계층을 순서대로 지나는 실행 본체.
모든 Tool 호출은 Gateway 를 경유하고, 각 단계는 Trace 스팬과 감사 이벤트를 남긴다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..actions.explanation import ExplanationAgent
from ..actions.next_best import NextBestEvidenceAgent
from ..actions.packet import EvidencePacketBuilder
from ..domain.models import CriterionResult, NarrativeSnippet, Observation, ScreeningRun
from ..domain.states import CriterionStatus, EligibilityStatus
from ..persistence.audit import AuditTrail
from ..persistence.run_store import RunStore
from ..reasoning.aggregator import AggregateOutcome, DeterministicAggregator
from ..reasoning.bundle import EvidenceBundleBuilder
from ..reasoning.verifier import LocalEvidenceVerifier
from ..safety.observability import TraceCollector
from ..tools.base import PermissionDenied, ToolContext
from ..tools.criteria_tool import CriteriaTool
from ..tools.timeline_graph import TimelineGraphTool
from .gateway import ToolGateway
from .router import CriterionRouter, ExecutionPlan


class PatientNotFound(LookupError):
    pass


class TrialNotFound(LookupError):
    pass


@dataclass
class ScreeningOutput:
    """API 계층으로 넘기는 실행 결과 묶음."""

    run: ScreeningRun
    outcome: AggregateOutcome
    packet: dict[str, Any]
    requests: list[dict[str, Any]]
    explanations: dict[str, Any]
    trace_summary: dict[str, Any]
    review_ticket_id: str | None = None


class ScreeningOrchestrator:
    """환자 x 시험 한 건을 근거 기반으로 판정한다."""

    def __init__(
        self,
        *,
        gateway: ToolGateway,
        criteria_tool: CriteriaTool,
        timeline_tool: TimelineGraphTool,
        router: CriterionRouter,
        bundler: EvidenceBundleBuilder,
        verifier: LocalEvidenceVerifier,
        aggregator: DeterministicAggregator,
        packet_builder: EvidencePacketBuilder,
        next_best: NextBestEvidenceAgent,
        explainer: ExplanationAgent,
        run_store: RunStore,
        audit: AuditTrail,
        trace: TraceCollector,
        gatherer: Any | None = None,
        mode: str = "deterministic",
    ) -> None:
        self._gatherer = gatherer
        self._mode = mode
        self._gateway = gateway
        self._criteria_tool = criteria_tool
        self._timeline_tool = timeline_tool
        self._router = router
        self._bundler = bundler
        self._verifier = verifier
        self._aggregator = aggregator
        self._packet_builder = packet_builder
        self._next_best = next_best
        self._explainer = explainer
        self._runs = run_store
        self._audit = audit
        self._trace = trace

    def run(
        self,
        *,
        person_id: int,
        trial_id: str,
        actor: str = "system",
    ) -> ScreeningOutput:
        """스크리닝 한 건을 실행한다."""
        index_row = self._timeline_tool.index_encounter(person_id)
        if index_row is None:
            raise PatientNotFound(f"환자 타임라인을 찾을 수 없습니다: {person_id}")

        run_id = self._runs.new_run_id()
        context = ToolContext(
            run_id=run_id,
            person_id=person_id,
            trial_id=trial_id,
            index_encounter_id=index_row["encounter_id"],
            index_date=index_row["encounter_date"],
        )

        self._audit.record(
            "RUN_STARTED",
            actor=actor,
            run_id=run_id,
            person_id=person_id,
            trial_id=trial_id,
            index_encounter_id=context.index_encounter_id,
        )

        with self._trace.span(run_id, "agent:screening", "AGENT", person_id=person_id):
            rules = self._gateway.invoke("criteria_tool", context, trial_id=trial_id)
            if not rules:
                raise TrialNotFound(f"시험 기준을 찾을 수 없습니다: {trial_id}")
            criteria_version = rules[0].criteria_version
            self._audit.record(
                "CRITERIA_LOADED",
                actor=actor,
                run_id=run_id,
                person_id=person_id,
                trial_id=trial_id,
                criteria_version=criteria_version,
                criteria_count=len(rules),
            )

            plan = self._router.plan(rules)
            observations = self._collect_observations(context, plan)
            narratives = self._collect_narratives(context, plan)
            gathering = self._gather_more(
                context, plan, narratives, observations
            )
            results = self._resolve_criteria(
                context, plan, observations, narratives, actor
            )

        outcome = self._aggregator.aggregate(results)
        run = ScreeningRun(
            run_id=run_id,
            person_id=person_id,
            trial_id=trial_id,
            criteria_version=criteria_version,
            index_encounter_id=context.index_encounter_id,
            index_date=context.index_date,
            started_at=datetime.now(UTC).isoformat(),
            finished_at=datetime.now(UTC).isoformat(),
            eligibility_status=str(outcome.eligibility_status),
            results=results,
            metadata={
                "mode": self._mode,
                "kind_breakdown": plan.kind_breakdown(),
                "tool_calls": [
                    record.tool_name for record in self._gateway.calls_for(run_id)
                ],
                "agent": gathering,
            },
        )

        packet = self._packet_builder.build(
            run_id=run_id,
            person_id=person_id,
            trial_id=trial_id,
            criteria_version=criteria_version,
            index_encounter_id=context.index_encounter_id,
            index_date=context.index_date,
            results=results,
            outcome=outcome,
        )
        requests = [
            item.to_dict() for item in self._next_best.propose(run_id, results)
        ]
        explanations = self._explainer.explain_both(
            outcome=outcome, results=results, run_id=run_id
        )

        self._runs.save_run(run)
        self._runs.save_artifacts(
            run_id,
            packet=packet.__dict__,
            requests=requests,
            explanations=explanations,
        )

        ticket_id = self._maybe_open_review(run, outcome, actor)

        self._audit.record(
            "RUN_COMPLETED",
            actor=actor,
            run_id=run_id,
            person_id=person_id,
            trial_id=trial_id,
            eligibility_status=str(outcome.eligibility_status),
            criteria_met=outcome.criteria_met,
            criteria_total=outcome.criteria_total,
            open_criteria=list(outcome.open_criteria),
            blocking_criteria=list(outcome.blocking_criteria),
        )

        return ScreeningOutput(
            run=run,
            outcome=outcome,
            packet=packet.__dict__,
            requests=requests,
            explanations=explanations,
            trace_summary=self._trace.summary_for(run_id),
            review_ticket_id=ticket_id,
        )

    def _gather_more(
        self,
        context: ToolContext,
        plan: ExecutionPlan,
        narratives: dict[str, list[NarrativeSnippet]],
        observations: dict[str, Observation],
    ) -> dict[str, Any]:
        """에이전트가 붙어 있으면 부족한 근거를 보강한다.

        보강 결과는 전달된 dict 에 제자리 반영하므로 이후 판정이 그대로 사용한다.
        """
        if self._gatherer is None:
            return {"enabled": False}

        gathered = self._gatherer.gather(
            context,
            plan,
            known_narratives=narratives,
            known_observations=observations,
        )
        for criterion_id, snippets in gathered.narratives.items():
            if snippets and not narratives.get(criterion_id):
                narratives[criterion_id] = snippets
        for field_name, observation in gathered.observations.items():
            existing = observations.get(field_name)
            if existing is None or existing.value is None:
                observations[field_name] = observation

        return {
            "enabled": True,
            "iterations": gathered.iterations,
            "stopped_reason": gathered.stopped_reason,
            "tool_calls": list(gathered.tool_calls),
            "input_tokens": gathered.input_tokens,
            "output_tokens": gathered.output_tokens,
            "error": gathered.error,
        }

    def _collect_observations(
        self, context: ToolContext, plan: ExecutionPlan
    ) -> dict[str, Observation]:
        """그래프 조회를 한 번에 묶어 처리한다."""
        fields = plan.graph_fields
        if not fields:
            return {}
        try:
            return self._gateway.invoke(
                "timeline_graph_tool", context, fields=list(fields)
            )
        except PermissionDenied as exc:
            self._audit.record(
                "TOOL_DENIED",
                actor="system",
                run_id=context.run_id,
                person_id=context.person_id,
                trial_id=context.trial_id,
                tool="timeline_graph_tool",
                reason=str(exc),
            )
            raise

    def _collect_narratives(
        self, context: ToolContext, plan: ExecutionPlan
    ) -> dict[str, list[NarrativeSnippet]]:
        """자유서술 검색이 필요한 기준만 조회한다."""
        result: dict[str, list[NarrativeSnippet]] = {}
        for criterion_id, terms in plan.narrative_requests().items():
            result[criterion_id] = self._gateway.invoke(
                "evidence_retrieval_tool", context, terms=terms, top_k=3
            )
        return result

    def _resolve_criteria(
        self,
        context: ToolContext,
        plan: ExecutionPlan,
        observations: dict[str, Observation],
        narratives: dict[str, list[NarrativeSnippet]],
        actor: str,
    ) -> list[CriterionResult]:
        """기준별로 규칙 계산 → 번들 → 검증 → 상태 확정을 수행한다."""
        results: list[CriterionResult] = []
        for item in plan.plans:
            observation = observations.get(item.rule.field_name)
            outcome = self._gateway.invoke(
                "rule_evaluator", context, rule=item.rule, observation=observation
            )
            bundle = self._bundler.build(
                item,
                observation=observation,
                narrative=narratives.get(item.criterion_id),
                outcome=outcome,
                tool_calls=["criteria_tool", "timeline_graph_tool", "rule_evaluator"],
            )
            # 검증 단계 자체는 에이전트 스팬으로 남긴다.
            # 모델 호출이 있으면 검증기 내부에서 MODEL 스팬이 따로 붙는다.
            with self._trace.span(
                context.run_id,
                "reason:verify",
                "AGENT",
                criterion_id=item.criterion_id,
            ) as attributes:
                verification = self._verifier.verify(bundle, run_id=context.run_id)
                attributes["proposed_status"] = str(verification.proposed_status)
                attributes["confidence"] = verification.confidence

            result = self._aggregator.finalize(bundle, verification)
            results.append(result)

            self._audit.record(
                "CRITERION_RESOLVED",
                actor=actor,
                run_id=context.run_id,
                person_id=context.person_id,
                trial_id=context.trial_id,
                criterion_id=result.criterion_id,
                status=str(result.status),
                observed_value=result.observed_value,
                expected_condition=result.expected_condition,
                confidence=result.confidence,
                source_ids=list(result.source_ids),
            )
        return results

    def _maybe_open_review(
        self, run: ScreeningRun, outcome: AggregateOutcome, actor: str
    ) -> str | None:
        """검토가 필요한 실행이면 큐 항목을 만든다."""
        needs_review = [
            item.criterion_id
            for item in run.results
            if item.status
            in (CriterionStatus.REVIEW_REQUIRED, CriterionStatus.CONFLICTING)
        ]
        if not needs_review:
            return None
        ticket = self._runs.open_ticket(
            run_id=run.run_id,
            person_id=run.person_id,
            trial_id=run.trial_id,
            criterion_ids=needs_review,
        )
        self._audit.record(
            "REVIEW_DECIDED",
            actor=actor,
            run_id=run.run_id,
            person_id=run.person_id,
            trial_id=run.trial_id,
            ticket_id=ticket.ticket_id,
            status="PENDING",
            criterion_ids=needs_review,
        )
        return ticket.ticket_id

    def run_batch(
        self, *, person_ids: list[int], trial_id: str, actor: str = "system"
    ) -> list[ScreeningOutput]:
        """코호트 집계를 위해 여러 환자를 순차 실행한다."""
        outputs: list[ScreeningOutput] = []
        for person_id in person_ids:
            try:
                outputs.append(
                    self.run(person_id=person_id, trial_id=trial_id, actor=actor)
                )
            except PatientNotFound:
                continue
        return outputs
