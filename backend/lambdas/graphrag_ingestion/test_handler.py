from __future__ import annotations

import pytest

from lambdas.graphrag_ingestion import handler as module


class FakeBedrockAgent:
    def __init__(self) -> None:
        self.started = None
        self.requested = None

    def start_ingestion_job(self, **kwargs):
        self.started = kwargs
        return {"ingestionJob": {"ingestionJobId": "JOB-1", "status": "STARTING"}}

    def get_ingestion_job(self, **kwargs):
        self.requested = kwargs
        return {
            "ingestionJob": {
                "ingestionJobId": kwargs["ingestionJobId"],
                "status": "COMPLETE",
            }
        }


@pytest.fixture
def fake(monkeypatch):
    client = FakeBedrockAgent()
    monkeypatch.setattr(module, "KNOWLEDGE_BASE_ID", "KB-1")
    monkeypatch.setattr(module, "DATA_SOURCE_ID", "DS-1")
    monkeypatch.setattr(module, "GRAPHRAG_REGION", "us-east-1")
    monkeypatch.setattr(module, "_client", client)
    return client


def test_start_returns_json_safe_summary(fake) -> None:
    result = module.handler({"action": "start", "description": "test"}, None)

    assert result == {
        "ingestion_job_id": "JOB-1",
        "status": "STARTING",
        "region": "us-east-1",
    }
    assert fake.started == {
        "knowledgeBaseId": "KB-1",
        "dataSourceId": "DS-1",
        "description": "test",
    }


def test_get_returns_status(fake) -> None:
    result = module.handler(
        {"action": "get", "ingestion_job_id": "JOB-1"}, None
    )

    assert result["status"] == "COMPLETE"
    assert result["failure_reasons"] == []
    assert fake.requested["ingestionJobId"] == "JOB-1"


def test_get_requires_job_id(fake) -> None:
    with pytest.raises(ValueError, match="ingestion_job_id"):
        module.handler({"action": "get"}, None)
