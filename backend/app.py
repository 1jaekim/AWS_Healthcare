#!/usr/bin/env python3
"""
AWS Healthcare Backend - CDK App Entry Point
─────────────────────────────────────────────
cdk deploy --all 로 전체 인프라 배포
"""
import os

import aws_cdk as cdk

from infra.s3_stack import S3DataStack
from infra.bedrock_stack import BedrockKnowledgeBaseStack
from infra.observability_stack import ObservabilityStack
from infra.main_stack import MainStack

app = cdk.App()

# 환경 설정
env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
)

# 알림 이메일 (cdk.json 또는 환경변수에서)
alert_email = app.node.try_get_context("alert_email") or os.environ.get("ALERT_EMAIL", "")

# ─── Stack 1: S3 데이터 레이크 ───────────────────────────
s3_stack = S3DataStack(app, "HealthcareS3Stack", env=env)

# ─── Stack 2: Bedrock Knowledge Bases GraphRAG ───────────
bedrock_stack = BedrockKnowledgeBaseStack(
    app,
    "HealthcareBedrockStack",
    data_bucket_arn=s3_stack.data_bucket.bucket_arn,
    data_key_arn=s3_stack.data_key.key_arn,
    env=env,
)
bedrock_stack.add_dependency(s3_stack)

# ─── Stack 3: 관측성 ────────────────────────────────────
observability_stack = ObservabilityStack(
    app,
    "HealthcareObservabilityStack",
    alert_email=alert_email,
    env=env,
)

# ─── Stack 4: 메인 (Lambda, DynamoDB, Step Functions) ───
main_stack = MainStack(
    app,
    "HealthcareMainStack",
    s3_stack=s3_stack,
    bedrock_stack=bedrock_stack,
    observability_stack=observability_stack,
    env=env,
)
main_stack.add_dependency(s3_stack)
main_stack.add_dependency(bedrock_stack)
main_stack.add_dependency(observability_stack)

app.synth()
