"""
프로젝트 전역 설정 - 환경변수 기반
"""
import os

# ─── S3 설정 ─────────────────────────────────────────────
S3_BUCKET_NAME = os.environ.get("S3_BUCKET_NAME", "aws-healthcare-data")
S3_PREFIX_RAW = "raw/"
S3_PREFIX_RAG = "rag/"
S3_PREFIX_GRAPH = "graph/"
S3_PREFIX_TRIALS = "trials/"

# ─── Neptune 설정 ────────────────────────────────────────
NEPTUNE_ENDPOINT = os.environ.get("NEPTUNE_ENDPOINT", "")
NEPTUNE_PORT = int(os.environ.get("NEPTUNE_PORT", "8182"))

# ─── OpenSearch Serverless 설정 ──────────────────────────
OPENSEARCH_ENDPOINT = os.environ.get("OPENSEARCH_ENDPOINT", "")
OPENSEARCH_INDEX_NAME = os.environ.get("OPENSEARCH_INDEX_NAME", "emr-vectors")

# ─── DynamoDB 설정 ───────────────────────────────────────
DYNAMODB_CRITERIA_TABLE = os.environ.get("DYNAMODB_CRITERIA_TABLE", "CriteriaStore")

# ─── Bedrock 설정 ────────────────────────────────────────
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "anthropic.claude-3-sonnet-20240229-v1:0")
BEDROCK_EMBEDDING_MODEL_ID = os.environ.get("BEDROCK_EMBEDDING_MODEL_ID", "amazon.titan-embed-text-v2:0")
BEDROCK_REGION = os.environ.get("BEDROCK_REGION", "us-east-1")

# ─── Step Functions 설정 ─────────────────────────────────
MAX_RETRY_ATTEMPTS = int(os.environ.get("MAX_RETRY_ATTEMPTS", "3"))
RETRY_INTERVAL_SECONDS = int(os.environ.get("RETRY_INTERVAL_SECONDS", "60"))
RETRY_BACKOFF_RATE = float(os.environ.get("RETRY_BACKOFF_RATE", "2.0"))

# ─── 관측성 설정 ─────────────────────────────────────────
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
SNS_ALERT_TOPIC_ARN = os.environ.get("SNS_ALERT_TOPIC_ARN", "")
