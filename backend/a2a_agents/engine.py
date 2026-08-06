"""Bedrock-backed role engine used inside each independent A2A Lambda."""

from __future__ import annotations

import json
from typing import Any

from agent.model import Conversation, ModelClient
from agent.prompts import UNKNOWN_DELIBERATION

from .contracts import RESPONSE_SCHEMA, normalize_decisions, validate_request


def _evidence_text(criteria: list[dict[str, Any]]) -> str:
    """Guardrail 이 검사할 외부 유입 텍스트만 모은다.

    환자 근거 서술과 관찰값이 외부 입력이다. 비어 있으면 빈 블록을 보내지 않고
    자리표시자를 넣는다. 블록이 비면 Bedrock 이 요청을 거부한다.
    """
    parts: list[str] = []
    for item in criteria:
        for narrative in item.get("narratives") or []:
            text = str(narrative.get("text") or "").strip()
            if text:
                parts.append(text)
        observed = item.get("observed_value")
        if observed not in (None, ""):
            parts.append(str(observed))
    return "\n".join(parts) if parts else "(근거 서술 없음)"


class RoleReviewEngine:
    def __init__(self, *, role: str, model: ModelClient) -> None:
        self.role = role
        self._model = model

    def review(self, payload: Any) -> dict[str, Any]:
        request = validate_request(payload, role=self.role)
        model_request: dict[str, Any] = {
            "role": self.role,
            "criteria": request["criteria"],
        }
        if self.role == "challenge_reviewer":
            model_request["prior_review"] = request["prior_review"]

        conversation = Conversation()
        # 기준 JSON 과 역할 지시문은 우리가 만든 신뢰 입력이다. Guardrail 검사
        # 대상은 외부에서 들어온 환자 근거 서술로 좁힌다. 전체를 평가하면 임상
        # 서술(임신·정신질환·약물 등)이 오탐으로 막혀 판정이 UNKNOWN 으로 떨어진다.
        conversation.user_blocks(
            [
                {"text": json.dumps(model_request, ensure_ascii=False)},
                Conversation.guarded(_evidence_text(request["criteria"])),
            ]
        )
        response = self._model.converse(
            conversation=conversation,
            system=UNKNOWN_DELIBERATION,
        )
        decisions = normalize_decisions(
            response.json_payload(),
            allowed_ids={item["criterion_id"] for item in request["criteria"]},
        )
        return {
            "schema": RESPONSE_SCHEMA,
            "role": self.role,
            "discussion_id": request["discussion_id"],
            "round": request["round"],
            "decisions": decisions,
            "usage": {
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
            },
        }
