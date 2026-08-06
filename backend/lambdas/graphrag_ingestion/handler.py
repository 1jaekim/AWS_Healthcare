"""Cross-region Bedrock Knowledge Base ingestion adapter.

The Step Functions state machine runs in Seoul, while the GraphRAG knowledge
base runs in Virginia. AWS SDK service integrations execute in the state
machine's region, so the workflow invokes this Lambda and the Lambda creates a
Bedrock Agent client for the explicit GraphRAG region.
"""

from __future__ import annotations

import os
from typing import Any

import boto3


KNOWLEDGE_BASE_ID = os.getenv("KNOWLEDGE_BASE_ID", "")
DATA_SOURCE_ID = os.getenv("DATA_SOURCE_ID", "")
GRAPHRAG_REGION = os.getenv("GRAPHRAG_REGION", "us-east-1")

_client: Any | None = None


def _bedrock_agent_client():
    global _client
    if _client is None:
        _client = boto3.client("bedrock-agent", region_name=GRAPHRAG_REGION)
    return _client


def _require_config() -> None:
    if not KNOWLEDGE_BASE_ID or not DATA_SOURCE_ID:
        raise RuntimeError("KNOWLEDGE_BASE_ID and DATA_SOURCE_ID are required")


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    _require_config()
    action = str(event.get("action", "")).strip().lower()
    client = _bedrock_agent_client()

    if action == "start":
        response = client.start_ingestion_job(
            knowledgeBaseId=KNOWLEDGE_BASE_ID,
            dataSourceId=DATA_SOURCE_ID,
            description=str(
                event.get("description") or "Public trial and standard references ingestion"
            ),
        )
        job = response["ingestionJob"]
        return {
            "ingestion_job_id": job["ingestionJobId"],
            "status": job["status"],
            "region": GRAPHRAG_REGION,
        }

    if action == "get":
        ingestion_job_id = str(event.get("ingestion_job_id", "")).strip()
        if not ingestion_job_id:
            raise ValueError("ingestion_job_id is required for get")
        response = client.get_ingestion_job(
            knowledgeBaseId=KNOWLEDGE_BASE_ID,
            dataSourceId=DATA_SOURCE_ID,
            ingestionJobId=ingestion_job_id,
        )
        job = response["ingestionJob"]
        return {
            "ingestion_job_id": job["ingestionJobId"],
            "status": job["status"],
            "failure_reasons": list(job.get("failureReasons") or []),
            "region": GRAPHRAG_REGION,
        }

    raise ValueError("action must be 'start' or 'get'")
