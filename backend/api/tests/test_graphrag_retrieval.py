"""공개 공고·표준문서용 Bedrock GraphRAG Tool 계약."""

from __future__ import annotations

import json

from botocore.session import get_session
from botocore.validate import validate_parameters

from agent.loop import EvidenceGatheringAgent
from agent.model import ModelResponse, ToolUse
from app.config import GraphRagSettings, settings
from app.container import build_container
from app.domain.models import CriterionRule
from app.domain.states import CriterionKind
from app.orchestration.gateway import ToolGateway, default_policy
from app.orchestration.router import CriterionPlan, ExecutionPlan
from app.safety.observability import TraceCollector
from app.tools.base import ToolContext
from app.tools.evidence_retrieval import (
    BedrockKnowledgeBaseEvidenceRetrievalTool,
)


class CapturingBedrock:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def retrieve(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "retrievalResults": [
                {
                    "content": {"text": "임신 중인 지원자는 제외한다."},
                    "score": 0.91,
                    "metadata": {
                        "document_type": "trial_notice",
                        "trial_id": "TRIAL-1",
                        "source_id": "trial-notice:TRIAL-1",
                        "title": "공개 모집 공고",
                    },
                    "location": {"s3Location": {"uri": "s3://bucket/rag/references/trials/TRIAL-1.md"}},
                },
                {
                    "content": {"text": "HbA1c 단위는 %로 표기한다."},
                    "score": 0.74,
                    "metadata": {
                        "document_type": "standard_document",
                        "source_id": "standard:hba1c",
                        "title": "검사 표준",
                    },
                },
                {
                    "content": {"text": "환자 HbA1c 8.2%"},
                    "score": 0.99,
                    "metadata": {
                        "document_type": "patient_evidence",
                        "patient_key": "pt_forbidden",
                    },
                },
            ]
        }


def _context() -> ToolContext:
    return ToolContext(
        run_id="RUN-RAG",
        person_id=None,
        trial_id="TRIAL-1",
        index_encounter_id="APPLICATION-APP-1",
        index_date="2026-08-06",
    )


def test_bedrock_retrieve_filters_public_references_without_patient_key() -> None:
    client = CapturingBedrock()
    tool = BedrockKnowledgeBaseEvidenceRetrievalTool(
        knowledge_base_id="ABCDEFGHIJ", client=client
    )

    results = tool.invoke(_context(), terms=("임신 중",), top_k=5)

    request = client.calls[0]
    filter_value = request["retrievalConfiguration"]["vectorSearchConfiguration"][
        "filter"
    ]
    assert filter_value == {
        "orAll": [
            {
                "andAll": [
                    {"equals": {"key": "document_type", "value": "trial_notice"}},
                    {"equals": {"key": "trial_id", "value": "TRIAL-1"}},
                ]
            },
            {"equals": {"key": "document_type", "value": "standard_document"}},
        ]
    }
    serialized = json.dumps(request, ensure_ascii=False)
    assert "patient_key" not in serialized
    assert "person_id" not in serialized
    assert [item.document_type for item in results] == [
        "trial_notice",
        "standard_document",
    ]
    assert results[0].note_id == "trial-notice:TRIAL-1"
    operation = get_session().get_service_model("bedrock-agent-runtime").operation_model(
        "Retrieve"
    )
    validate_parameters(request, operation.input_shape)


def test_container_configures_graphrag_without_pseudonym_secret() -> None:
    client = CapturingBedrock()
    container = build_container(
        settings.data_dir,
        graphrag_config=GraphRagSettings(knowledge_base_id="ABCDEFGHIJ"),
        retrieval_client=client,
    )

    results = container.gateway.invoke(
        "evidence_retrieval_tool", _context(), terms=("임신 중",), top_k=3
    )

    assert container.retrieval_mode == "bedrock_graphrag"
    assert results[0].document_type == "trial_notice"
    assert len(client.calls) == 1


def test_evidence_agent_reaches_reference_rag_without_patient_identity() -> None:
    client = CapturingBedrock()
    tool = BedrockKnowledgeBaseEvidenceRetrievalTool(
        knowledge_base_id="ABCDEFGHIJ", client=client
    )
    trace = TraceCollector()
    gateway = ToolGateway(trace)
    gateway.register(tool, allow=default_policy()[tool.name])
    rule = CriterionRule(
        criterion_id="T-C01",
        criterion_type="EXCLUSION",
        field_name="active_pregnancy",
        operator="=",
        value_low="false",
        value_high=None,
        unit="boolean",
        label="임신 여부",
        kind=CriterionKind.DERIVED_BOOLEAN,
        trial_id="TRIAL-1",
        criteria_version="v1",
    )
    plan = ExecutionPlan(
        plans=[
            CriterionPlan(
                rule=rule,
                needs_graph=False,
                needs_narrative=True,
                narrative_terms=("임신 중",),
                graph_fields=(),
            )
        ]
    )

    class OneToolCall:
        mode = "scripted"

        def __init__(self) -> None:
            self.messages = []

        def converse(self, *, conversation, system, tools=None):
            self.messages = list(conversation.messages)
            return ModelResponse(
                text="",
                tool_uses=(
                    ToolUse(
                        "tu-rag",
                        "evidence_retrieval_tool",
                        {"terms": ["임신 중"], "top_k": 3},
                    ),
                ),
                stop_reason="tool_use",
            )

    model = OneToolCall()
    result = EvidenceGatheringAgent(
        model=model, gateway=gateway, trace=trace, max_iterations=2
    ).gather(
        _context(), plan, known_narratives={}, known_observations={}
    )

    assert result.stopped_reason == "gathered_all"
    assert result.narratives["T-C01"][0].note_id == "trial-notice:TRIAL-1"
    prompt_text = model.messages[0]["content"][0]["text"]
    assert "patient_key" not in prompt_text
    assert "person_id" not in prompt_text
