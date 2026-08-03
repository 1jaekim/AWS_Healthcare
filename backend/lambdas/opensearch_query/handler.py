"""
OpenSearch 쿼리 API Lambda
───────────────────────────
Bedrock Knowledge Bases의 Retrieve API 또는
OpenSearch Serverless 직접 쿼리를 통해 EMR 검색 제공

기능:
  - 자연어 쿼리 → Bedrock KB Retrieve (벡터 + 키워드 하이브리드)
  - 직접 벡터 검색 (OpenSearch kNN)
  - 필터 기반 검색 (환자 속성, 노트 유형 등)
"""
import json
import logging
import os
from typing import Any

import boto3

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

bedrock_agent_client = boto3.client("bedrock-agent-runtime")
opensearch_client = boto3.client("opensearchserverless")

# ─── 환경변수 ────────────────────────────────────────────
KNOWLEDGE_BASE_ID = os.environ.get("KNOWLEDGE_BASE_ID", "")
OPENSEARCH_ENDPOINT = os.environ.get("OPENSEARCH_ENDPOINT", "")
INDEX_NAME = os.environ.get("OPENSEARCH_INDEX_NAME", "emr-vectors")


def retrieve_from_kb(query: str, top_k: int = 10, filters: dict | None = None) -> list[dict]:
    """
    Bedrock Knowledge Base Retrieve API를 통한 검색
    벡터 유사도 + 키워드 하이브리드 검색
    """
    retrieve_params = {
        "knowledgeBaseId": KNOWLEDGE_BASE_ID,
        "retrievalQuery": {"text": query},
        "retrievalConfiguration": {
            "vectorSearchConfiguration": {
                "numberOfResults": top_k,
            }
        },
    }

    # 필터 적용
    if filters:
        filter_config = build_filter(filters)
        if filter_config:
            retrieve_params["retrievalConfiguration"]["vectorSearchConfiguration"]["filter"] = filter_config

    try:
        response = bedrock_agent_client.retrieve(**retrieve_params)
        results = []
        for item in response.get("retrievalResults", []):
            results.append({
                "text": item.get("content", {}).get("text", ""),
                "score": item.get("score", 0.0),
                "metadata": item.get("metadata", {}),
                "location": item.get("location", {}),
            })
        return results
    except Exception as e:
        logger.error(f"KB Retrieve 실패: {e}")
        raise


def build_filter(filters: dict) -> dict | None:
    """검색 필터 구성"""
    conditions = []

    if "note_type" in filters:
        conditions.append({
            "equals": {
                "key": "note_type",
                "value": filters["note_type"],
            }
        })

    if "patient_id" in filters:
        conditions.append({
            "equals": {
                "key": "patient_id",
                "value": filters["patient_id"],
            }
        })

    if not conditions:
        return None
    if len(conditions) == 1:
        return conditions[0]
    return {"andAll": conditions}


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """
    Lambda 핸들러
    event 형식:
        {
            "query": "환자의 혈당 수치 변화",
            "top_k": 10,
            "filters": {"note_type": "lab_result"},
            "search_type": "knowledge_base"  # "knowledge_base" | "direct"
        }
    """
    logger.info(f"OpenSearch 쿼리 요청: {json.dumps(event, ensure_ascii=False)}")

    query = event.get("query", "")
    if not query:
        return {
            "statusCode": 400,
            "body": json.dumps({"error": "query 파라미터가 필요합니다"}, ensure_ascii=False),
        }

    top_k = event.get("top_k", 10)
    filters = event.get("filters")
    search_type = event.get("search_type", "knowledge_base")

    if search_type == "knowledge_base":
        results = retrieve_from_kb(query, top_k, filters)
    else:
        # 향후 OpenSearch 직접 쿼리 구현
        results = retrieve_from_kb(query, top_k, filters)

    response = {
        "statusCode": 200,
        "body": json.dumps(
            {
                "query": query,
                "result_count": len(results),
                "results": results,
            },
            ensure_ascii=False,
        ),
    }

    logger.info(f"검색 결과: {len(results)}건 반환")
    return response
