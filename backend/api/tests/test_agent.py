"""에이전트 계층 검증.

실제 Bedrock 호출 없이 검증한다. 모델 응답은 가짜 클라이언트로 주입하고,
Converse API 요청 형식은 기록된 호출 인자로 확인한다.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from agent.model import (
    BedrockModelClient,
    Conversation,
    ModelError,
    ModelResponse,
    StubModelClient,
    ToolUse,
)
from agent.toolspec import ALLOWED_TOOL_NAMES, AGENT_TOOLS
from app.config import ModelSettings
from app.container import build_container
from app.domain.models import (
    CriterionRule,
    EvidenceBundle,
    NarrativeSnippet,
    Observation,
    RuleOutcome,
)
from app.domain.states import CriterionKind
from app.reasoning.verifier import LocalEvidenceVerifier
from app.safety.observability import TraceCollector


# ---------------------------------------------------------------------------
# 테스트용 모델 클라이언트
# ---------------------------------------------------------------------------


class ScriptedModel:
    """정해진 응답을 순서대로 돌려주는 모델."""

    mode = "scripted"

    def __init__(self, responses: list[ModelResponse]) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def converse(self, *, conversation, system, tools=None):
        self.requests.append(
            {
                "system": system,
                "tools": tools,
                "messages": [dict(m) for m in conversation.messages],
            }
        )
        if not self._responses:
            return ModelResponse(text="{}")
        return self._responses.pop(0)


class FailingModel:
    mode = "failing"

    def converse(self, *, conversation, system, tools=None):
        raise ModelError("의도적 실패")


def _bundle(
    *,
    satisfied: bool | None,
    value: float | bool | None = 8.2,
    narrative: list[NarrativeSnippet] | None = None,
) -> EvidenceBundle:
    rule = CriterionRule(
        criterion_id="C01",
        criterion_type="INCLUSION",
        field_name="hba1c",
        operator="between",
        value_low="7.5",
        value_high="10.5",
        unit="%",
        label="HbA1c",
        kind=CriterionKind.NUMERIC_POINT,
        trial_id="T",
        criteria_version="v1-test",
    )
    bundle = EvidenceBundle(rule=rule)
    if value is not None:
        bundle.observations.append(
            Observation(
                field_name="hba1c",
                value=value,
                unit="%",
                observed_at="2020-01-01",
                source="TIMELINE_GRAPH",
                source_id="ENC-1",
            )
        )
    if narrative:
        bundle.narrative.extend(narrative)
    bundle.outcome = RuleOutcome(
        criterion_id="C01",
        satisfied=satisfied,
        observed_repr=str(value) if value is not None else None,
        expected_repr="7.5-10.5",
        explanation="규칙 계산",
    )
    return bundle


def _nli(entailment: str, confidence: float, conflicts=None) -> ModelResponse:
    return ModelResponse(
        text=json.dumps(
            {
                "entailment": entailment,
                "confidence": confidence,
                "rationale": "테스트 근거",
                "conflicts": conflicts or [],
            },
            ensure_ascii=False,
        )
    )


# ---------------------------------------------------------------------------
# 모델 응답 파싱
# ---------------------------------------------------------------------------


def test_model_response_extracts_json_from_noisy_text() -> None:
    response = ModelResponse(
        text='설명입니다.\n```json\n{"entailment": "SUPPORTED"}\n```\n끝.'
    )
    assert response.json_payload() == {"entailment": "SUPPORTED"}


def test_model_response_returns_none_for_non_json() -> None:
    assert ModelResponse(text="JSON이 없습니다").json_payload() is None


def test_bedrock_client_builds_converse_request() -> None:
    """Converse API 요청 형식과 응답 파싱을 확인한다."""

    class FakeBedrock:
        def __init__(self) -> None:
            self.kwargs: dict[str, Any] = {}

        def converse(self, **kwargs):
            self.kwargs = kwargs
            return {
                "output": {
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"text": "확인했습니다"},
                            {
                                "toolUse": {
                                    "toolUseId": "tu-1",
                                    "name": "evidence_retrieval_tool",
                                    "input": {"terms": ["임신 중"]},
                                }
                            },
                        ],
                    }
                },
                "stopReason": "tool_use",
                "usage": {"inputTokens": 120, "outputTokens": 45},
            }

    fake = FakeBedrock()
    client = BedrockModelClient(
        model_id="test-model",
        region="us-east-1",
        max_tokens=512,
        guardrail_id="gr-1",
        guardrail_version="1",
        client=fake,
    )
    conversation = Conversation()
    conversation.user_text("안녕")
    response = client.converse(
        conversation=conversation, system="시스템", tools=AGENT_TOOLS
    )

    assert fake.kwargs["modelId"] == "test-model"
    assert fake.kwargs["system"] == [{"text": "시스템"}]
    assert fake.kwargs["inferenceConfig"] == {"maxTokens": 512, "temperature": 0.0}
    assert fake.kwargs["toolConfig"]["tools"] == AGENT_TOOLS
    assert fake.kwargs["guardrailConfig"] == {
        "guardrailIdentifier": "gr-1",
        "guardrailVersion": "1",
    }

    assert response.text == "확인했습니다"
    assert response.stop_reason == "tool_use"
    assert response.input_tokens == 120
    assert response.output_tokens == 45
    assert len(response.tool_uses) == 1
    assert response.tool_uses[0].name == "evidence_retrieval_tool"
    # 어시스턴트 턴이 대화에 누적되어야 다음 턴에서 tool_result 를 붙일 수 있다.
    assert conversation.messages[-1]["role"] == "assistant"


def test_bedrock_client_wraps_failure_in_model_error() -> None:
    class Boom:
        def converse(self, **kwargs):
            raise RuntimeError("network down")

    client = BedrockModelClient(
        model_id="m", region="r", client=Boom()
    )
    conversation = Conversation()
    conversation.user_text("x")
    with pytest.raises(ModelError):
        client.converse(conversation=conversation, system="s")


def test_conversation_tool_results_shape() -> None:
    conversation = Conversation()
    conversation.tool_results([("tu-1", {"match_count": 0}, True)])
    block = conversation.messages[-1]["content"][0]["toolResult"]
    assert block["toolUseId"] == "tu-1"
    assert block["status"] == "success"
    assert block["content"] == [{"json": {"match_count": 0}}]


# ---------------------------------------------------------------------------
# 도구 노출 범위
# ---------------------------------------------------------------------------


def test_model_cannot_see_judgement_or_write_tools() -> None:
    """모델에게 판정·저장 도구를 노출하지 않는다."""
    assert ALLOWED_TOOL_NAMES == {
        "evidence_retrieval_tool",
        "timeline_graph_tool",
    }
    assert "rule_evaluator" not in ALLOWED_TOOL_NAMES
    assert "criteria_tool" not in ALLOWED_TOOL_NAMES


def test_agent_loop_rejects_tool_outside_whitelist() -> None:
    """모델이 허용 목록 밖 도구를 부르면 실행하지 않고 오류를 돌려준다."""
    from agent.loop import EvidenceGatheringAgent
    from app.orchestration.router import CriterionRouter
    from app.tools.base import ToolContext

    class SpyGateway:
        def __init__(self) -> None:
            self.invoked: list[str] = []

        def invoke(self, name, context, /, **kwargs):
            self.invoked.append(name)
            return {}

    rule = CriterionRule(
        criterion_id="C09",
        criterion_type="EXCLUSION",
        field_name="active_pregnancy",
        operator="=",
        value_low="false",
        value_high=None,
        unit="boolean",
        label="임신 여부",
        kind=CriterionKind.DERIVED_BOOLEAN,
        trial_id="T",
        criteria_version="v1",
    )
    plan = CriterionRouter().plan([rule])

    model = ScriptedModel(
        [
            ModelResponse(
                text="",
                tool_uses=(
                    ToolUse("tu-bad", "rule_evaluator", {"rule": "x"}),
                ),
                stop_reason="tool_use",
            ),
            ModelResponse(text=json.dumps({"done": True})),
        ]
    )
    gateway = SpyGateway()
    agent = EvidenceGatheringAgent(
        model=model, gateway=gateway, trace=TraceCollector(), max_iterations=3
    )
    result = agent.gather(
        ToolContext("RUN-1", 1, "T", "ENC-1", "2020-01-01"),
        plan,
        known_narratives={},
        known_observations={},
    )

    assert gateway.invoked == [], "허용 목록 밖 도구가 실행되었습니다."
    assert result.stopped_reason == "model_done"


def test_agent_loop_respects_iteration_cap() -> None:
    """모델이 계속 도구를 요청해도 상한에서 멈춘다."""
    from agent.loop import EvidenceGatheringAgent
    from app.orchestration.router import CriterionRouter
    from app.tools.base import ToolContext

    class AlwaysGateway:
        def invoke(self, name, context, /, **kwargs):
            return []

    rule = CriterionRule(
        criterion_id="C09",
        criterion_type="EXCLUSION",
        field_name="active_pregnancy",
        operator="=",
        value_low="false",
        value_high=None,
        unit="boolean",
        label="임신 여부",
        kind=CriterionKind.DERIVED_BOOLEAN,
        trial_id="T",
        criteria_version="v1",
    )
    plan = CriterionRouter().plan([rule])

    greedy = ModelResponse(
        text="",
        tool_uses=(
            ToolUse("tu", "evidence_retrieval_tool", {"terms": ["임신 중"]}),
        ),
        stop_reason="tool_use",
    )
    model = ScriptedModel([greedy] * 10)
    agent = EvidenceGatheringAgent(
        model=model, gateway=AlwaysGateway(), trace=TraceCollector(), max_iterations=3
    )
    result = agent.gather(
        ToolContext("RUN-2", 1, "T", "ENC-1", "2020-01-01"),
        plan,
        known_narratives={},
        known_observations={},
    )

    assert result.iterations == 3
    assert result.stopped_reason == "max_iterations"


def test_agent_loop_skips_when_nothing_missing() -> None:
    """수집할 근거가 없으면 모델을 호출하지 않는다."""
    from agent.loop import EvidenceGatheringAgent
    from app.orchestration.router import CriterionRouter
    from app.tools.base import ToolContext

    rule = CriterionRule(
        criterion_id="C01",
        criterion_type="INCLUSION",
        field_name="hba1c",
        operator=">=",
        value_low="7.5",
        value_high=None,
        unit="%",
        label="HbA1c",
        kind=CriterionKind.NUMERIC_POINT,
        trial_id="T",
        criteria_version="v1",
    )
    plan = CriterionRouter().plan([rule])
    model = ScriptedModel([])
    agent = EvidenceGatheringAgent(
        model=model, gateway=None, trace=TraceCollector()
    )
    result = agent.gather(
        ToolContext("RUN-3", 1, "T", "ENC-1", "2020-01-01"),
        plan,
        known_narratives={},
        known_observations={
            "hba1c": Observation(
                field_name="hba1c",
                value=8.0,
                unit="%",
                observed_at="2020-01-01",
                source="TIMELINE_GRAPH",
                source_id="ENC-1",
            )
        },
    )

    assert result.stopped_reason == "nothing_to_gather"
    assert model.requests == []


# ---------------------------------------------------------------------------
# FM NLI Verifier
# ---------------------------------------------------------------------------


def _verifier(model):
    from agent.verifier import ModelEvidenceVerifier
    from app.safety.guardrails import LocalGuardrail

    return ModelEvidenceVerifier(
        model=model,
        fallback=LocalEvidenceVerifier(LocalGuardrail(attach_disclaimer=False)),
        trace=TraceCollector(),
    )


def test_model_verifier_accepts_agreeing_verdict() -> None:
    verifier = _verifier(ScriptedModel([_nli("SUPPORTED", 0.9)]))
    verification = verifier.verify(_bundle(satisfied=True), run_id="RUN-1")
    assert verification.proposed_status == "EVIDENCE_FOUND"
    assert verification.confidence == 0.9


def test_model_verifier_escalates_on_divergence() -> None:
    """모델이 규칙과 반대 결론을 내면 사람 검토로 올린다."""
    verifier = _verifier(ScriptedModel([_nli("CONTRADICTED", 0.95)]))
    verification = verifier.verify(_bundle(satisfied=True), run_id="RUN-1")
    assert verification.proposed_status == "REVIEW_REQUIRED"
    assert any("달라" in note for note in verification.notes)


def test_model_verifier_downgrades_low_confidence() -> None:
    verifier = _verifier(ScriptedModel([_nli("SUPPORTED", 0.3)]))
    verification = verifier.verify(_bundle(satisfied=True), run_id="RUN-1")
    assert verification.proposed_status == "REVIEW_REQUIRED"


def test_model_verifier_falls_back_on_model_error() -> None:
    verifier = _verifier(FailingModel())
    verification = verifier.verify(_bundle(satisfied=True), run_id="RUN-1")
    assert verification.proposed_status == "EVIDENCE_FOUND"
    assert any("모델 검증 실패" in note for note in verification.notes)


def test_model_verifier_falls_back_on_malformed_response() -> None:
    verifier = _verifier(ScriptedModel([ModelResponse(text="형식이 아님")]))
    verification = verifier.verify(_bundle(satisfied=False), run_id="RUN-1")
    assert verification.proposed_status == "CONTRADICTED"
    assert any("형식" in note for note in verification.notes)


def test_model_verifier_falls_back_on_unknown_entailment() -> None:
    verifier = _verifier(
        ScriptedModel([ModelResponse(text=json.dumps({"entailment": "MAYBE"}))])
    )
    verification = verifier.verify(_bundle(satisfied=True), run_id="RUN-1")
    assert verification.proposed_status == "EVIDENCE_FOUND"
    assert any("알 수 없는" in note for note in verification.notes)


def test_model_verifier_skips_model_without_any_evidence() -> None:
    """근거가 아예 없으면 모델을 부르지 않는다."""
    model = ScriptedModel([_nli("SUPPORTED", 0.9)])
    verifier = _verifier(model)
    verification = verifier.verify(
        _bundle(satisfied=None, value=None), run_id="RUN-1"
    )
    assert verification.proposed_status == "UNKNOWN"
    assert model.requests == []


def test_model_verifier_sends_source_ids_in_payload() -> None:
    """모델 입력에 출처 ID가 포함되어야 grounding 을 요구할 수 있다."""
    model = ScriptedModel([_nli("SUPPORTED", 0.9)])
    _verifier(model).verify(_bundle(satisfied=True), run_id="RUN-1")
    sent = model.requests[0]["messages"][0]["content"][0]["text"]
    assert "ENC-1" in sent
    assert '"task": "nli"' in sent


# ---------------------------------------------------------------------------
# 설명 · 질문 생성
# ---------------------------------------------------------------------------


def _outcome_and_results():
    from app.domain.models import CriterionResult
    from app.domain.states import CriterionStatus
    from app.reasoning.aggregator import DeterministicAggregator

    results = [
        CriterionResult(
            criterion_id="C01",
            criterion_type="INCLUSION",
            label="HbA1c",
            field_name="hba1c",
            kind=CriterionKind.NUMERIC_POINT,
            status=CriterionStatus.EVIDENCE_FOUND,
            observed_value="8.2",
            expected_condition="7.5-10.5",
            unit="%",
            observed_at="2020-01-01",
            confidence=0.9,
            explanation="충족",
            source_ids=("ENC-1",),
        )
    ]
    return DeterministicAggregator().aggregate(results), results


def _explainer(model):
    from app.actions.explanation import ExplanationAgent
    from agent.narration import ModelExplanationAgent
    from app.safety.guardrails import LocalGuardrail

    guardrail = LocalGuardrail()
    return ModelExplanationAgent(
        model=model,
        guardrail=guardrail,
        fallback=ExplanationAgent(guardrail),
        trace=TraceCollector(),
    )


def test_model_explanation_passes_through_guardrail() -> None:
    """모델이 확정 표현을 써도 Guardrails 가 완화한다."""
    outcome, results = _outcome_and_results()
    model = ScriptedModel(
        [
            ModelResponse(
                text=json.dumps(
                    {
                        "summary": "제2형 당뇨로 확진되었습니다.",
                        "highlights": ["참여가 확정되었습니다."],
                    },
                    ensure_ascii=False,
                )
            )
        ]
    )
    explanation = _explainer(model).explain(
        outcome=outcome, results=results, audience="patient", run_id="RUN-1"
    )
    assert "확진" not in explanation.summary
    assert all("참여가 확정" not in line for line in explanation.highlights)
    assert explanation.guardrail_findings


def test_model_explanation_falls_back_on_empty_summary() -> None:
    outcome, results = _outcome_and_results()
    model = ScriptedModel([ModelResponse(text=json.dumps({"summary": ""}))])
    explanation = _explainer(model).explain(
        outcome=outcome, results=results, audience="admin", run_id="RUN-1"
    )
    assert explanation.summary
    assert not explanation.blocked


def test_model_explanation_skips_model_without_sources() -> None:
    """출처가 없으면 모델을 부르지 않고 폴백한다."""
    from app.domain.models import CriterionResult
    from app.domain.states import CriterionStatus
    from app.reasoning.aggregator import DeterministicAggregator

    results = [
        CriterionResult(
            criterion_id="C01",
            criterion_type="INCLUSION",
            label="HbA1c",
            field_name="hba1c",
            kind=CriterionKind.NUMERIC_POINT,
            status=CriterionStatus.UNKNOWN,
            observed_value=None,
            expected_condition="7.5-10.5",
            unit="%",
            observed_at=None,
            confidence=0.0,
            explanation="근거 없음",
            source_ids=(),
        )
    ]
    outcome = DeterministicAggregator().aggregate(results)
    model = ScriptedModel([ModelResponse(text=json.dumps({"summary": "무근거"}))])
    explanation = _explainer(model).explain(
        outcome=outcome, results=results, audience="admin", run_id="RUN-1"
    )
    assert model.requests == []
    assert explanation.blocked


def test_model_question_writer_keeps_deterministic_priority() -> None:
    """질문 문장만 모델이 쓰고, 우선순위는 규칙이 정한다."""
    from app.actions.next_best import NextBestEvidenceAgent
    from agent.narration import ModelNextBestEvidenceAgent
    from app.domain.models import CriterionResult
    from app.domain.states import CriterionStatus
    from app.safety.guardrails import LocalGuardrail

    def unknown(cid: str, label: str, ctype: str) -> CriterionResult:
        return CriterionResult(
            criterion_id=cid,
            criterion_type=ctype,
            label=label,
            field_name="hba1c",
            kind=CriterionKind.NUMERIC_POINT,
            status=CriterionStatus.UNKNOWN,
            observed_value=None,
            expected_condition="7.5-10.5",
            unit="%",
            observed_at=None,
            confidence=0.0,
            explanation="",
            source_ids=(),
        )

    results = [
        unknown("C01", "HbA1c", "INCLUSION"),
        unknown("C02", "eGFR", "EXCLUSION"),
    ]
    guardrail = LocalGuardrail(attach_disclaimer=False)
    fallback = NextBestEvidenceAgent(guardrail)
    baseline = fallback.propose("RUN-1", results)

    model = ScriptedModel(
        [
            ModelResponse(text=json.dumps({"question": "질문 A"}, ensure_ascii=False)),
            ModelResponse(text=json.dumps({"question": "질문 B"}, ensure_ascii=False)),
        ]
    )
    agent = ModelNextBestEvidenceAgent(
        model=model, guardrail=guardrail, fallback=fallback, trace=TraceCollector()
    )
    requests = agent.propose("RUN-1", results)

    assert [r.criterion_id for r in requests] == [
        r.criterion_id for r in baseline
    ]
    assert [r.priority for r in requests] == [r.priority for r in baseline]
    assert [r.information_value for r in requests] == [
        r.information_value for r in baseline
    ]
    assert [r.question for r in requests] == ["질문 A", "질문 B"]


def test_model_question_falls_back_on_error() -> None:
    from app.actions.next_best import NextBestEvidenceAgent
    from agent.narration import ModelNextBestEvidenceAgent
    from app.domain.models import CriterionResult
    from app.domain.states import CriterionStatus
    from app.safety.guardrails import LocalGuardrail

    results = [
        CriterionResult(
            criterion_id="C01",
            criterion_type="INCLUSION",
            label="HbA1c",
            field_name="hba1c",
            kind=CriterionKind.NUMERIC_POINT,
            status=CriterionStatus.UNKNOWN,
            observed_value=None,
            expected_condition="7.5-10.5",
            unit="%",
            observed_at=None,
            confidence=0.0,
            explanation="",
            source_ids=(),
        )
    ]
    guardrail = LocalGuardrail(attach_disclaimer=False)
    fallback = NextBestEvidenceAgent(guardrail)
    agent = ModelNextBestEvidenceAgent(
        model=FailingModel(),
        guardrail=guardrail,
        fallback=fallback,
        trace=TraceCollector(),
    )
    requests = agent.propose("RUN-1", results)
    assert requests[0].question == fallback.propose("RUN-1", results)[0].question


# ---------------------------------------------------------------------------
# 컨테이너 배선과 종단 동작
# ---------------------------------------------------------------------------


def test_default_container_is_deterministic() -> None:
    """기본 설정에서는 모델을 쓰지 않는다."""
    from app.config import settings

    container = build_container(settings.data_dir)
    assert container.agent.enabled is False
    assert container.agent.mode == "deterministic"
    assert container.orchestrator._gatherer is None


def test_container_with_stub_model_enables_agent_mode() -> None:
    from app.config import settings

    container = build_container(
        settings.data_dir, model_client=StubModelClient()
    )
    assert container.agent.enabled is True
    assert container.agent.mode == "agent:stub"


def test_bedrock_failure_falls_back_to_stub() -> None:
    """Bedrock 을 켰지만 붙지 못하면 스텁으로 내려앉고 서비스는 산다."""
    from app.config import settings

    class Unavailable(ModelSettings):
        pass

    config = ModelSettings(
        enabled=True,
        region="us-east-1",
        model_id="m",
        max_tokens=256,
        max_agent_iterations=2,
        guardrail_id=None,
        guardrail_version="DRAFT",
    )
    import app.container as container_module

    original = container_module._build_model_client
    container_module._build_model_client = lambda cfg: (None, "자격 증명 없음")
    try:
        container = build_container(settings.data_dir, model_config=config)
    finally:
        container_module._build_model_client = original

    assert container.agent.enabled is True
    assert container.agent.mode == "agent:stub"
    assert container.agent.fallback_reason == "자격 증명 없음"


def test_agent_mode_run_matches_deterministic_verdict() -> None:
    """스텁 에이전트를 붙여도 판정 결과는 결정론적 경로와 같다.

    상태 확정을 Aggregator 가 독점한다는 설계가 실제로 지켜지는지 확인한다.
    """
    from app.config import settings

    plain = build_container(settings.data_dir)
    agentic = build_container(settings.data_dir, model_client=StubModelClient())

    trial_id = "SYN-T2D-INTENSIFY-01"
    person_id = sorted(plain.repository.patients)[0]

    a = plain.orchestrator.run(person_id=person_id, trial_id=trial_id)
    b = agentic.orchestrator.run(person_id=person_id, trial_id=trial_id)

    assert a.outcome.eligibility_status == b.outcome.eligibility_status
    assert a.outcome.criteria_met == b.outcome.criteria_met
    assert [r.status for r in a.run.results] == [r.status for r in b.run.results]
    assert b.run.metadata["mode"] == "agent:stub"
    assert b.run.metadata["agent"]["enabled"] is True


def test_agent_run_is_reproducible() -> None:
    """같은 입력을 두 번 실행하면 같은 상태가 나온다."""
    from app.config import settings

    container = build_container(
        settings.data_dir, model_client=StubModelClient()
    )
    trial_id = "SYN-DKD-01"
    person_id = sorted(container.repository.patients)[0]

    first = container.orchestrator.run(person_id=person_id, trial_id=trial_id)
    second = container.orchestrator.run(person_id=person_id, trial_id=trial_id)

    assert first.outcome.eligibility_status == second.outcome.eligibility_status
    assert [r.status for r in first.run.results] == [
        r.status for r in second.run.results
    ]


def test_agent_model_spans_recorded() -> None:
    """모델 호출이 Trace 에 MODEL 스팬으로 남는다."""
    from app.config import settings

    container = build_container(
        settings.data_dir, model_client=StubModelClient()
    )
    person_id = sorted(container.repository.patients)[0]
    output = container.orchestrator.run(
        person_id=person_id, trial_id="SYN-T2D-INTENSIFY-01"
    )

    spans = container.trace.spans_for(output.run.run_id)
    model_spans = [s for s in spans if s["kind"] == "MODEL"]
    assert model_spans, "MODEL 스팬이 기록되지 않았습니다."
    names = {s["name"] for s in model_spans}
    assert "model:nli_verifier" in names
    assert "model:explanation" in names


def test_architecture_endpoint_reports_agent_status() -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        body = client.get("/api/v1/architecture").json()
        assert "agent" in body
        assert body["agent"]["mode"] == "deterministic"
        assert set(body["agent"]["exposed_tools"]) == {
            "evidence_retrieval_tool",
            "timeline_graph_tool",
        }
        managed = {item["name"]: item for item in body["agent"]["managed_agents"]}
        assert "screening_orchestrator" in managed
        assert "rag_evidence_retrieval" in managed
        assert "intake_agent" in managed
        # Intake 는 규칙 추출기만으로 완결되므로 모델이 꺼져 있어도 동작한다.
        assert managed["intake_agent"]["enabled"] is True
        assert managed["intake_agent"]["mode"] == "deterministic"
        assert managed["intake_agent"]["model_backed"] is False


def test_stub_model_is_deterministic() -> None:
    """같은 입력에 같은 출력. 스텁이 재현성을 깨지 않는다."""
    payload = {"task": "nli", "has_observation": True, "rule_satisfied": True}
    outputs = []
    for _ in range(3):
        conversation = Conversation()
        conversation.user_text(json.dumps(payload, ensure_ascii=False))
        outputs.append(
            StubModelClient()
            .converse(conversation=conversation, system="s")
            .text
        )
    assert len(set(outputs)) == 1


def test_agent_loop_does_not_repeat_fruitless_search() -> None:
    """검색 결과가 비어도 같은 기준을 다시 검색하지 않는다.

    상태를 갱신하지 않으면 모델이 낡은 목록을 보고 같은 요청을 되풀이한다.
    """
    from agent.loop import EvidenceGatheringAgent
    from app.orchestration.router import CriterionRouter
    from app.tools.base import ToolContext

    class EmptyGateway:
        def __init__(self) -> None:
            self.calls: list[tuple[str, Any]] = []

        def invoke(self, name, context, /, **kwargs):
            self.calls.append((name, kwargs.get("terms") or kwargs.get("fields")))
            if name == "evidence_retrieval_tool":
                return []
            return {
                field: Observation(
                    field_name=field,
                    value=False,
                    unit="boolean",
                    observed_at="2020-01-01",
                    source="TIMELINE_GRAPH",
                    source_id="ENC-1",
                )
                for field in kwargs.get("fields", [])
            }

    rule = CriterionRule(
        criterion_id="C09",
        criterion_type="EXCLUSION",
        field_name="active_pregnancy",
        operator="=",
        value_low="false",
        value_high=None,
        unit="boolean",
        label="임신 여부",
        kind=CriterionKind.DERIVED_BOOLEAN,
        trial_id="T",
        criteria_version="v1",
    )
    plan = CriterionRouter().plan([rule])

    search = ModelResponse(
        text="",
        tool_uses=(
            ToolUse("tu-1", "evidence_retrieval_tool", {"terms": ["임신 중"]}),
        ),
        stop_reason="tool_use",
    )
    lookup = ModelResponse(
        text="",
        tool_uses=(
            ToolUse("tu-2", "timeline_graph_tool", {"fields": ["active_pregnancy"]}),
        ),
        stop_reason="tool_use",
    )
    gateway = EmptyGateway()
    agent = EvidenceGatheringAgent(
        model=ScriptedModel([search, lookup, search, search]),
        gateway=gateway,
        trace=TraceCollector(),
        max_iterations=5,
    )
    result = agent.gather(
        ToolContext("RUN-9", 1, "T", "ENC-1", "2020-01-01"),
        plan,
        known_narratives={},
        known_observations={},
    )

    assert result.stopped_reason == "gathered_all"
    assert result.iterations == 2
    searches = [c for c in gateway.calls if c[0] == "evidence_retrieval_tool"]
    assert len(searches) == 1, "결과가 빈 검색이 반복되었습니다."


def test_agent_loop_refreshes_state_inside_tool_result_message() -> None:
    """갱신된 상태는 toolResult 와 같은 메시지에 담긴다.

    별도 user 메시지로 보내면 Converse API 역할 교대 규칙을 깬다.
    """
    from agent.loop import EvidenceGatheringAgent
    from app.orchestration.router import CriterionRouter
    from app.tools.base import ToolContext

    class NullGateway:
        def invoke(self, name, context, /, **kwargs):
            return [] if name == "evidence_retrieval_tool" else {}

    rules = [
        CriterionRule(
            criterion_id=f"C{i:02d}",
            criterion_type="EXCLUSION",
            field_name=field,
            operator="=",
            value_low="false",
            value_high=None,
            unit="boolean",
            label=field,
            kind=CriterionKind.DERIVED_BOOLEAN,
            trial_id="T",
            criteria_version="v1",
        )
        for i, field in enumerate(("active_pregnancy", "uncontrolled_bp"), start=1)
    ]
    plan = CriterionRouter().plan(rules)

    search = ModelResponse(
        text="",
        tool_uses=(
            ToolUse("tu", "evidence_retrieval_tool", {"terms": ["임신 중"]}),
        ),
        stop_reason="tool_use",
    )
    model = ScriptedModel([search, ModelResponse(text=json.dumps({"done": True}))])
    agent = EvidenceGatheringAgent(
        model=model, gateway=NullGateway(), trace=TraceCollector(), max_iterations=4
    )
    agent.gather(
        ToolContext("RUN-10", 1, "T", "ENC-1", "2020-01-01"),
        plan,
        known_narratives={},
        known_observations={},
    )

    messages = model.requests[-1]["messages"]
    roles = [m["role"] for m in messages]
    # user / assistant 가 번갈아 나와야 한다.
    assert all(a != b for a, b in zip(roles, roles[1:])), roles
    last_user = messages[-1]
    kinds = {next(iter(block)) for block in last_user["content"]}
    assert "toolResult" in kinds
    assert "text" in kinds
