"""Matching API CDK Stack — FastAPI on Lambda + Function URL.

아키텍처 v2 의 `API Gateway 또는 FastAPI on ECS/Lambda` 자리다. Lambda 를 고른
이유는 이 서비스의 트래픽 모양 때문이다. 매칭 실행은 사람이 화면에서 누를 때만
일어나고 그 사이는 완전히 비어 있다. 상시 기동하는 Fargate 는 그 빈 시간에도
과금되지만 Lambda 는 0이다.

Function URL 을 쓰고 API Gateway 를 두지 않는다. 게이트웨이가 해줄 일(인증,
CORS, 라우팅)을 이 앱이 이미 자기 안에서 하고 있어서다 — 토큰 검증은
`app/auth/`, CORS 는 FastAPI 미들웨어, 라우팅은 FastAPI. 앞에 하나 더 두면 같은
규칙이 두 곳에 생기고, 두 곳이 어긋나면 디버깅이 어려워진다.

Function URL 의 AuthType 은 NONE 이다. IAM 이 아니라는 뜻이지 무인증이라는
뜻이 아니다. 브라우저가 직접 부르는 엔드포인트라 SigV4 서명을 할 수 없고,
실제 인증은 Cognito ID 토큰을 앱이 검증해서 한다. 그래서 `AUTH_REQUIRED=true`
가 이 스택에서 반드시 켜져 있어야 한다. 꺼져 있으면 공개 엔드포인트가 된다.

패키지는 `backend/scripts/build_api_asset.sh` 가 만든다. Docker 없이 linux 휠을
직접 받는 방식이라 로컬에 Docker 데몬이 없어도 배포된다.
"""

from pathlib import Path

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_logs as logs,
)
from constructs import Construct


ASSET_DIR = Path(__file__).resolve().parents[1] / "build" / "api_lambda"
"""build_api_asset.sh 의 출력 디렉터리."""


class ApiStack(Stack):
    """스크리닝 API Lambda 와 공개 진입점."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        criteria_table,
        pseudonym_secret,
        user_pool_id: str,
        user_pool_client_id: str,
        knowledge_base_id: str = "",
        knowledge_base_region: str = "",
        guardrail_id: str = "",
        guardrail_version: str = "",
        cors_origins: str = "",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        if not ASSET_DIR.exists():
            raise FileNotFoundError(
                f"API Lambda 패키지가 없습니다: {ASSET_DIR}\n"
                "먼저 backend/scripts/build_api_asset.sh 를 실행하세요."
            )

        environment = {
            # ─── 모델 ────────────────────────────────────
            # 로컬 기본값은 비활성이지만 배포본은 켠다. 스텁으로 조용히 도는
            # 배포는 '되는 것처럼 보이는' 최악의 상태다.
            "BEDROCK_ENABLED": "true",
            "BEDROCK_MODEL_ID": "global.anthropic.claude-sonnet-4-5-20250929-v1:0",
            "BEDROCK_MAX_TOKENS": "1536",
            "AGENT_MAX_ITERATIONS": "4",
            # AWS_REGION 은 넣지 않는다. Lambda 예약 환경 변수라 설정하면 배포가
            # 거부되고, 런타임이 이미 실행 리전으로 채워준다
            # (`app/config.py` 의 resolve_region() 이 그것을 읽는다).
            # ─── 인증 ────────────────────────────────────
            "COGNITO_USER_POOL_ID": user_pool_id,
            "COGNITO_CLIENT_ID": user_pool_client_id,
            "COGNITO_ADMIN_GROUP": "admin",
            # 설정 누락이 무인증 배포가 되지 않게 하는 장치. Function URL 이
            # AuthType NONE 이라 이 값이 유일한 방어선이다.
            "AUTH_REQUIRED": "true",
            # ─── GraphRAG ────────────────────────────────
            "KNOWLEDGE_BASE_ID": knowledge_base_id,
            "KNOWLEDGE_BASE_REGION": knowledge_base_region,
            "PATIENT_PSEUDONYM_SECRET_ARN": pseudonym_secret.secret_arn,
            # ─── Guardrail ───────────────────────────────
            "BEDROCK_GUARDRAIL_ID": guardrail_id,
            "BEDROCK_GUARDRAIL_VERSION": guardrail_version,
            # ─── 기타 ────────────────────────────────────
            "DYNAMODB_CRITERIA_TABLE": criteria_table.table_name,
            "CORS_ALLOW_ORIGINS": cors_origins,
            "LOG_LEVEL": "INFO",
        }

        self.function = _lambda.Function(
            self,
            "MatchingApiFunction",
            function_name="healthcare-matching-api",
            runtime=_lambda.Runtime.PYTHON_3_12,
            architecture=_lambda.Architecture.X86_64,
            handler="lambda_handler.handler",
            code=_lambda.Code.from_asset(str(ASSET_DIR)),
            environment=environment,
            # 콜드 스타트에 데이터셋 CSV 를 읽고 컨테이너를 조립한다. 메모리를
            # 올리면 CPU 도 함께 올라가서 그 시간이 짧아진다.
            memory_size=2048,
            # 매칭 한 번에 기준마다 모델 호출이 붙는다. API Gateway 의 29초
            # 제한이 없는 Function URL 이므로 여유를 준다.
            timeout=Duration.minutes(5),
            log_group=logs.LogGroup(
                self,
                "MatchingApiLogGroup",
                log_group_name="/aws/lambda/healthcare-matching-api",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=RemovalPolicy.DESTROY,
            ),
        )

        # ─── 권한 ────────────────────────────────────────
        # Converse API 는 추론 프로파일(global.*)로 부른다. 프로파일 ARN 과 그
        # 프로파일이 라우팅하는 리전별 모델 ARN 둘 다 필요하다.
        self.function.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                    "bedrock:Converse",
                    "bedrock:ConverseStream",
                ],
                resources=[
                    "arn:aws:bedrock:*::foundation-model/*",
                    f"arn:aws:bedrock:*:{Stack.of(self).account}:inference-profile/*",
                ],
            )
        )

        if guardrail_id:
            self.function.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["bedrock:ApplyGuardrail"],
                    resources=[
                        f"arn:aws:bedrock:{Stack.of(self).region}:"
                        f"{Stack.of(self).account}:guardrail/{guardrail_id}"
                    ],
                )
            )

        if knowledge_base_id:
            # KB 는 us-west-2 에 있다. 이 스택의 리전을 쓰면 존재하지 않는
            # ARN 이 되어 Retrieve 가 AccessDenied 로 떨어진다.
            self.function.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["bedrock:Retrieve", "bedrock:RetrieveAndGenerate"],
                    resources=[
                        f"arn:aws:bedrock:{knowledge_base_region}:"
                        f"{Stack.of(self).account}:knowledge-base/{knowledge_base_id}"
                    ],
                )
            )

        # 일반 사용자는 승인 데이터만 읽고, 관리자 검토 API 는 대기 레코드의
        # 상태와 감사 필드를 원자적으로 갱신한다. 권한 판정은 Cognito admin
        # 그룹을 검증하는 API 계층에서 수행한다.
        criteria_table.grant_read_write_data(self.function)
        pseudonym_secret.grant_read(self.function)

        # ─── 공개 진입점 ─────────────────────────────────
        # CORS 는 FastAPI 미들웨어 한 곳에서만 처리한다. Function URL 에도 CORS 를
        # 설정하면 실제 응답에 Access-Control-Allow-Origin 이 두 번 붙고, 브라우저는
        # 동일한 값이어도 중복 헤더를 잘못된 CORS 응답으로 거부한다. 프리플라이트가
        # Lambda 를 깨우는 작은 비용보다 모든 브라우저 요청이 실패하는 쪽이 훨씬
        # 치명적이다.
        self.function_url = self.function.add_function_url(
            auth_type=_lambda.FunctionUrlAuthType.NONE,
        )

        CfnOutput(
            self,
            "ApiUrl",
            value=self.function_url.url,
            description="VITE_API_BASE_URL (끝의 / 를 빼고 넣는다)",
        )
        CfnOutput(
            self,
            "ApiFunctionName",
            value=self.function.function_name,
            description="CloudWatch 로그 조회용 함수 이름",
        )
