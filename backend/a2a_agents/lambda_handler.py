"""Lambda entry point shared by the two independently deployed A2A agents."""

from __future__ import annotations

import os

from mangum import Mangum

from agent.model import BedrockModelClient

from .engine import RoleReviewEngine
from .server import create_a2a_app


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


model = BedrockModelClient(
    model_id=_required("BEDROCK_MODEL_ID"),
    region=os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "ap-northeast-2",
    max_tokens=int(os.getenv("BEDROCK_MAX_TOKENS", "1536")),
    temperature=0.0,
    guardrail_id=os.getenv("BEDROCK_GUARDRAIL_ID") or None,
    guardrail_version=os.getenv("BEDROCK_GUARDRAIL_VERSION", "DRAFT"),
)
app = create_a2a_app(
    RoleReviewEngine(role=_required("A2A_AGENT_ROLE"), model=model)
)
handler = Mangum(app, lifespan="off")
