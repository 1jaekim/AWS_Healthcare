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
import re
from typing import Any

import boto3

try:
    from .reference_documents import build_trial_notice_document
except ImportError:
    from reference_documents import build_trial_notice_document

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

AWS_REGION = os.environ.get("AWS_REGION", "ap-northeast-2")

s3_client = boto3.client("s3", region_name=AWS_REGION)
bedrock_client = boto3.client("bedrock-runtime", region_name=AWS_REGION)
dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
textract_client = boto3.client("textract", region_name=AWS_REGION)

# ─── 환경변수 ────────────────────────────────────────────
BUCKET_NAME = os.environ.get("S3_BUCKET_NAME", "aws-healthcare-data")
CRITERIA_TABLE = os.environ.get("DYNAMODB_CRITERIA_TABLE", "CriteriaStore")
BEDROCK_MODEL_ID = os.environ.get(
    "BEDROCK_MODEL_ID",
    "global.anthropic.claude-sonnet-4-5-20250929-v1:0",
)
RAG_BUCKET_NAME = os.environ.get("S3_RAG_BUCKET_NAME", "")
RAG_REGION = os.environ.get("S3_RAG_REGION", "us-east-1")

NEEDS_FIX_STATUS = "NEEDS_FIX"
MIN_DOCUMENT_CHARS = int(os.environ.get("MIN_DOCUMENT_CHARS", "50"))
MIN_EXPECTED_HANGUL_RATIO = float(
    os.environ.get("MIN_EXPECTED_HANGUL_RATIO", "0.05")
)
SCREENSHOT_PATH_SEGMENT = "/screenshots/"

rag_s3_client = boto3.client("s3", region_name=RAG_REGION)

table = dynamodb.Table(CRITERIA_TABLE)


def is_screenshot_source(source_key: str) -> bool:
    """감사용 스크린샷은 기준 추출과 RAG 색인 입력에서 제외한다."""
    normalized = f"/{str(source_key or '').replace(chr(92), '/').lower().lstrip('/')}"
    return SCREENSHOT_PATH_SEGMENT in normalized


def stable_source_trial_id(source_key: str) -> str:
    """파싱 전 실패도 CriteriaStore에 기록할 수 있는 안정 ID를 만든다."""
    source_digest = hashlib.sha256(source_key.encode("utf-8")).hexdigest()[:16]
    return f"SRC-{source_digest}"


def inspect_document_quality(document_text: str, source_key: str) -> dict[str, Any]:
    """짧은 문서와 한국어 OCR 오인식을 Bedrock 호출 전에 차단한다."""
    stripped = document_text.strip()
    hangul_count = len(re.findall(r"[\u3131-\u318e\uac00-\ud7a3]", stripped))
    letter_count = sum(character.isalpha() for character in stripped)
    hangul_ratio = hangul_count / max(letter_count, 1)
    expects_korean = "kct_" in source_key.lower()
    issues: list[str] = []
    if len(stripped) < MIN_DOCUMENT_CHARS:
        issues.append("TEXT_TOO_SHORT")
    if (
        expects_korean
        and len(stripped) >= MIN_DOCUMENT_CHARS
        and hangul_ratio < MIN_EXPECTED_HANGUL_RATIO
    ):
        issues.append("KOREAN_OCR_UNREADABLE")
    return {
        "passed": not issues,
        "issues": issues,
        "document_chars": len(stripped),
        "hangul_chars": hangul_count,
        "hangul_ratio": round(hangul_ratio, 4),
        "expects_korean": expects_korean,
    }


def inspect_parsed_quality(
    trial_data: dict[str, Any], document_quality: dict[str, Any]
) -> dict[str, Any]:
    """구조화 결과가 검토 큐에 올릴 최소 품질인지 확인한다."""
    report = dict(document_quality)
    issues = list(report.get("issues", []))
    inclusion = trial_data.get("inclusion_criteria", [])
    exclusion = trial_data.get("exclusion_criteria", [])
    inclusion_count = len(inclusion) if isinstance(inclusion, list) else 0
    exclusion_count = len(exclusion) if isinstance(exclusion, list) else 0
    title = str(trial_data.get("trial_title") or "").strip()
    if inclusion_count + exclusion_count == 0:
        issues.append("NO_ELIGIBILITY_CRITERIA")
    if not title or title.upper() == "UNKNOWN":
        issues.append("MISSING_TRIAL_TITLE")
    report.update(
        passed=not issues,
        issues=issues,
        inclusion_count=inclusion_count,
        exclusion_count=exclusion_count,
    )
    return report

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


def save_to_dynamodb(
    trial_data: dict,
    source_key: str,
    *,
    status: str = "pending_review",
    failure_reason: str = "",
    quality_report: dict[str, Any] | None = None,
) -> str:
    """
    구조화된 기준을 DynamoDB에 저장
    정상 결과는 pending_review, 품질 실패는 NEEDS_FIX로 저장한다.
    """
    import time

    trial_id = trial_data.get("trial_id", "UNKNOWN")
    item = {
        "trial_id": trial_id,
        "source_key": source_key,
        "status": status,
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
    if failure_reason:
        item["failure_reason"] = failure_reason
    if quality_report is not None:
        # DynamoDB는 Python float을 허용하지 않으므로 JSON 문자열로 보존한다.
        item["quality_report"] = json.dumps(quality_report, ensure_ascii=False)

    table.put_item(Item=item)
    logger.info("DynamoDB 저장: trial_id=%s, status=%s", trial_id, status)
    return trial_id


def record_needs_fix(
    *,
    source_key: str,
    failure_reason: str,
    quality_report: dict[str, Any],
    trial_data: dict[str, Any] | None = None,
) -> str:
    """내용 품질 실패를 재처리 가능한 상태로 영속화한다."""
    failed_data = dict(trial_data or {})
    candidate_id = str(failed_data.get("trial_id") or "").strip()
    if not candidate_id or candidate_id.upper() == "UNKNOWN":
        failed_data["trial_id"] = stable_source_trial_id(source_key)
    failed_data.setdefault("trial_title", "")
    failed_data.setdefault("inclusion_criteria", [])
    failed_data.setdefault("exclusion_criteria", [])
    return save_to_dynamodb(
        failed_data,
        source_key,
        status=NEEDS_FIX_STATUS,
        failure_reason=failure_reason,
        quality_report=quality_report,
    )


def parser_result(
    *,
    status: str,
    source_key: str,
    trial_id: str,
    trial_data: dict[str, Any] | None = None,
    quality_report: dict[str, Any] | None = None,
    failure_reason: str = "",
    rag_keys: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """모든 분기에서 Step Functions가 읽을 수 있는 동일 응답 계약을 만든다."""
    data = trial_data or {}
    inclusion = data.get("inclusion_criteria", [])
    exclusion = data.get("exclusion_criteria", [])
    return {
        "status": status,
        "trial_id": trial_id,
        "trial_title": data.get("trial_title", ""),
        "inclusion_count": len(inclusion) if isinstance(inclusion, list) else 0,
        "exclusion_count": len(exclusion) if isinstance(exclusion, list) else 0,
        "review_status": status if status != "success" else "pending_review",
        "source_key": source_key,
        "failure_reason": failure_reason,
        "quality_issues": list((quality_report or {}).get("issues", [])),
        "rag_document_key": rag_keys[0] if rag_keys else None,
        "rag_metadata_key": rag_keys[1] if rag_keys else None,
    }


def publish_reference_document(
    trial_data: dict[str, Any], *, source_key: str, document_text: str
) -> tuple[str, str] | None:
    """공개 공고와 sidecar를 GraphRAG 소스 버킷에 저장한다."""
    if not RAG_BUCKET_NAME:
        logger.info("S3_RAG_BUCKET_NAME이 없어 공고 RAG 발행을 건너뜁니다")
        return None
    document = build_trial_notice_document(
        trial_data=trial_data,
        source_key=source_key,
        source_text=document_text,
    )
    metadata_key = f"{document.key}.metadata.json"
    rag_s3_client.put_object(
        Bucket=RAG_BUCKET_NAME,
        Key=document.key,
        Body=document.body.encode("utf-8"),
        ContentType="text/markdown; charset=utf-8",
        ServerSideEncryption="aws:kms",
    )
    rag_s3_client.put_object(
        Bucket=RAG_BUCKET_NAME,
        Key=metadata_key,
        Body=json.dumps(document.metadata, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json",
        ServerSideEncryption="aws:kms",
    )
    return document.key, metadata_key


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

    if is_screenshot_source(source_key):
        logger.info("스크린샷 감사 원본은 기준 추출에서 제외합니다: %s", source_key)
        return parser_result(
            status="skipped",
            source_key=source_key,
            trial_id=stable_source_trial_id(source_key),
            failure_reason="SCREENSHOT_EXCLUDED",
            quality_report={"passed": False, "issues": ["SCREENSHOT_EXCLUDED"]},
        )

    # 1. 텍스트 추출
    try:
        document_text = extract_text_from_document(bucket, source_key)
        logger.info(f"텍스트 추출 완료: {len(document_text)} chars")
    except Exception as e:
        logger.error(f"텍스트 추출 실패: {e}")
        quality_report = {
            "passed": False,
            "issues": ["TEXT_EXTRACTION_FAILED"],
            "error_type": type(e).__name__,
        }
        trial_id = record_needs_fix(
            source_key=source_key,
            failure_reason="TEXT_EXTRACTION_FAILED",
            quality_report=quality_report,
        )
        return parser_result(
            status=NEEDS_FIX_STATUS,
            source_key=source_key,
            trial_id=trial_id,
            failure_reason="TEXT_EXTRACTION_FAILED",
            quality_report=quality_report,
        )

    document_quality = inspect_document_quality(document_text, source_key)
    if not document_quality["passed"]:
        failure_reason = ",".join(document_quality["issues"])
        trial_id = record_needs_fix(
            source_key=source_key,
            failure_reason=failure_reason,
            quality_report=document_quality,
        )
        return parser_result(
            status=NEEDS_FIX_STATUS,
            source_key=source_key,
            trial_id=trial_id,
            failure_reason=failure_reason,
            quality_report=document_quality,
        )

    # 2. Bedrock으로 구조화 파싱
    try:
        trial_data = parse_with_bedrock(document_text)
        logger.info(
            f"구조화 완료: 선정 {len(trial_data.get('inclusion_criteria', []))}건, "
            f"제외 {len(trial_data.get('exclusion_criteria', []))}건"
        )
    except Exception as e:
        logger.error(f"Bedrock 파싱 실패: {e}")
        quality_report = {
            **document_quality,
            "passed": False,
            "issues": [*document_quality["issues"], "BEDROCK_PARSE_FAILED"],
            "error_type": type(e).__name__,
        }
        trial_id = record_needs_fix(
            source_key=source_key,
            failure_reason="BEDROCK_PARSE_FAILED",
            quality_report=quality_report,
        )
        return parser_result(
            status=NEEDS_FIX_STATUS,
            source_key=source_key,
            trial_id=trial_id,
            failure_reason="BEDROCK_PARSE_FAILED",
            quality_report=quality_report,
        )

    # 공개 모집 공고에는 NCT 번호가 없는 경우가 많다. UNKNOWN 하나로 덮어쓰지
    # 않도록 S3 source key의 안정적인 해시를 식별자로 사용한다.
    parsed_trial_id = str(trial_data.get("trial_id") or "").strip()
    if not parsed_trial_id or parsed_trial_id.upper() == "UNKNOWN":
        try:
            metadata = s3_client.head_object(Bucket=bucket, Key=source_key).get(
                "Metadata", {}
            )
        except Exception as exc:  # noqa: BLE001 - 메타데이터는 선택 사항이다.
            logger.warning("S3 canonical trial metadata lookup failed: %s", exc)
            metadata = {}
        canonical = str(metadata.get("canonical-trial-id") or "").strip()
        trial_data["trial_id"] = canonical or stable_source_trial_id(source_key)

    parsed_quality = inspect_parsed_quality(trial_data, document_quality)
    if not parsed_quality["passed"]:
        failure_reason = ",".join(parsed_quality["issues"])
        trial_id = record_needs_fix(
            source_key=source_key,
            failure_reason=failure_reason,
            quality_report=parsed_quality,
            trial_data=trial_data,
        )
        return parser_result(
            status=NEEDS_FIX_STATUS,
            source_key=source_key,
            trial_id=trial_id,
            trial_data=trial_data,
            failure_reason=failure_reason,
            quality_report=parsed_quality,
        )

    # 3. DynamoDB 저장
    try:
        trial_id = save_to_dynamodb(
            trial_data, source_key, quality_report=parsed_quality
        )
    except Exception as e:
        logger.error(f"DynamoDB 저장 실패: {e}")
        raise

    rag_keys = publish_reference_document(
        trial_data, source_key=source_key, document_text=document_text
    )

    result = parser_result(
        status="success",
        source_key=source_key,
        trial_id=trial_id,
        trial_data=trial_data,
        quality_report=parsed_quality,
        rag_keys=rag_keys,
    )

    logger.info(f"Protocol Parser 완료: {json.dumps(result, ensure_ascii=False)}")
    return result
