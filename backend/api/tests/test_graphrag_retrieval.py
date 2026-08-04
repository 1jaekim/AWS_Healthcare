"""EvidenceGatheringAgent가 사용하는 Bedrock GraphRAG Tool 계약."""

from __future__ import annotations

import json

import pytest
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
from app.safety.pseudonyms import PatientKeyResolver, pseudonymize_patient_id
from app.tools.base import ToolContext
from app.tools.evidence_retrieval import (
    BedrockKnowledgeBaseEvidenceRetrievalTool,
)
from backend.lambdas.sanitizer.graphrag_documents import pseudonymize_identifier


SECRET = "shared-test-secret"


class CapturingBedrock:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def retrieve(self, **kwargs):
        self.calls.append(kwargs)
        patient_key = kwargs["retrievalConfiguration"]["vectorSearchConfiguration"][
            "filter"
        ]["andAll"][0]["equals"]["value"]
        return {
            "retrievalResults": [
                {
                    "content": {
                        "text": (
                            "- 이벤트 참조: evt_abc123\n"
                            "HbA1c 7.8%로 기록되어 있다."
                        )
                    },
                    "score": 0.87,
                    "metadata": {
                        "patient_key": patient_key,
                        "document_type": "patient_evidence",
                    },
                    "location": {
                        "s3Location": {
                            "uri": "s3://bucket/rag/patients/pt_expected.md"
                        }
                    },
                }
            ]
        }


def _context(patient_key: str | None = "pt_expected") -> ToolContext:
    return ToolContext(
        run_id="RUN-RAG",
        person_id=12345,
        trial_id="TRIAL-1",
        index_encounter_id="raw-encounter",
        index_date="2026-01-01",
        patient_key=patient_key,
    )


def test_bedrock_retrieve_always_filters_by_patient_and_document_type() -> None:
    client = CapturingBedrock()
    tool = BedrockKnowledgeBaseEvidenceRetrievalTool(
        knowledge_base_id="ABCDEFGHIJ",
        client=client,
    )

    results = tool.invoke(_context(), terms=("HbA1c",), top_k=3)

    assert len(client.calls) == 1
    request = client.calls[0]
    assert request["knowledgeBaseId"] == "ABCDEFGHIJ"
    filters = request["retrievalConfiguration"]["vectorSearchConfiguration"][
        "filter"
    ]["andAll"]
    assert filters == [
        {"equals": {"key": "patient_key", "value": "pt_expected"}},
        {
            "equals": {
                "key": "document_type",
                "value": "patient_evidence",
            }
        },
    ]
    serialized = json.dumps(request, ensure_ascii=False)
    assert "12345" not in serialized
    assert "raw-encounter" not in serialized
    assert "person_id" not in serialized
    assert results[0].note_id == "evt_abc123"
    assert results[0].score == 0.87
    operation = get_session().get_service_model("bedrock-agent-runtime").operation_model(
        "Retrieve"
    )
    validate_parameters(request, operation.input_shape)


def test_bedrock_retrieve_rejects_missing_patient_key_before_api_call() -> None:
    client = CapturingBedrock()
    tool = BedrockKnowledgeBaseEvidenceRetrievalTool(
        knowledge_base_id="ABCDEFGHIJ",
        client=client,
    )

    with pytest.raises(ValueError, match="patient_key"):
        tool.invoke(_context(None), terms=("임신",), top_k=3)

    assert client.calls == []


def test_mismatched_patient_metadata_is_discarded() -> None:
    response = {
        "retrievalResults": [
            {
                "content": {"text": "다른 환자의 HbA1c 기록"},
                "score": 0.99,
                "metadata": {
                    "patient_key": "pt_other",
                    "document_type": "patient_evidence",
                },
            }
        ]
    }

    assert BedrockKnowledgeBaseEvidenceRetrievalTool._snippets(
        response,
        ("HbA1c",),
        3,
        "pt_expected",
    ) == []


def test_api_and_sanitizer_use_same_patient_key_contract() -> None:
    assert pseudonymize_patient_id(12345, SECRET) == pseudonymize_identifier(
        "pt", "12345", SECRET
    )


def test_secret_manager_value_is_loaded_once() -> None:
    class Secrets:
        calls = 0

        def get_secret_value(self, **kwargs):
            self.calls += 1
            assert kwargs == {"SecretId": "arn:secret"}
            return {"SecretString": SECRET}

    secrets = Secrets()
    resolver = PatientKeyResolver(
        secret_arn="arn:secret",
        secrets_client=secrets,
    )

    assert resolver(1).startswith("pt_")
    assert resolver(2).startswith("pt_")
    assert secrets.calls == 1


def test_container_registers_bedrock_adapter_when_kb_is_configured() -> None:
    client = CapturingBedrock()
    container = build_container(
        settings.data_dir,
        graphrag_config=GraphRagSettings(
            knowledge_base_id="ABCDEFGHIJ",
            patient_pseudonym_secret=SECRET,
        ),
        retrieval_client=client,
    )
    patient_key = pseudonymize_patient_id(12345, SECRET)

    results = container.gateway.invoke(
        "evidence_retrieval_tool",
        _context(patient_key),
        terms=("HbA1c",),
        top_k=3,
    )

    assert container.retrieval_mode == "bedrock_graphrag"
    assert results[0].note_id == "evt_abc123"
    assert client.calls[0]["retrievalConfiguration"]["vectorSearchConfiguration"][
        "filter"
    ]["andAll"][0]["equals"]["value"] == patient_key


def test_configured_kb_never_silently_falls_back_without_pseudonym_secret() -> None:
    with pytest.raises(ValueError, match="PATIENT_PSEUDONYM_SECRET"):
        build_container(
            settings.data_dir,
            graphrag_config=GraphRagSettings(
                knowledge_base_id="ABCDEFGHIJ",
                patient_pseudonym_secret=None,
                patient_pseudonym_secret_arn=None,
            ),
            retrieval_client=CapturingBedrock(),
        )


def test_evidence_gathering_agent_reaches_bedrock_through_gateway() -> None:
    client = CapturingBedrock()
    tool = BedrockKnowledgeBaseEvidenceRetrievalTool(
        knowledge_base_id="ABCDEFGHIJ",
        client=client,
    )
    trace = TraceCollector()
    gateway = ToolGateway(trace)
    gateway.register(tool, allow=default_policy()[tool.name])

    rule = CriterionRule(
        criterion_id="T-C01",
        criterion_type="INCLUSION",
        field_name="hba1c",
        operator="between",
        value_low="7.5",
        value_high="10.5",
        unit="%",
        label="최근 HbA1c",
        kind=CriterionKind.NARRATIVE,
        trial_id="TRIAL-1",
        criteria_version="v1",
    )
    plan = ExecutionPlan(
        plans=[
            CriterionPlan(
                rule=rule,
                needs_graph=False,
                needs_narrative=True,
                narrative_terms=("HbA1c",),
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
                        {"terms": ["HbA1c"], "top_k": 3},
                    ),
                ),
                stop_reason="tool_use",
            )

    model = OneToolCall()
    agent = EvidenceGatheringAgent(
        model=model,
        gateway=gateway,
        trace=trace,
        max_iterations=2,
    )
    context = _context(pseudonymize_patient_id(12345, SECRET))

    result = agent.gather(
        context,
        plan,
        known_narratives={},
        known_observations={},
    )

    assert result.stopped_reason == "gathered_all"
    assert result.tool_calls == ["evidence_retrieval_tool"]
    assert result.narratives["T-C01"][0].note_id == "evt_abc123"
    assert len(client.calls) == 1
    prompt_text = model.messages[0]["content"][0]["text"]
    assert context.patient_key in prompt_text
    assert '"person_id"' not in prompt_text
