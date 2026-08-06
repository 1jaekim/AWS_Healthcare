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
from ..reasoning.judgment import JudgmentVerifier
from ..reasoning.rule_aggregator import A2A, CriterionDecision, RuleAggregator
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
    human_review_criteria: tuple[str, ...] = ()
    """사람이 직접 확인해야 하는 기준. 비어 있으면 자동 판정으로 충분하다."""


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
        deliberator: Any | None = None,
        judge: Any | None = None,
        judgment_verifier: JudgmentVerifier | None = None,
        rule_aggregator: RuleAggregator | None = None,
        patient_key_resolver: Any | None = None,
        mode: str = "deterministic",
    ) -> None:
        self._gatherer = gatherer
        self._deliberator = deliberator
        # 판단자가 붙으면 기준별로 LLM 판단 → Verifier → Rule Aggregator 를 돈다.
        # 확정 계층은 판단자 유무와 무관하게 결정론적이다.
        self._judge = judge
        self._judgment_verifier = judgment_verifier or JudgmentVerifier()
        self._rule_aggregator = rule_aggregator or RuleAggregator()
        self._patient_key_resolver = patient_key_resolver
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
        supplements: dict[str, Observation] | None = None,
        allow_profile_only: bool = False,
    ) -> ScreeningOutput:
        """스크리닝 한 건을 실행한다.

        `supplements` 는 참여자 답변에서 승격된 관찰값이다. 그래프에 값이 없는
        필드만 채운다. 기록으로 확인된 값을 덮어쓰지 않는다.
        """
        index_row = self._timeline_tool.index_encounter(person_id)
        if index_row is None:
            if not allow_profile_only or not supplements:
                raise PatientNotFound(f"환자 타임라인을 찾을 수 없습니다: {person_id}")
            index_row = {
                "encounter_id": f"APPLICATION-{person_id}",
                "encounter_date": datetime.now(UTC).date().isoformat(),
            }

        run_id = self._runs.new_run_id()
        context = ToolContext(
            run_id=run_id,
            person_id=person_id,
            trial_id=trial_id,
            index_encounter_id=index_row["encounter_id"],
            index_date=index_row["encounter_date"],
            patient_key=(
                self._patient_key_resolver(person_id)
                if self._patient_key_resolver is not None
                else None
            ),
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
            # Agent 모드에서는 EvidenceGatheringAgent가 RAG Tool 호출을 계획한다.
            # 결정론적 모드만 Runtime이 직접 조회한다.
            narratives = (
                {}
                if self._gatherer is not None
                else self._collect_narratives(context, plan)
            )
            applied = self._apply_supplements(
                context, plan, observations, supplements, actor
            )
            gathering = self._gather_more(
                context, plan, narratives, observations
            )
            decisions: list[CriterionDecision] = []
            results = self._resolve_criteria(
                context, plan, observations, narratives, actor, decisions
            )
            deliberation = self._deliberate_unknowns(context, results, actor)

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
                "deliberation": deliberation,
                "supplements": applied,
                "judgment": self._judgment_summary(decisions),
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
        self._record_guardrail_events(context, explanations, actor)

        self._runs.save_run(run)
        self._runs.save_artifacts(
            run_id,
            packet=packet.__dict__,
            requests=requests,
            explanations=explanations,
        )

        human_review = self._human_review_criteria(run, actor)

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
            human_review_criteria=human_review,
        )

    def _record_guardrail_events(
        self,
        context: ToolContext,
        explanations: dict[str, Any],
        actor: str,
    ) -> None:
        """생성 문장이 차단·완화되었으면 감사 로그에 남긴다.

        무엇을 왜 바꿨는지 남지 않으면 나중에 되짚을 수 없다. 규칙 ID와 심각도만
        기록하고 원문은 남기지 않는다. 차단된 문구를 감사 로그로 흘리지 않기 위함이다.
        """
        for audience, payload in explanations.items():
            if not isinstance(payload, dict):
                continue
            findings = payload.get("guardrail_findings") or []
            blocked = bool(payload.get("blocked"))
            if not findings and not blocked:
                continue
            self._audit.record(
                "GUARDRAIL_TRIGGERED",
                actor=actor,
                run_id=context.run_id,
                person_id=context.person_id,
                trial_id=context.trial_id,
                audience=audience,
                blocked=blocked,
                rule_ids=sorted(
                    {
                        str(item.get("rule_id"))
                        for item in findings
                        if item.get("rule_id")
                    }
                ),
                severities=sorted(
                    {
                        str(item.get("severity"))
                        for item in findings
                        if item.get("severity")
                    }
                ),
                finding_count=len(findings),
            )

    def _apply_supplements(
        self,
        context: ToolContext,
        plan: ExecutionPlan,
        observations: dict[str, Observation],
        supplements: dict[str, Observation] | None,
        actor: str,
    ) -> dict[str, Any]:
        """참여자 답변에서 온 관찰값으로 빈 필드를 채운다.

        이미 값이 있는 필드는 건드리지 않는다. 계획에 없는 필드도 무시한다.
        어떤 필드가 채워졌는지 메타데이터와 감사 로그에 남긴다.
        """
        if not supplements:
            return {"applied": [], "ignored": []}

        planned = {item.rule.field_name for item in plan.plans}
        applied: list[str] = []
        ignored: list[dict[str, str]] = []

        for field_name, observation in supplements.items():
            if field_name not in planned:
                ignored.append(
                    {"field": field_name, "reason": "이 시험 기준에 없는 필드입니다."}
                )
                continue
            existing = observations.get(field_name)
            if existing is not None and existing.value is not None:
                ignored.append(
                    {"field": field_name, "reason": "기록으로 확인된 값이 이미 있습니다."}
                )
                continue
            observations[field_name] = observation
            applied.append(field_name)

        if applied:
            self._audit.record(
                "SUPPLEMENT_APPLIED",
                actor=actor,
                run_id=context.run_id,
                person_id=context.person_id,
                trial_id=context.trial_id,
                fields=sorted(applied),
                source_ids=[
                    supplements[name].source_id for name in sorted(applied)
                ],
            )

        return {"applied": sorted(applied), "ignored": ignored}

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

        # 모델이 실패하거나 도구 호출 없이 끝난 기준만 결정론적으로 보강한다.
        fallback_queries: list[str] = []
        for criterion_id, terms in plan.narrative_requests().items():
            if narratives.get(criterion_id) or criterion_id in gathered.attempted:
                continue
            narratives[criterion_id] = self._gateway.invoke(
                "evidence_retrieval_tool", context, terms=terms, top_k=3
            )
            fallback_queries.append(criterion_id)

        return {
            "enabled": True,
            "iterations": gathered.iterations,
            "stopped_reason": gathered.stopped_reason,
            "tool_calls": list(gathered.tool_calls),
            "input_tokens": gathered.input_tokens,
            "output_tokens": gathered.output_tokens,
            "error": gathered.error,
            "fallback_queries": fallback_queries,
        }

    def _deliberate_unknowns(
        self,
        context: ToolContext,
        results: list[CriterionResult],
        actor: str,
    ) -> dict[str, Any]:
        """미해소 기준을 유한 교차 검토하고 추천만 기록한다."""
        if self._deliberator is None:
            return {
                "enabled": False,
                "rounds": 0,
                "max_rounds": 2,
                "stopped_reason": "model_disabled",
                "items": [],
            }

        deliberation = self._deliberator.deliberate(
            results,
            run_id=context.run_id,
        )
        payload = deliberation.to_dict()
        if payload["rounds"]:
            self._audit.record(
                "UNKNOWN_DELIBERATION_COMPLETED",
                actor=actor,
                run_id=context.run_id,
                person_id=context.person_id,
                trial_id=context.trial_id,
                rounds=payload["rounds"],
                stopped_reason=payload["stopped_reason"],
                recommendations={
                    item["criterion_id"]: item["recommendation"]
                    for item in payload["items"]
                },
            )
        return payload

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
        decisions: list[CriterionDecision] | None = None,
    ) -> list[CriterionResult]:
        """기준별로 규칙 계산 → 번들 → 검증 → 상태 확정을 수행한다."""
        prepared: list[tuple[Any, Any]] = []
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

            prepared.append((bundle, verification))

        judgments: list[Any | None] = [None] * len(prepared)
        skipped_model: list[str] = []
        if self._judge is not None:
            # 규칙이 이미 확정한 기준은 모델에 보내지 않는다.
            #
            # `RuleAggregator` 는 어차피 모델이 규칙보다 강한 결론을 내도 올려주지
            # 않는다. 즉 `age >= 19` 에 그래프 값 41 이 있으면 답은 이미 나와 있고,
            # 모델 호출은 결론에 기여하지 못한다. 오히려 어긋나면 CONFLICTING 으로
            # A2A 를 발동시켜 모델을 더 부른다.
            #
            # 그래서 규칙 계산이 성립한 결정론적 기준은 `from_rule()` 로 판단을
            # 승계한다. 검증·확정 경로는 그대로 지나가므로 판정은 바뀌지 않고
            # 모델 왕복만 사라진다.
            model_items: list[tuple[Any, Any]] = []
            model_indexes: list[int] = []
            for index, (bundle, verification) in enumerate(prepared):
                if self._is_rule_settled(bundle):
                    judgments[index] = self._judge.from_rule(
                        bundle,
                        verification,
                        reason_suffix="규칙 계산으로 확정해 모델 판단을 생략했습니다.",
                    )
                    skipped_model.append(bundle.rule.criterion_id)
                    continue
                model_items.append((bundle, verification))
                model_indexes.append(index)

            if model_items and hasattr(self._judge, "judge_many"):
                batch = list(
                    self._judge.judge_many(
                        model_items,
                        run_id=context.run_id,
                        patient_key=context.patient_key,
                        index_date=context.index_date,
                    )
                )
                for offset, judgment in zip(model_indexes, batch):
                    judgments[offset] = judgment

        if skipped_model:
            self._trace_note(context.run_id, skipped_model)

        results: list[CriterionResult] = []
        for index, (bundle, verification) in enumerate(prepared):
            judgment = judgments[index] if index < len(judgments) else None
            decision = self._judge_criterion(
                context, bundle, verification, actor, judgment=judgment
            )
            if decision is not None:
                verification = decision.verification
                if decisions is not None:
                    decisions.append(decision)

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

    @staticmethod
    def _is_rule_settled(bundle: Any) -> bool:
        """규칙 계산만으로 결론이 나는 기준인지 판단한다.

        두 조건을 모두 만족해야 한다.

          1. 조건 종류가 결정론적이다 — 수치·시간창·범주형. 자유서술 해석이
             필요한 DERIVED_BOOLEAN·NARRATIVE 는 규칙이 답을 못 낸다.
          2. 규칙이 실제로 계산에 성공했다 — `outcome.satisfied` 가 True/False.
             관찰값이 없으면 None 이고, 그때는 근거를 찾아야 하므로 모델이 필요하다.

        1번만 보고 넘기면 값이 없는 수치 기준까지 모델을 건너뛰어, 자유서술에서
        값을 찾을 기회를 잃는다. 그래서 2번이 반드시 함께 있어야 한다.
        """
        if not CriterionRouter.is_deterministic_only(bundle.rule):
            return False
        outcome = getattr(bundle, "outcome", None)
        return outcome is not None and outcome.satisfied is not None

    def _trace_note(self, run_id: str, skipped: list[str]) -> None:
        """모델 판단을 생략한 기준을 스팬으로 남긴다. 비용·지연 분석의 근거다."""
        with self._trace.span(
            run_id,
            "reason:judge_skipped",
            "AGENT",
            criterion_ids=sorted(skipped),
            count=len(skipped),
        ):
            pass

    @staticmethod
    def _judgment_summary(
        decisions: list[CriterionDecision],
    ) -> dict[str, Any]:
        """LLM 판단 단계의 실행 요약. 실행 메타데이터와 응답에 싣는다."""
        if not decisions:
            return {"enabled": False, "items": []}
        return {
            "enabled": True,
            "mode": (
                "model"
                if any(item.judgment.model_backed for item in decisions)
                else "rule"
            ),
            "criteria_total": len(decisions),
            "decisions": {
                "OK": sum(1 for item in decisions if item.status == "OK"),
                "NOT_OK": sum(1 for item in decisions if item.status == "NOT_OK"),
                "UNKNOWN": sum(
                    1 for item in decisions if item.status == "UNKNOWN"
                ),
            },
            "routes": {
                route: sum(1 for item in decisions if item.route == route)
                for route in sorted({item.route for item in decisions})
            },
            "a2a_criteria": [
                item.criterion_id for item in decisions if item.route == A2A
            ],
            "failed_checks": sorted(
                {
                    check.check_id
                    for item in decisions
                    for check in item.report.failures
                }
            ),
            "input_tokens": sum(
                item.judgment.input_tokens for item in decisions
            ),
            "output_tokens": sum(
                item.judgment.output_tokens for item in decisions
            ),
            "items": [item.to_dict() for item in decisions],
        }

    def _judge_criterion(
        self,
        context: ToolContext,
        bundle: Any,
        rule_verification: Any,
        actor: str,
        judgment: Any | None = None,
    ) -> CriterionDecision | None:
        """LLM 판단 → Verifier → Rule Aggregator 단계를 실행한다.

        판단자가 없으면 None 을 돌려주고 호출부는 규칙 판정을 그대로 쓴다.
        판단자가 있어도 상태 확정은 Rule Aggregator 가 하므로, 모델이 규칙보다
        강한 결론을 내려도 판정이 올라가지 않는다.
        """
        if self._judge is None:
            return None

        criterion_id = bundle.rule.criterion_id
        with self._trace.span(
            context.run_id,
            "reason:judge",
            "AGENT",
            criterion_id=criterion_id,
        ) as attributes:
            if judgment is None:
                judgment = self._judge.judge(
                    bundle,
                    rule_verification=rule_verification,
                    run_id=context.run_id,
                    patient_key=context.patient_key,
                    index_date=context.index_date,
                )
            report = self._judgment_verifier.verify(
                bundle,
                judgment,
                rule_verification=rule_verification,
                index_date=context.index_date,
            )
            decision = self._rule_aggregator.confirm(
                bundle,
                judgment=judgment,
                report=report,
                rule_verification=rule_verification,
            )
            attributes["proposed_status"] = judgment.proposed_status
            attributes["judgment_origin"] = judgment.origin
            attributes["failed_checks"] = [
                check.check_id for check in report.failures
            ]
            attributes["status"] = decision.status
            attributes["route"] = decision.route

        self._audit.record(
            "CRITERION_JUDGED",
            actor=actor,
            run_id=context.run_id,
            person_id=context.person_id,
            trial_id=context.trial_id,
            criterion_id=criterion_id,
            proposed_status=judgment.proposed_status,
            judgment_origin=judgment.origin,
            rule_status=str(rule_verification.proposed_status),
            status=decision.status,
            route=decision.route,
            used_evidence_ids=list(report.cited_evidence_ids),
            fabricated_evidence_ids=list(report.fabricated_evidence_ids),
            failed_checks=[check.check_id for check in report.failures],
        )
        return decision

    def _human_review_criteria(
        self, run: ScreeningRun, actor: str
    ) -> tuple[str, ...]:
        """사람이 직접 확인해야 하는 기준을 고른다.

        예전에는 여기서 검토 큐 티켓을 만들었다. 그 큐는 Lambda 메모리에 있어
        콜드 스타트마다 비워지고, 읽는 화면도 없었다. 쌓이지 않고 아무도 보지
        않는 큐는 안전장치가 아니라 안전장치처럼 보이는 코드다.

        대신 어떤 기준이 왜 사람 손을 필요로 하는지 판정 결과에 담아 돌려준다.
        티켓 ID 하나보다 정보량이 많고, 검토 화면이 필요해지면 판정을 조회해
        만들면 된다. 영속 저장이 필요해지면 그때 DynamoDB 로 만드는 것이 맞다.

        두 종류를 합친다.

          1. 규칙 계층이 검토로 표시한 기준 (REVIEW_REQUIRED · CONFLICTING)
          2. A2A 교차 검토가 합의에 이르지 못한 미해소 기준

        2번이 A2A 를 살려두는 이유다. 근거가 없어서 판정불가인 것과 두 검토자의
        판단이 갈려서 판정불가인 것은 다른 상황이고, 후자는 질문으로 풀리지 않는다.
        """
        needs_review = {
            item.criterion_id
            for item in run.results
            if item.status
            in (CriterionStatus.REVIEW_REQUIRED, CriterionStatus.CONFLICTING)
        }
        deliberation = run.metadata.get("deliberation", {})
        if deliberation.get("enabled") and deliberation.get("rounds", 0) > 0:
            recommendations = {
                str(item.get("criterion_id")): item.get("recommendation")
                for item in deliberation.get("items", [])
                if isinstance(item, dict) and item.get("criterion_id")
            }
            needs_review.update(
                item.criterion_id
                for item in run.results
                if item.status
                in (
                    CriterionStatus.UNKNOWN,
                    CriterionStatus.CONFLICTING,
                    CriterionStatus.REVIEW_REQUIRED,
                )
                and recommendations.get(item.criterion_id) not in {"OK", "NOT_OK"}
            )
        if not needs_review:
            return ()

        criterion_ids = tuple(sorted(needs_review))
        self._audit.record(
            "HUMAN_REVIEW_FLAGGED",
            actor=actor,
            run_id=run.run_id,
            person_id=run.person_id,
            trial_id=run.trial_id,
            criterion_ids=list(criterion_ids),
        )
        return criterion_ids

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
