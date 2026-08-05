"""
공고 구조화 파이프라인 - Bedrock Protocol Parser Lambda
────────────────────────────────────────────────────────
임상시험 공고 PDF(자연어) → 선정·제외 기준 JSON → DynamoDB Criteria Store

처리 흐름:
  1. S3 trials/ 경로에서 공고 PDF 텍스트 추출
  2. Bedrock (Claude) 호출 → 선정/제외 기준을 구조화 JSON으로 변환
  3. 연구자 기준 검토를 위해 DynamoDB에 저장 (status: pending_review)
  4. 검토 완료 후 status: approved 로 업데이트

트리거: EventBridge → Step Functions → 이 Lambda
"""
import json
import hashlib
import logging
import os
from typing import Any

import boto3

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

s3_client = boto3.client("s3")
bedrock_client = boto3.client("bedrock-runtime")
dynamodb = boto3.resource("dynamodb")
textract_client = boto3.client("textract")

# ─── 환경변수 ────────────────────────────────────────────
BUCKET_NAME = os.environ.get("S3_BUCKET_NAME", "aws-healthcare-data")
CRITERIA_TABLE = os.environ.get("DYNAMODB_CRITERIA_TABLE", "CriteriaStore")
BEDROCK_MODEL_ID = os.environ.get(
    "BEDROCK_MODEL_ID",
    "global.anthropic.claude-sonnet-4-5-20250929-v1:0",
)
AWS_REGION = os.environ.get("AWS_REGION", "ap-northeast-2")

table = dynamodb.Table(CRITERIA_TABLE)

# ─── Bedrock 프롬프트 ────────────────────────────────────
PARSING_PROMPT = """당신은 임상시험 프로토콜 분석 전문가입니다.
아래 임상시험 공고 텍스트에서 선정 기준(Inclusion Criteria)과 제외 기준(Exclusion Criteria)을 구조화된 JSON으로 추출하세요. 탭별 본문에 있는 조건을 하나도 요약하거나 합치지 말고 조건 문장마다 배열 항목 하나로 보존하세요.

## 출력 형식 (JSON)
{{
  "trial_id": "공고에서 추출한 임상시험 ID (NCT 번호 등)",
  "trial_title": "임상시험 제목",
  "phase": "Phase 1/2/3/4 또는 N/A",
  "condition": "대상 질환",
  "intervention": "시험 중재/약물",
  "inclusion_criteria": [
    {{
      "id": "INC-001",
      "category": "demographics|diagnosis|lab_values|medical_history|consent|other",
      "description": "기준 설명 (영문 원문이면 정확한 한국어 번역)",
      "structured": {{
        "parameter": "측정 항목 (예: age, HbA1c, eGFR)",
        "operator": ">=|<=|==|between|in|not_in",
        "value": "기준값",
        "unit": "단위 (해당시)"
      }}
    }}
  ],
  "exclusion_criteria": [
    {{
      "id": "EXC-001",
      "category": "demographics|diagnosis|lab_values|medical_history|medication|allergy|other",
      "description": "기준 설명 (영문 원문이면 정확한 한국어 번역)",
      "structured": {{
        "parameter": "측정 항목",
        "operator": ">=|<=|==|has|not_has",
        "value": "기준값",
        "unit": "단위 (해당시)"
      }}
    }}
  ]
}}

## 규칙
1. 원문에 명시된 기준만 추출 (추론하지 마세요)
2. 수치가 있는 기준은 반드시 structured 필드에 파라미터/연산자/값을 분리
3. 수치가 없는 정성적 기준은 structured를 null로 설정
4. trial_id가 명시되지 않으면 "UNKNOWN"으로 설정
5. JSON만 출력하세요 (추가 설명 없이)
6. 질환·단계·기준 설명은 한국어로 쓰되 약물명·고유 시험명은 원문을 병기하세요
7. structured가 null인 정성 조건도 절대 버리지 말고 description과 함께 포함하세요

## 임상시험 공고 텍스트
{document_text}
"""


def extract_text_from_document(bucket: str, key: str) -> str:
    """
    S3의 PDF에서 텍스트 추출
    Textract 사용 (동기 호출, 단일 페이지) 또는 S3 Select (텍스트)
    """
    suffix = key.lower().rsplit(".", 1)[-1] if "." in key else ""
    # Playwright 스크린샷과 기존 PDF는 바이너리를 직접 디코딩하지 않고
    # Textract OCR을 거친다.
    if suffix in {"pdf", "png", "jpg", "jpeg", "tif", "tiff"}:
        try:
            response = textract_client.detect_document_text(
                Document={
                    "S3Object": {
                        "Bucket": bucket,
                        "Name": key,
                    }
                }
            )
            blocks = response.get("Blocks", [])
            lines = [
                block["Text"]
                for block in blocks
                if block["BlockType"] == "LINE"
            ]
            return "\n".join(lines)
        except Exception as e:
            logger.error(f"Textract OCR 실패: {e}")
            raise

    if suffix not in {"txt", "md", "json"}:
        raise ValueError(f"지원하지 않는 공고 파일 형식입니다: {suffix or 'unknown'}")

    # 텍스트 파일인 경우 직접 읽기
    response = s3_client.get_object(Bucket=bucket, Key=key)
    return response["Body"].read().decode("utf-8", errors="ignore")


def parse_with_bedrock(document_text: str) -> dict:
    """
    Bedrock Claude를 사용하여 공고 텍스트를 구조화 JSON으로 변환
    """
    prompt = PARSING_PROMPT.format(document_text=document_text[:50000])  # 토큰 제한

    request_body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 4096,
        "temperature": 0.0,
        "messages": [
            {
                "role": "user",
                "content": prompt,
            }
        ],
    }

    response = bedrock_client.invoke_model(
        modelId=BEDROCK_MODEL_ID,
        body=json.dumps(request_body),
        contentType="application/json",
        accept="application/json",
    )

    response_body = json.loads(response["body"].read())
    assistant_text = response_body["content"][0]["text"]

    # JSON 파싱 (코드블록 제거)
    json_text = assistant_text.strip()
    if json_text.startswith("```"):
        json_text = json_text.split("\n", 1)[1]
        json_text = json_text.rsplit("```", 1)[0]

    try:
        parsed = json.loads(json_text)
        return parsed
    except json.JSONDecodeError as e:
        logger.error(f"Bedrock 응답 JSON 파싱 실패: {e}")
        logger.error(f"응답 텍스트: {assistant_text[:500]}")
        raise ValueError(f"구조화 파싱 실패: {e}")


def save_to_dynamodb(trial_data: dict, source_key: str) -> str:
    """
    구조화된 기준을 DynamoDB에 저장
    status: pending_review (연구자 검토 대기)
    """
    import time

    trial_id = trial_data.get("trial_id", "UNKNOWN")
    item = {
        "trial_id": trial_id,
        "source_key": source_key,
        "status": "pending_review",
        "trial_title": trial_data.get("trial_title", ""),
        "phase": trial_data.get("phase", ""),
        "condition": trial_data.get("condition", ""),
        "intervention": trial_data.get("intervention", ""),
        "inclusion_criteria": json.dumps(
            trial_data.get("inclusion_criteria", []), ensure_ascii=False
        ),
        "exclusion_criteria": json.dumps(
            trial_data.get("exclusion_criteria", []), ensure_ascii=False
        ),
        "inclusion_count": len(trial_data.get("inclusion_criteria", [])),
        "exclusion_count": len(trial_data.get("exclusion_criteria", [])),
        "created_at": int(time.time()),
        "updated_at": int(time.time()),
    }

    table.put_item(Item=item)
    logger.info(f"DynamoDB 저장: trial_id={trial_id}, status=pending_review")
    return trial_id


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """
    Lambda 핸들러
    
    event 형식 (Step Functions에서 호출):
        {
            "source_key": "trials/NCT12345678.pdf",
            "bucket": "aws-healthcare-data"  (optional)
        }
    
    또는 EventBridge 이벤트:
        {
            "detail": {
                "bucket": {"name": "..."},
                "object": {"key": "trials/..."}
            }
        }
    """
    logger.info(f"Protocol Parser Lambda 시작")

    # 소스 키 결정
    bucket = BUCKET_NAME
    source_key = None

    if "detail" in event:
        # EventBridge 이벤트
        detail = event["detail"]
        bucket = detail.get("bucket", {}).get("name", BUCKET_NAME)
        source_key = detail.get("object", {}).get("key")
    else:
        # 직접 호출 / Step Functions
        bucket = event.get("bucket", BUCKET_NAME)
        source_key = event.get("source_key")

    if not source_key:
        return {"status": "error", "message": "source_key가 필요합니다"}

    logger.info(f"처리 대상: s3://{bucket}/{source_key}")

    # 1. 텍스트 추출
    try:
        document_text = extract_text_from_document(bucket, source_key)
        logger.info(f"텍스트 추출 완료: {len(document_text)} chars")
    except Exception as e:
        logger.error(f"텍스트 추출 실패: {e}")
        raise

    if len(document_text.strip()) < 50:
        return {
            "status": "error",
            "message": "문서에서 유효한 텍스트를 추출할 수 없습니다",
            "source_key": source_key,
        }

    # 2. Bedrock으로 구조화 파싱
    try:
        trial_data = parse_with_bedrock(document_text)
        logger.info(
            f"구조화 완료: 선정 {len(trial_data.get('inclusion_criteria', []))}건, "
            f"제외 {len(trial_data.get('exclusion_criteria', []))}건"
        )
    except Exception as e:
        logger.error(f"Bedrock 파싱 실패: {e}")
        raise

    # 공개 모집 공고에는 NCT 번호가 없는 경우가 많다. UNKNOWN 하나로 덮어쓰지
    # 않도록 S3 source key의 안정적인 해시를 식별자로 사용한다.
    parsed_trial_id = str(trial_data.get("trial_id") or "").strip()
    if not parsed_trial_id or parsed_trial_id.upper() == "UNKNOWN":
        metadata = s3_client.head_object(Bucket=bucket, Key=source_key).get(
            "Metadata", {}
        )
        canonical = str(metadata.get("canonical-trial-id") or "").strip()
        source_digest = hashlib.sha256(source_key.encode("utf-8")).hexdigest()[:16]
        trial_data["trial_id"] = canonical or f"SRC-{source_digest}"

    # 3. DynamoDB 저장
    try:
        trial_id = save_to_dynamodb(trial_data, source_key)
    except Exception as e:
        logger.error(f"DynamoDB 저장 실패: {e}")
        raise

    result = {
        "status": "success",
        "trial_id": trial_id,
        "trial_title": trial_data.get("trial_title", ""),
        "inclusion_count": len(trial_data.get("inclusion_criteria", [])),
        "exclusion_count": len(trial_data.get("exclusion_criteria", [])),
        "review_status": "pending_review",
        "source_key": source_key,
    }

    logger.info(f"Protocol Parser 완료: {json.dumps(result, ensure_ascii=False)}")
    return result
