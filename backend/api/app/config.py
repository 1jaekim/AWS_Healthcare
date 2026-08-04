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
    region: str = field(
        default_factory=lambda: os.getenv(
            "AWS_REGION",
            os.getenv("AWS_DEFAULT_REGION", "ap-northeast-2"),
        )
    )
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
    region: str = field(
        default_factory=lambda: os.getenv("AWS_REGION", "us-east-1")
    )
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


settings = Settings()
