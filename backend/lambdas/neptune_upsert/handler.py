"""
Neptune Bulk Loader + 증분 Upsert Lambda
──────────────────────────────────────────
1. 초기 적재: Neptune Bulk Loader API로 graph/ CSV 일괄 로드
2. 증분 업데이트: S3 graph/ 이벤트 → Gremlin upsert 쿼리

트리거:
  - Step Functions에서 초기 적재 호출
  - S3 graph/ 경로 ObjectCreated 이벤트 (증분)

Neptune 연결:
  - IAM 인증 (SigV4)
  - VPC 내부 Lambda → Neptune 엔드포인트
"""
import csv
import io
import json
import logging
import os
from typing import Any
from urllib.parse import quote_plus

import boto3
import requests
from requests_aws4auth import AWS4Auth

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

s3_client = boto3.client("s3")
neptune_client = boto3.client("neptunedata")

# ─── 환경변수 ────────────────────────────────────────────
NEPTUNE_ENDPOINT = os.environ.get("NEPTUNE_ENDPOINT", "")
NEPTUNE_PORT = int(os.environ.get("NEPTUNE_PORT", "8182"))
NEPTUNE_LOADER_ROLE_ARN = os.environ.get("NEPTUNE_LOADER_ROLE_ARN", "")
BUCKET_NAME = os.environ.get("S3_BUCKET_NAME", "aws-healthcare-data")
GRAPH_PREFIX = os.environ.get("S3_PREFIX_GRAPH", "graph/")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# Neptune Gremlin 엔드포인트
GREMLIN_ENDPOINT = f"https://{NEPTUNE_ENDPOINT}:{NEPTUNE_PORT}/gremlin"
LOADER_ENDPOINT = f"https://{NEPTUNE_ENDPOINT}:{NEPTUNE_PORT}/loader"


def get_aws_auth() -> AWS4Auth:
    """IAM 기반 SigV4 인증 생성"""
    session = boto3.Session()
    credentials = session.get_credentials().get_frozen_credentials()
    return AWS4Auth(
        credentials.access_key,
        credentials.secret_key,
        AWS_REGION,
        "neptune-db",
        session_token=credentials.token,
    )


# ═══════════════════════════════════════════════════════════
# 초기 적재 (Bulk Loader)
# ═══════════════════════════════════════════════════════════

def start_bulk_load(source_s3_uri: str, format_type: str = "csv") -> dict:
    """
    Neptune Bulk Loader로 초기 데이터 적재
    - graph/nodes/ → 노드 로드
    - graph/edges/ → 엣지 로드
    """
    payload = {
        "source": source_s3_uri,
        "format": format_type,
        "iamRoleArn": NEPTUNE_LOADER_ROLE_ARN,
        "region": AWS_REGION,
        "failOnError": "FALSE",
        "parallelism": "MEDIUM",
        "updateSingleCardinalityProperties": "TRUE",
        "queueRequest": "TRUE",
    }

    auth = get_aws_auth()
    response = requests.post(
        LOADER_ENDPOINT,
        json=payload,
        auth=auth,
        headers={"Content-Type": "application/json"},
        timeout=30,
    )

    if response.status_code == 200:
        result = response.json()
        logger.info(f"Bulk Load 시작: {result}")
        return result
    else:
        logger.error(f"Bulk Load 실패: {response.status_code} - {response.text}")
        raise RuntimeError(f"Neptune Bulk Loader 오류: {response.status_code}")


def check_load_status(load_id: str) -> dict:
    """Bulk Load 작업 상태 확인"""
    auth = get_aws_auth()
    response = requests.get(
        f"{LOADER_ENDPOINT}/{load_id}",
        auth=auth,
        params={"details": "true", "errors": "true"},
        timeout=30,
    )
    return response.json()


# ═══════════════════════════════════════════════════════════
# 증분 Upsert (Gremlin)
# ═══════════════════════════════════════════════════════════

def execute_gremlin(query: str) -> dict:
    """Gremlin 쿼리 실행"""
    auth = get_aws_auth()
    payload = {"gremlin": query}
    response = requests.post(
        GREMLIN_ENDPOINT,
        json=payload,
        auth=auth,
        headers={"Content-Type": "application/json"},
        timeout=60,
    )

    if response.status_code == 200:
        return response.json()
    else:
        logger.error(f"Gremlin 오류: {response.status_code} - {response.text}")
        raise RuntimeError(f"Gremlin 쿼리 실패: {response.status_code}")


def upsert_node(node_id: str, label: str, properties: dict) -> None:
    """노드 Upsert (존재하면 업데이트, 없으면 생성)"""
    # Gremlin upsert 패턴: fold + coalesce
    props_query = ""
    for key, value in properties.items():
        if value is not None and value != "":
            escaped_value = str(value).replace("'", "\\'")
            props_query += f".property('{key}', '{escaped_value}')"

    query = (
        f"g.V('{node_id}').fold()"
        f".coalesce(unfold(), addV('{label}').property(id, '{node_id}'))"
        f"{props_query}"
    )

    execute_gremlin(query)


def upsert_edge(edge_id: str, from_id: str, to_id: str, label: str, properties: dict | None = None) -> None:
    """엣지 Upsert"""
    props_query = ""
    if properties:
        for key, value in properties.items():
            if value is not None and value != "":
                escaped_value = str(value).replace("'", "\\'")
                props_query += f".property('{key}', '{escaped_value}')"

    query = (
        f"g.E('{edge_id}').fold()"
        f".coalesce(unfold(), "
        f"g.V('{from_id}').addE('{label}').to(g.V('{to_id}')).property(id, '{edge_id}'))"
        f"{props_query}"
    )

    execute_gremlin(query)


def process_node_csv(bucket: str, key: str) -> int:
    """S3의 Node CSV를 읽어 Gremlin upsert"""
    response = s3_client.get_object(Bucket=bucket, Key=key)
    content = response["Body"].read().decode("utf-8")
    reader = csv.DictReader(io.StringIO(content))

    count = 0
    for row in reader:
        node_id = row.pop("~id", None)
        label = row.pop("~label", "Unknown")
        if not node_id:
            continue

        # 타입 접미사 제거 (예: "name:String" → "name")
        properties = {}
        for k, v in row.items():
            clean_key = k.split(":")[0]
            properties[clean_key] = v

        upsert_node(node_id, label, properties)
        count += 1

    logger.info(f"노드 upsert 완료: {key} → {count}건")
    return count


def process_edge_csv(bucket: str, key: str) -> int:
    """S3의 Edge CSV를 읽어 Gremlin upsert"""
    response = s3_client.get_object(Bucket=bucket, Key=key)
    content = response["Body"].read().decode("utf-8")
    reader = csv.DictReader(io.StringIO(content))

    count = 0
    for row in reader:
        edge_id = row.pop("~id", None)
        from_id = row.pop("~from", None)
        to_id = row.pop("~to", None)
        label = row.pop("~label", "RELATED")
        if not all([edge_id, from_id, to_id]):
            continue

        properties = {}
        for k, v in row.items():
            clean_key = k.split(":")[0]
            properties[clean_key] = v

        upsert_edge(edge_id, from_id, to_id, label, properties)
        count += 1

    logger.info(f"엣지 upsert 완료: {key} → {count}건")
    return count


# ═══════════════════════════════════════════════════════════
# Lambda 핸들러
# ═══════════════════════════════════════════════════════════

def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """
    Lambda 핸들러
    
    모드 1 - 초기 적재 (Step Functions 호출):
        {"mode": "bulk_load", "source": "s3://bucket/graph/"}
    
    모드 2 - 적재 상태 확인:
        {"mode": "check_status", "load_id": "xxx-xxx"}
    
    모드 3 - 증분 Upsert (S3 이벤트 트리거):
        {"Records": [...]}  # S3 ObjectCreated 이벤트
    """
    logger.info(f"Neptune Upsert Lambda 시작: {json.dumps(event)[:500]}")

    mode = event.get("mode")

    # ─── 모드 1: Bulk Load 시작 ──────────────────────────
    if mode == "bulk_load":
        source = event.get("source", f"s3://{BUCKET_NAME}/{GRAPH_PREFIX}")
        result = start_bulk_load(source)
        return {
            "status": "bulk_load_started",
            "load_id": result.get("payload", {}).get("loadId", ""),
            "response": result,
        }

    # ─── 모드 2: 상태 확인 ───────────────────────────────
    if mode == "check_status":
        load_id = event.get("load_id", "")
        if not load_id:
            return {"status": "error", "message": "load_id 필요"}
        result = check_load_status(load_id)
        return {
            "status": "checked",
            "load_status": result,
        }

    # ─── 모드 3: 증분 Upsert (S3 이벤트) ────────────────
    if "Records" in event:
        total_nodes = 0
        total_edges = 0

        for record in event["Records"]:
            bucket = record["s3"]["bucket"]["name"]
            key = record["s3"]["object"]["key"]

            logger.info(f"증분 처리: s3://{bucket}/{key}")

            if "/nodes/" in key and key.endswith(".csv"):
                total_nodes += process_node_csv(bucket, key)
            elif "/edges/" in key and key.endswith(".csv"):
                total_edges += process_edge_csv(bucket, key)
            else:
                logger.info(f"건너뜀 (대상 아님): {key}")

        return {
            "status": "upsert_complete",
            "nodes_upserted": total_nodes,
            "edges_upserted": total_edges,
        }

    return {"status": "error", "message": "알 수 없는 이벤트 형식"}
