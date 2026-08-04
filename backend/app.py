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
from infra.auth_stack import AuthStack

app = cdk.App()

# 환경 설정
# 기본 리전은 서울이다. Bedrock Knowledge Bases 와 Neptune Analytics 를 이 리전에
# 만들고, API 도 같은 리전을 본다(backend/api/app/config.py 의 DEFAULT_REGION).
# 두 값이 어긋나면 배포는 되지만 런타임에 KB 를 찾지 못한다.
env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=(
        os.environ.get("CDK_DEFAULT_REGION")
        or os.environ.get("AWS_REGION")
        or "ap-northeast-2"
    ),
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

# ─── Stack 5: 사용자 인증 (Cognito) ─────────────────────
# 프론트엔드 로그인·회원가입이 이 User Pool 에 붙는다. 다른 스택에 의존하지
# 않으므로 이것만 따로 배포해도 된다:
#   cdk deploy HealthcareAuthStack
auth_stack = AuthStack(app, "HealthcareAuthStack", env=env)

app.synth()
