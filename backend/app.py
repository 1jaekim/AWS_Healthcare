#!/usr/bin/env python3
"""
AWS Healthcare Backend - CDK App Entry Point
─────────────────────────────────────────────
`doc/ARCHITECTURE_V2.md` 의 목표 아키텍처를 배포한다.

리전 구성
────────
기본은 서울(ap-northeast-2)이다. Matching API, DynamoDB,
Cognito, Bedrock 을 여기에 둔다. GraphRAG 계층 하나만 us-east-1 에 둔다.
선택이 아니라 제약이다 — Bedrock Knowledge Bases 의 GraphRAG(NEPTUNE_ANALYTICS
스토리지)는 서울에서 제공되지 않는다. 지원 리전은 프랑크푸르트·런던·아일랜드·
오리건·버지니아·도쿄·싱가포르뿐이다.

리전이 갈리면 CloudFormation 의 Export/ImportValue 로 스택을 엮을 수 없다.
그래서 배포가 두 단계다. GraphRAG 를 먼저 올리고, 그 출력값을 CDK 컨텍스트로
넘겨 서울 스택들을 올린다.

배포 순서
────────
    # 1단계 — 선택적 GraphRAG (us-east-1)
    cdk deploy HealthcareGraphRagStack

    # 2단계 — 나머지 (서울). 1단계 출력값을 컨텍스트로 넘긴다
    cdk deploy --all \\
      -c knowledge_base_id=<KnowledgeBaseId> \\
      -c data_source_id=<DataSourceId> \\
      -c graphrag_bucket=<GraphRagBucketName> \\
      -c frontend_origin=<FrontendUrl>

컨텍스트 값을 생략하면 GraphRAG 없이도 배포된다. API 는 로컬 키워드 검색으로
내려앉고 파이프라인의 색인 단계만 비어 있게 된다.

`frontend_origin` 은 FrontendStack 을 한 번 올린 뒤에야 알 수 있다(CloudFront
도메인). API 와 프론트가 서로를 참조하는 순환이라, 인프라를 먼저 올리고 값을
채워 다시 올리는 식으로 끊는다.
"""
import os

import aws_cdk as cdk

from infra.api_stack import ApiStack
from infra.a2a_stack import A2AStack
from infra.auth_stack import AuthStack
from infra.frontend_stack import FrontendStack
from infra.graphrag_stack import GraphRagStack
from infra.guardrail_stack import GuardrailStack
from infra.main_stack import MainStack
from infra.observability_stack import ObservabilityStack
from infra.s3_stack import S3DataStack

app = cdk.App()

account = os.environ.get("CDK_DEFAULT_ACCOUNT")

PRIMARY_REGION = (
    app.node.try_get_context("primary_region")
    or os.environ.get("CDK_DEFAULT_REGION")
    or os.environ.get("AWS_REGION")
    or "ap-northeast-2"
)
"""서비스 본체가 사는 리전. `backend/api/app/config.py` 의 DEFAULT_REGION 과 같아야 한다."""

GRAPHRAG_REGION = app.node.try_get_context("graphrag_region") or "us-east-1"
"""GraphRAG 전용 리전. 버지니아다. 운영 KB 는 VZIL9VWWKP 이며 공개 공고·표준문서
검색용이다. 환자 개인 근거 저장소가 아니므로 검색에 환자 가명 키 필터가 필요없다.

서울에서 Bedrock KB 가 Neptune Analytics 를 받게 되면 이 값을 PRIMARY_REGION 으로
되돌리고, 그때 `KNOWLEDGE_BASE_REGION`·`S3_RAG_BUCKET_NAME` 환경 변수도 함께
걷어내면 된다.
"""

env = cdk.Environment(account=account, region=PRIMARY_REGION)
graphrag_env = cdk.Environment(account=account, region=GRAPHRAG_REGION)

alert_email = app.node.try_get_context("alert_email") or os.environ.get("ALERT_EMAIL", "")

# 1단계 배포 결과를 받는 자리. 비어 있으면 GraphRAG 없이 구성된다.
knowledge_base_id = app.node.try_get_context("knowledge_base_id") or ""
data_source_id = app.node.try_get_context("data_source_id") or ""
graphrag_bucket = app.node.try_get_context("graphrag_bucket") or ""

# FrontendStack 을 올린 뒤 CloudFront 도메인을 넣는다. 비어 있으면 API 가
# 로컬 vite 주소만 허용한다(`app/config.py` 의 DEFAULT_CORS_ORIGINS).
frontend_origin = app.node.try_get_context("frontend_origin") or ""


# ─── GraphRAG (us-east-1) ────────────────────────────────
# 그래프·KB·소스 버킷을 한 스택에 묶었다. KB 의 데이터소스 버킷은 KB 와 같은
# 리전이어야 해서 셋을 쪼갤 수 없다.
graphrag_stack = GraphRagStack(
    app,
    "HealthcareGraphRagStack",
    writer_account_id=account or "",
    env=graphrag_env,
)

# ─── Stack 1: S3 데이터 레이크 (서울) ────────────────────
s3_stack = S3DataStack(app, "HealthcareS3Stack", env=env)

# ─── Stack 2: 관측성 ────────────────────────────────────
observability_stack = ObservabilityStack(
    app,
    "HealthcareObservabilityStack",
    alert_email=alert_email,
    env=env,
)

# ─── Stack 3: 메인 (Lambda, DynamoDB, Step Functions) ───
main_stack = MainStack(
    app,
    "HealthcareMainStack",
    s3_stack=s3_stack,
    observability_stack=observability_stack,
    graphrag_region=GRAPHRAG_REGION,
    knowledge_base_id=knowledge_base_id,
    data_source_id=data_source_id,
    graphrag_bucket=graphrag_bucket,
    env=env,
)
main_stack.add_dependency(s3_stack)
main_stack.add_dependency(observability_stack)

# ─── Stack 4: 사용자 인증 (Cognito) ─────────────────────
auth_stack = AuthStack(app, "HealthcareAuthStack", env=env)

# ─── Stack 5: 안전장치 (Bedrock Guardrails) ─────────────
guardrail_stack = GuardrailStack(app, "HealthcareGuardrailStack", env=env)
guardrail_id = app.node.try_get_context("guardrail_id") or guardrail_stack.guardrail_id
guardrail_version = (
    app.node.try_get_context("guardrail_version") or guardrail_stack.version
)

# ─── Stack 6: 프론트엔드 정적 호스팅 ────────────────────
# 아무것에도 의존하지 않는다. API 주소는 빌드 시점에 번들에 구워지므로
# 인프라 차원의 연결이 없다.
frontend_stack = FrontendStack(app, "HealthcareFrontendStack", env=env)

# A2A is enabled in the deployed service. Reviewer and challenger run as two
# independent Seoul Lambda runtimes and the API receives their IAM Function
# URLs through stack references. It can still be disabled explicitly with
# ``-c a2a_enabled=false`` for latency comparisons or incident isolation.
a2a_enabled_value = app.node.try_get_context("a2a_enabled")
a2a_enabled = str(a2a_enabled_value or "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
a2a_asset_exists = os.path.isdir(
    os.path.join(os.path.dirname(__file__), "build", "a2a_lambda")
)
api_asset_exists = os.path.isdir(
    os.path.join(os.path.dirname(__file__), "build", "api_lambda")
)
if a2a_enabled and not a2a_asset_exists:
    raise RuntimeError(
        "A2A is enabled but its asset is missing. Run "
        "scripts/build_a2a_asset.sh before synth/deploy."
    )

a2a_stack = None
if a2a_enabled:
    a2a_stack = A2AStack(
        app,
        "HealthcareA2AStack",
        guardrail_id=guardrail_id,
        guardrail_version=guardrail_version,
        env=env,
    )
    a2a_stack.add_dependency(guardrail_stack)

# ─── Stack 7: Matching API (Lambda + Function URL) ──────
# 패키지가 있을 때만 만든다. `backend/scripts/build_api_asset.sh` 를 돌리지 않은
# 상태에서 `cdk synth` 만 해보는 경우가 있어서, 없으면 조용히 건너뛴다.
if api_asset_exists:
    api_stack = ApiStack(
        app,
        "HealthcareApiStack",
        criteria_table=main_stack.criteria_table,
        intake_table=main_stack.intake_table,
        user_pool_id=auth_stack.user_pool.user_pool_id,
        user_pool_client_id=auth_stack.user_pool_client.user_pool_client_id,
        knowledge_base_id=knowledge_base_id,
        knowledge_base_region=GRAPHRAG_REGION,
        guardrail_id=guardrail_id,
        guardrail_version=guardrail_version,
        cors_origins=frontend_origin,
        a2a_reviewer_url=a2a_stack.reviewer_url.url if a2a_stack else "",
        a2a_challenger_url=a2a_stack.challenger_url.url if a2a_stack else "",
        env=env,
    )
    api_stack.add_dependency(main_stack)
    api_stack.add_dependency(auth_stack)
    api_stack.add_dependency(guardrail_stack)
    if a2a_stack is not None:
        a2a_stack.grant_invoke(api_stack.function)
        api_stack.add_dependency(a2a_stack)

app.synth()
