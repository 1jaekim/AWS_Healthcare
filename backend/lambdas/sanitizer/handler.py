"""
Python Sanitizer Lambda
─────────────────────────
clinical_notes.jsonl (10,485개 노트)에서:
1. PHI(개인건강정보) 마스킹 - 이름, 날짜, 전화번호, 주소 등
2. 판정 문장 제거 - 진단 확정/판정 관련 문장 필터링
3. canonical 노트를 환자별 Markdown으로 집계 → S3 rag/patients/ 저장

입력: S3 raw/ 경로의 clinical_notes.jsonl
출력: S3 rag/patients/ 경로의 Markdown + Bedrock metadata sidecar
"""
import json
import logging
import os
from functools import lru_cache
from typing import Any

import boto3

try:
    from .graphrag_documents import build_patient_documents
except ImportError:  # Lambda zip은 현재 디렉터리를 모듈 루트로 사용한다.
    from graphrag_documents import build_patient_documents

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

s3_client = boto3.client("s3")
secrets_client = boto3.client("secretsmanager")

# ─── 환경변수 ────────────────────────────────────────────
BUCKET_NAME = os.environ.get("S3_BUCKET_NAME", "aws-healthcare-data")
RAW_PREFIX = os.environ.get("S3_PREFIX_RAW", "raw/")
RAG_PREFIX = os.environ.get("S3_PREFIX_RAG", "rag/")
PSEUDONYM_SECRET_ARN = os.environ.get("PATIENT_PSEUDONYM_SECRET_ARN", "")


@lru_cache(maxsize=1)
def get_pseudonymization_secret() -> str:
    """Load the HMAC secret once; production uses Secrets Manager."""

    local_secret = os.environ.get("PATIENT_PSEUDONYM_SECRET")
    if local_secret:
        return local_secret
    if not PSEUDONYM_SECRET_ARN:
        raise RuntimeError("PATIENT_PSEUDONYM_SECRET_ARN is required")
    response = secrets_client.get_secret_value(SecretId=PSEUDONYM_SECRET_ARN)
    secret = response.get("SecretString", "")
    if not secret:
        raise RuntimeError("pseudonymization secret is empty")
    return secret


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """
    Lambda 핸들러
    event 형식:
        - S3 이벤트 (Step Functions에서 호출)
        - 또는 직접 호출: {"source_key": "raw/clinical_notes.jsonl"}
    """
    logger.info("Sanitizer Lambda 시작")

    # S3 소스 키 결정
    source_key = None
    if "Records" in event:
        # S3 이벤트 트리거
        record = event["Records"][0]
        source_key = record["s3"]["object"]["key"]
    else:
        source_key = event.get("source_key", f"{RAW_PREFIX}clinical_notes.jsonl")

    logger.info(f"처리 대상: s3://{BUCKET_NAME}/{source_key}")

    # S3에서 파일 읽기
    try:
        response = s3_client.get_object(Bucket=BUCKET_NAME, Key=source_key)
        body = response["Body"].read().decode("utf-8")
    except Exception as e:
        logger.error(f"S3 읽기 실패: {e}")
        raise

    # JSONL 파싱 및 처리
    source_notes = []
    total_count = 0
    error_count = 0

    for line in body.strip().split("\n"):
        if not line.strip():
            continue
        total_count += 1
        try:
            note = json.loads(line)
            source_notes.append(note)
        except json.JSONDecodeError as e:
            error_count += 1
            logger.warning(f"JSON 파싱 오류 (line {total_count}): {e}")

    logger.info(
        f"처리 완료: 전체 {total_count}건, "
        f"파싱 {len(source_notes)}건, "
        f"오류 {error_count}건"
    )

    documents = build_patient_documents(
        source_notes,
        pseudonymization_secret=get_pseudonymization_secret(),
    )
    canonical_count = sum(document.note_count for document in documents)
    output_prefix = f"{RAG_PREFIX}patients/"

    try:
        for document in documents:
            document_key = f"{output_prefix}{document.patient_key}.md"
            metadata_key = f"{document_key}.metadata.json"
            s3_client.put_object(
                Bucket=BUCKET_NAME,
                Key=document_key,
                Body=document.body.encode("utf-8"),
                ContentType="text/markdown; charset=utf-8",
                ServerSideEncryption="aws:kms",
            )
            s3_client.put_object(
                Bucket=BUCKET_NAME,
                Key=metadata_key,
                Body=json.dumps(document.metadata, ensure_ascii=False).encode("utf-8"),
                ContentType="application/json",
                ServerSideEncryption="aws:kms",
            )
        logger.info(
            "GraphRAG 문서 저장 완료: s3://%s/%s (%s명)",
            BUCKET_NAME,
            output_prefix,
            len(documents),
        )
    except Exception as e:
        logger.error(f"S3 저장 실패: {e}")
        raise

    # 원본도 raw/에 보존 (이미 있으면 skip)
    result = {
        "status": "success",
        "total_input": total_count,
        "canonical_output": canonical_count,
        "patient_documents": len(documents),
        "errors": error_count,
        "output_key": output_prefix,
        "bucket": BUCKET_NAME,
    }

    logger.info(f"결과: {json.dumps(result)}")
    return result
