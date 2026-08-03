"""Criterion Router: 조건 종류별 실행 계획을 세운다.

모든 기준을 같은 경로로 태우지 않는다. 수치 조건은 그래프 조회 + 규칙 계산만으로
끝나고, 파생 불리언은 자유서술 검색을 함께 요구한다. 이 분기를 여기서 결정한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..domain.criteria import requires_graph_lookup, requires_narrative_lookup, spec_for
from ..domain.models import CriterionRule
from ..domain.states import CriterionKind


@dataclass(frozen=True)
class CriterionPlan:
    """기준 한 건의 실행 계획."""

    rule: CriterionRule
    needs_graph: bool
    needs_narrative: bool
    narrative_terms: tuple[str, ...]
    graph_fields: tuple[str, ...]

    @property
    def criterion_id(self) -> str:
        return self.rule.criterion_id


@dataclass
class ExecutionPlan:
    """실행 전체 계획. 배치 조회를 위해 필드를 미리 모아둔다."""

    plans: list[CriterionPlan] = field(default_factory=list)

    @property
    def graph_fields(self) -> tuple[str, ...]:
        """한 번의 그래프 조회로 가져올 필드 집합."""
        collected: list[str] = []
        for plan in self.plans:
            for item in plan.graph_fields:
                if item not in collected:
                    collected.append(item)
        return tuple(collected)

    def narrative_requests(self) -> dict[str, tuple[str, ...]]:
        """criterion_id 별 자유서술 질의어."""
        return {
            plan.criterion_id: plan.narrative_terms
            for plan in self.plans
            if plan.needs_narrative and plan.narrative_terms
        }

    def kind_breakdown(self) -> dict[str, int]:
        """조건 유형 분포. Trace attribute 로 남긴다."""
        counts: dict[str, int] = {}
        for plan in self.plans:
            key = str(plan.rule.kind)
            counts[key] = counts.get(key, 0) + 1
        return counts


class CriterionRouter:
    """기준 목록을 실행 계획으로 변환한다."""

    def plan(self, rules: list[CriterionRule]) -> ExecutionPlan:
        plan = ExecutionPlan()
        for rule in rules:
            spec = spec_for(rule.field_name)
            needs_graph = requires_graph_lookup(spec)
            needs_narrative = requires_narrative_lookup(spec)
            plan.plans.append(
                CriterionPlan(
                    rule=rule,
                    needs_graph=needs_graph,
                    needs_narrative=needs_narrative,
                    narrative_terms=spec.narrative_terms,
                    graph_fields=(rule.field_name,) if needs_graph else (),
                )
            )
        return plan

    @staticmethod
    def is_deterministic_only(rule: CriterionRule) -> bool:
        """FM 개입 없이 규칙 계산만으로 확정할 수 있는 조건인지 판단한다."""
        return rule.kind in (
            CriterionKind.NUMERIC_POINT,
            CriterionKind.TEMPORAL_WINDOW,
            CriterionKind.CATEGORICAL,
        )
