"""Evidence Bundle 조립.

Tool 들이 만든 부분 근거를 기준 단위로 묶는다. 이 시점에서 출처 ID가 빠진 근거는
버리지 않고 그대로 실어 보내되, Verifier 가 grounding 실패로 판정할 수 있게 한다.
"""

from __future__ import annotations

from ..domain.models import (
    EvidenceBundle,
    NarrativeSnippet,
    Observation,
    RuleOutcome,
)
from ..orchestration.router import CriterionPlan


class EvidenceBundleBuilder:
    """조건 · 관찰값 · 원문 · 계산 결과를 하나로 합친다."""

    def build(
        self,
        plan: CriterionPlan,
        *,
        observation: Observation | None,
        narrative: list[NarrativeSnippet] | None,
        outcome: RuleOutcome | None,
        tool_calls: list[str],
    ) -> EvidenceBundle:
        bundle = EvidenceBundle(rule=plan.rule, tool_calls=list(tool_calls))
        if observation is not None:
            bundle.observations.append(observation)
        if narrative:
            bundle.narrative.extend(narrative)
        bundle.outcome = outcome
        return bundle
