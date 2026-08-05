from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


# backend/api/app/config.py -> parents[3] 이 저장소 루트다.
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DATA_DIR = PROJECT_ROOT / "outputs" / "longitudinal_emr_v2"


def _flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


DEFAULT_REGION = "ap-northeast-2"
"""배포 리전 기본값. 서울 리전에 Bedrock KB 와 Neptune Analytics 를 둔다.

리전 기본값을 여러 곳에 흩어 적으면 한쪽만 바뀌어도 조용히 어긋난다. 모델 호출과
GraphRAG 검색이 서로 다른 리전을 보게 되면 KB 를 찾지 못한다. 그래서 이 상수와
`resolve_region()` 을 유일한 출처로 둔다.
"""


def resolve_region() -> str:
    """실행 리전을 정한다. AWS 표준 환경 변수를 순서대로 본다."""
    return os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or DEFAULT_REGION


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class ModelSettings:
    """Bedrock FM 설정.

    기본값은 비활성이다. 자격 증명이 없는 환경에서도 결정론적 스텁으로
    동작해야 하므로, 실제 호출은 명시적으로 켜야 한다.
    """

    enabled: bool = field(
        default_factory=lambda: _flag("BEDROCK_ENABLED", False)
    )
    region: str = field(default_factory=resolve_region)
    model_id: str = field(
        default_factory=lambda: os.getenv(
            "BEDROCK_MODEL_ID",
            "global.anthropic.claude-sonnet-4-5-20250929-v1:0",
        )
    )
    max_tokens: int = field(
        default_factory=lambda: _int("BEDROCK_MAX_TOKENS", 1024)
    )
    temperature: float = 0.0
    """재현성을 위해 0으로 고정한다."""

    max_agent_iterations: int = field(
        default_factory=lambda: _int("AGENT_MAX_ITERATIONS", 4)
    )
    """Tool-use 루프 상한. 비용과 지연시간을 묶어두는 안전장치."""

    max_deliberation_criteria: int = field(
        default_factory=lambda: _int("A2A_MAX_CRITERIA", 5)
    )
    """한 실행에서 2라운드 교차 검토할 미해소 기준 수 상한."""

    recommendation_max_workers: int = field(
        default_factory=lambda: min(
            5, max(1, _int("RECOMMENDATION_MAX_WORKERS", 2))
        )
    )
    """추천 시 동시에 스크리닝할 공고 수.

    Bedrock 호출은 네트워크 대기가 대부분이므로 공고를 완전히 순차 처리할 이유가
    없다. 다만 무제한 병렬화는 모델 throttling과 비용 급증을 만들 수 있어 1~5로
    제한한다. 1로 두면 기존 순차 동작으로 즉시 되돌릴 수 있다.
    """

    criterion_judge_enabled: bool = field(
        default_factory=lambda: _flag("CRITERION_JUDGE_ENABLED", True)
    )
    """기준별 `LLM 판단 → Verifier → Rule Aggregator` 단계를 사용할지 여부.

    끄면 FM NLI 검증기(`agent/verifier.py`) 경로로 되돌아간다. 두 경로 모두
    상태 확정은 결정론적 계층이 하므로 판정 재현성은 같다.
    """

    guardrail_id: str | None = field(
        default_factory=lambda: os.getenv("BEDROCK_GUARDRAIL_ID") or None
    )
    guardrail_version: str = field(
        default_factory=lambda: os.getenv("BEDROCK_GUARDRAIL_VERSION", "DRAFT")
    )


@dataclass(frozen=True)
class GraphRagSettings:
    """Bedrock Knowledge Base Retrieve 연결 설정."""

    knowledge_base_id: str | None = field(
        default_factory=lambda: os.getenv("KNOWLEDGE_BASE_ID") or None
    )
    region: str = field(default_factory=resolve_region)
    """Secrets Manager 등 이 서비스와 같은 리전에 있는 것들을 부를 때 쓴다."""

    knowledge_base_region: str = field(
        default_factory=lambda: os.getenv("KNOWLEDGE_BASE_REGION")
        or resolve_region()
    )
    """KB 만 다른 리전에 있을 때 쓰는 값.

    나머지 인프라는 서울에 있지만 GraphRAG 는 us-west-2 에 있다. Bedrock
    Knowledge Bases 가 서울에서 Neptune Analytics 스토리지를 아직 받지 않기
    때문이다(`backend/infra/graphrag_stack.py`).

    비워두면 `region` 과 같아진다. 리전이 갈린 배포에서 이 값을 빠뜨리면 API 가
    서울에서 KB 를 찾다가 ResourceNotFound 로 떨어진다. 조용히 로컬 검색으로
    내려앉지 않고 실패하는 편이 낫다 — 근거 없이 판정이 나가는 것보다 낫다.

    서울에서 KB 가 열리면 이 값을 지우면 된다.
    """

    patient_pseudonym_secret: str | None = field(
        default_factory=lambda: os.getenv("PATIENT_PSEUDONYM_SECRET") or None
    )
    patient_pseudonym_secret_arn: str | None = field(
        default_factory=lambda: os.getenv("PATIENT_PSEUDONYM_SECRET_ARN") or None
    )

    @property
    def enabled(self) -> bool:
        return bool(self.knowledge_base_id)


@dataclass(frozen=True)
class AuthSettings:
    """Amazon Cognito 토큰 검증 설정.

    프론트엔드(`frontend/`)는 Cognito ID 토큰을 `Authorization: Bearer` 로 보낸다.
    이 설정이 채워지면 API 가 그 토큰을 검증하고, 비어 있으면 개방 모드로 돈다.

    개방 모드는 로컬 개발과 기존 테스트를 위한 것이다. 배포 환경에서는
    `AUTH_REQUIRED=true` 를 켜서 설정 누락이 조용한 무인증 배포가 되지 않게 한다.

    값은 `cdk deploy HealthcareAuthStack` 출력에서 가져온다.

        COGNITO_USER_POOL_ID=ap-northeast-2_xxxxxxxxx
        COGNITO_CLIENT_ID=xxxxxxxxxxxxxxxxxxxxxxxxxx
    """

    user_pool_id: str | None = field(
        default_factory=lambda: os.getenv("COGNITO_USER_POOL_ID") or None
    )
    client_ids: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            item.strip()
            for item in os.getenv("COGNITO_CLIENT_ID", "").split(",")
            if item.strip()
        )
    )
    region: str = field(
        default_factory=lambda: os.getenv("COGNITO_REGION") or resolve_region()
    )
    admin_group: str = field(
        default_factory=lambda: os.getenv("COGNITO_ADMIN_GROUP", "admin")
    )
    required: bool = field(default_factory=lambda: _flag("AUTH_REQUIRED", False))
    """켜면 설정이 비었을 때 개방 모드로 내려앉지 않고 요청을 거부한다."""

    jwks_cache_seconds: int = field(
        default_factory=lambda: _int("COGNITO_JWKS_CACHE_SECONDS", 3600)
    )
    """JWKS 캐시 수명. Cognito 의 서명 키는 거의 바뀌지 않는다."""

    leeway_seconds: int = field(
        default_factory=lambda: _int("COGNITO_CLOCK_LEEWAY_SECONDS", 30)
    )
    """시계 오차 허용치. 컨테이너 시계가 몇 초 밀리는 일은 흔하다."""

    @property
    def configured(self) -> bool:
        return bool(self.user_pool_id and self.client_ids)

    @property
    def issuer(self) -> str:
        """토큰의 `iss` 클레임과 일치해야 하는 값."""
        return (
            f"https://cognito-idp.{self.region}.amazonaws.com/{self.user_pool_id}"
        )

    @property
    def jwks_url(self) -> str:
        return f"{self.issuer}/.well-known/jwks.json"


DEFAULT_CORS_ORIGINS = (
    "http://localhost:3000",
    "http://127.0.0.1:3000",
)


def _origins() -> tuple[str, ...]:
    """CORS 허용 출처.

    프론트엔드(`frontend/`)는 Amplify Hosting 또는 S3+CloudFront 에 정적으로
    배포된다. 배포 도메인은 환경마다 달라서 쉼표로 구분해 받는다. 기본값은
    로컬 vite 개발 서버뿐이다.

        CORS_ALLOW_ORIGINS=https://main.d123.amplifyapp.com,https://trial.example.com
    """
    raw = os.getenv("CORS_ALLOW_ORIGINS")
    if raw is None or not raw.strip():
        return DEFAULT_CORS_ORIGINS
    return tuple(item.strip() for item in raw.split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    app_name: str = "Clinical Trial Screening API"
    app_version: str = "0.3.0"
    data_dir: Path = field(
        default_factory=lambda: Path(
            os.getenv("EMR_DATA_DIR", str(DEFAULT_DATA_DIR))
        )
    )
    model: ModelSettings = field(default_factory=ModelSettings)
    graphrag: GraphRagSettings = field(default_factory=GraphRagSettings)
    auth: AuthSettings = field(default_factory=AuthSettings)
    cors_allow_origins: tuple[str, ...] = field(default_factory=_origins)


settings = Settings()
