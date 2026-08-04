"""
Cognito 사용자 인증 CDK Stack
─────────────────────────────
아키텍처 v2 의 `Amazon Cognito` 자리다. 프론트엔드(`frontend/`)의 로그인·회원가입
화면이 이 User Pool 에 직접 붙는다.

담는 것과 담지 않는 것:
  - 담는다: 로그인 자격(이메일·비밀번호), 이름·생년·성별·연락처, 관심 임상 분야
            같은 설문 답변
  - 담지 않는다: 진료·검사 기록. 임상 정보는 비식별화를 거쳐
                 PatientClinicalEventTable 과 S3 rag/ 로 간다.

설문 답변을 커스텀 속성에 두는 것은 임시다. 아키텍처의 목표 저장소는
DynamoDB `UserProfileTable` 이며, 그 테이블과 API 가 생기면 이 속성들은 걷어낸다.
커스텀 속성은 한 번 만들면 삭제할 수 없으므로 개수를 최소로 유지한다.

백엔드 FastAPI(`backend/api`)가 이 풀이 발급한 토큰을 검증한다
(`backend/api/app/auth/`). 아래 출력값을 API 환경 변수로 넣어야 `/api/v1/*` 가
보호된다. 넣지 않으면 API 는 개방 모드로 뜨고, `AUTH_REQUIRED=true` 를 함께 주면
설정 누락 상태에서 요청을 아예 거부한다.

    COGNITO_USER_POOL_ID=<UserPoolId 출력값>
    COGNITO_CLIENT_ID=<UserPoolClientId 출력값>
    AUTH_REQUIRED=true
"""
from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_cognito as cognito,
)
from constructs import Construct


ADMIN_GROUP_NAME = "admin"
"""관리자 그룹 이름. 백엔드 기본값(`COGNITO_ADMIN_GROUP`)과 같아야 한다."""


class AuthStack(Stack):
    """임상시험 매칭 서비스 사용자 인증 스택"""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ─── User Pool ───────────────────────────────────
        self.user_pool = cognito.UserPool(
            self,
            "TrialMatchingUserPool",
            user_pool_name="trial-matching-users",
            # 이메일을 아이디로 쓴다. 시안 2a 의 "아이디 (이메일)" 과 같다.
            self_sign_up_enabled=True,
            sign_in_aliases=cognito.SignInAliases(email=True),
            sign_in_case_sensitive=False,
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            user_verification=cognito.UserVerificationConfig(
                email_subject="임상시험 매칭 - 이메일 인증",
                email_body=(
                    "임상시험 매칭 서비스 가입을 확인합니다.\n\n"
                    "인증 코드: {####}\n\n"
                    "본인이 요청하지 않았다면 이 메일을 무시하세요."
                ),
                email_style=cognito.VerificationEmailStyle.CODE,
            ),
            # 시안 2b 의 안내 문구와 같은 정책: 8자 이상, 영문·숫자 조합.
            # 화면에 적힌 것보다 실제 정책이 더 빡세면 사용자가 이유를 모른 채
            # 막히므로, 기호는 요구하지 않는다.
            password_policy=cognito.PasswordPolicy(
                min_length=8,
                require_lowercase=True,
                require_digits=True,
                require_uppercase=False,
                require_symbols=False,
                temp_password_validity=Duration.days(3),
            ),
            standard_attributes=cognito.StandardAttributes(
                email=cognito.StandardAttribute(required=True, mutable=True),
                fullname=cognito.StandardAttribute(required=False, mutable=True),
                birthdate=cognito.StandardAttribute(required=False, mutable=True),
                gender=cognito.StandardAttribute(required=False, mutable=True),
                phone_number=cognito.StandardAttribute(required=False, mutable=True),
            ),
            custom_attributes={
                # 합성 EMR 의 person_id 바인딩. 백엔드 API 가 전부 person_id 로
                # 말하기 때문에 계정과 이어줄 값이 필요하다.
                "person_id": cognito.StringAttribute(
                    min_len=1, max_len=32, mutable=True
                ),
                # 설문 4문항 (시안 2c). UserProfileTable 이 생기면 옮긴다.
                "interest_areas": cognito.StringAttribute(
                    min_len=0, max_len=512, mutable=True
                ),
                "survey_purpose": cognito.StringAttribute(
                    min_len=0, max_len=256, mutable=True
                ),
                "survey_medication": cognito.StringAttribute(
                    min_len=0, max_len=32, mutable=True
                ),
                "survey_allergy": cognito.StringAttribute(
                    min_len=0, max_len=512, mutable=True
                ),
                # 약관·민감정보 동의 기록. 필수 동의 없이는 가입이 제한되므로
                # 동의 시점을 남긴다.
                "agreed_at": cognito.StringAttribute(
                    min_len=0, max_len=64, mutable=True
                ),
                "marketing_opt_in": cognito.StringAttribute(
                    min_len=0, max_len=8, mutable=True
                ),
            },
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            # 사용자 계정을 실수로 날리지 않는다. 스택을 지워도 풀은 남는다.
            removal_policy=RemovalPolicy.RETAIN,
        )

        # ─── SPA 클라이언트 ──────────────────────────────
        # 정적 호스팅(S3/Amplify)에서 도는 브라우저 앱이므로 클라이언트 시크릿을
        # 두지 않는다. 번들에 넣으면 누구나 꺼낼 수 있어 비밀이 아니다.
        self.user_pool_client = self.user_pool.add_client(
            "TrialMatchingWebClient",
            user_pool_client_name="trial-matching-web",
            generate_secret=False,
            auth_flows=cognito.AuthFlow(user_srp=True),
            prevent_user_existence_errors=True,
            access_token_validity=Duration.hours(1),
            id_token_validity=Duration.hours(1),
            refresh_token_validity=Duration.days(30),
            enable_token_revocation=True,
        )

        # ─── 관리자 그룹 ─────────────────────────────────
        # 코호트·검토 큐·감사 로그·근거 패널은 연구 담당자만 본다. 백엔드는
        # ID 토큰의 `cognito:groups` 에 이 이름이 있는지로 판단한다
        # (backend/api/app/auth/, COGNITO_ADMIN_GROUP 기본값 = admin).
        #
        # 그룹 소속은 콘솔이나 CLI 로 부여한다. 셀프 가입으로는 들어올 수 없다:
        #   aws cognito-idp admin-add-user-to-group \
        #     --user-pool-id <pool> --username <email> --group-name admin
        self.admin_group = cognito.CfnUserPoolGroup(
            self,
            "AdminGroup",
            user_pool_id=self.user_pool.user_pool_id,
            group_name=ADMIN_GROUP_NAME,
            description="연구 담당자. 코호트·검토 큐·감사 로그 조회 권한",
            precedence=0,
        )

        # ─── 출력 ────────────────────────────────────────
        # 프론트 빌드 환경변수로 넣는 값들이다.
        # Vite 는 빌드 시점에 굽기 때문에 값이 바뀌면 재배포해야 한다.
        CfnOutput(
            self,
            "UserPoolId",
            value=self.user_pool.user_pool_id,
            description="VITE_COGNITO_USER_POOL_ID / COGNITO_USER_POOL_ID",
        )
        CfnOutput(
            self,
            "UserPoolClientId",
            value=self.user_pool_client.user_pool_client_id,
            description="VITE_COGNITO_CLIENT_ID / COGNITO_CLIENT_ID",
        )
        CfnOutput(
            self,
            "UserPoolRegion",
            value=self.region,
            description="VITE_COGNITO_REGION / COGNITO_REGION",
        )
        CfnOutput(
            self,
            "AdminGroupName",
            value=ADMIN_GROUP_NAME,
            description="COGNITO_ADMIN_GROUP (관리자 전용 API 판단 기준)",
        )
        CfnOutput(
            self,
            "TokenIssuer",
            value=(
                f"https://cognito-idp.{self.region}.amazonaws.com/"
                f"{self.user_pool.user_pool_id}"
            ),
            description="백엔드가 검증하는 iss 클레임",
        )
