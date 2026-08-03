"""
Python Sanitizer Lambda
─────────────────────────
clinical_notes.jsonl (10,485개 노트)에서:
1. PHI(개인건강정보) 마스킹 - 이름, 날짜, 전화번호, 주소 등
2. 판정 문장 제거 - 진단 확정/판정 관련 문장 필터링
3. canonical 2,097건 생성 → S3 rag/ 경로에 저장

입력: S3 raw/ 경로의 clinical_notes.jsonl
출력: S3 rag/ 경로의 비식별 EMR JSON
"""
import json
import logging
import os
import re
from typing import Any

import boto3

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

s3_client = boto3.client("s3")

# ─── 환경변수 ────────────────────────────────────────────
BUCKET_NAME = os.environ.get("S3_BUCKET_NAME", "aws-healthcare-data")
RAW_PREFIX = os.environ.get("S3_PREFIX_RAW", "raw/")
RAG_PREFIX = os.environ.get("S3_PREFIX_RAG", "rag/")

# ─── PHI 패턴 정의 ──────────────────────────────────────
PHI_PATTERNS = {
    "PERSON_NAME": re.compile(
        r"\b[A-Z][a-z]+(?:\s[A-Z][a-z]+){1,2}\b"
    ),
    "DATE": re.compile(
        r"\b(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}[/-]\d{1,2}[/-]\d{1,2})\b"
    ),
    "PHONE": re.compile(
        r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"
    ),
    "SSN": re.compile(
        r"\b\d{3}-\d{2}-\d{4}\b"
    ),
    "EMAIL": re.compile(
        r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"
    ),
    "MRN": re.compile(
        r"\b(?:MRN|mrn|Medical Record Number)[:\s]*\d{6,10}\b"
    ),
    "ADDRESS": re.compile(
        r"\b\d{1,5}\s[A-Z][a-z]+(?:\s[A-Z][a-z]+)*\s(?:St|Ave|Blvd|Dr|Rd|Ln|Way|Ct)\b"
    ),
}

# ─── 판정 문장 패턴 (제거 대상) ──────────────────────────
JUDGMENT_PATTERNS = [
    re.compile(r"(?i)\b(?:diagnosed?\s+with|final\s+diagnosis|assessment\s*:)", re.IGNORECASE),
    re.compile(r"(?i)\b(?:impression\s*:|conclusion\s*:|determination\s*:)", re.IGNORECASE),
    re.compile(r"(?i)\b(?:confirmed|established)\s+(?:diagnosis|dx)", re.IGNORECASE),
    re.compile(r"(?i)\b(?:판정|확정\s*진단|최종\s*진단|진단\s*결과)", re.IGNORECASE),
]


def mask_phi(text: str) -> str:
    """PHI 항목을 마스킹 토큰으로 대체"""
    masked_text = text
    for phi_type, pattern in PHI_PATTERNS.items():
        masked_text = pattern.sub(f"[{phi_type}]", masked_text)
    return masked_text


def remove_judgment_sentences(text: str) -> str:
    """판정/확정 진단 문장을 제거"""
    sentences = re.split(r"(?<=[.!?。])\s+", text)
    filtered = []
    for sentence in sentences:
        is_judgment = any(
            pattern.search(sentence) for pattern in JUDGMENT_PATTERNS
        )
        if not is_judgment:
            filtered.append(sentence)
    return " ".join(filtered)


def process_note(note: dict) -> dict | None:
    """
    단일 clinical note 처리
    Returns:
        처리된 note dict 또는 None (유효하지 않은 경우)
    """
    text = note.get("text", "")
    if not text or len(text.strip()) < 10:
        return None

    # 1단계: 판정 문장 제거
    cleaned_text = remove_judgment_sentences(text)

    # 2단계: PHI 마스킹
    masked_text = mask_phi(cleaned_text)

    # 마스킹 후 유효한 내용이 없으면 제외
    if len(masked_text.strip()) < 10:
        return None

    return {
        "note_id": note.get("note_id", note.get("id", "")),
        "patient_id": mask_phi(note.get("patient_id", "")),
        "text": masked_text,
        "note_type": note.get("note_type", "unknown"),
        "date": "[DATE]",  # 날짜도 마스킹
        "metadata": {
            "original_length": len(text),
            "processed_length": len(masked_text),
            "sentences_removed": text.count(". ") - masked_text.count(". "),
        },
    }


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
    processed_notes = []
    total_count = 0
    error_count = 0

    for line in body.strip().split("\n"):
        if not line.strip():
            continue
        total_count += 1
        try:
            note = json.loads(line)
            processed = process_note(note)
            if processed:
                processed_notes.append(processed)
        except json.JSONDecodeError as e:
            error_count += 1
            logger.warning(f"JSON 파싱 오류 (line {total_count}): {e}")

    logger.info(
        f"처리 완료: 전체 {total_count}건, "
        f"canonical {len(processed_notes)}건, "
        f"오류 {error_count}건"
    )

    # 결과를 S3 rag/ 경로에 저장
    output_key = f"{RAG_PREFIX}canonical_notes.jsonl"
    output_body = "\n".join(json.dumps(note, ensure_ascii=False) for note in processed_notes)

    try:
        s3_client.put_object(
            Bucket=BUCKET_NAME,
            Key=output_key,
            Body=output_body.encode("utf-8"),
            ContentType="application/jsonl",
            ServerSideEncryption="aws:kms",
        )
        logger.info(f"저장 완료: s3://{BUCKET_NAME}/{output_key}")
    except Exception as e:
        logger.error(f"S3 저장 실패: {e}")
        raise

    # 원본도 raw/에 보존 (이미 있으면 skip)
    result = {
        "status": "success",
        "total_input": total_count,
        "canonical_output": len(processed_notes),
        "errors": error_count,
        "output_key": output_key,
        "bucket": BUCKET_NAME,
    }

    logger.info(f"결과: {json.dumps(result)}")
    return result
