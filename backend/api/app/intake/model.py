"""지원서 수집 모듈의 오프라인 결정론적 모델 대체 구현."""

from __future__ import annotations

import json
import re
from typing import Any

from agent.model import Conversation, ModelResponse


def _last_user_payload(conversation: Conversation) -> dict[str, Any]:
    for message in reversed(conversation.messages):
        if message.get("role") != "user":
            continue
        for block in message.get("content", []):
            text = block.get("text")
            if not isinstance(text, str):
                continue
            start, end = text.find("{"), text.rfind("}")
            if start < 0 or end <= start:
                continue
            try:
                payload = json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                return payload
    return {}


class StubApplicationModelClient:
    """Bedrock 없이 스키마 계약과 기본 추출 흐름을 검증한다."""

    mode = "stub"

    def converse(
        self,
        *,
        conversation: Conversation,
        system: str,
        tools: list[dict[str, Any]] | None = None,
    ) -> ModelResponse:
        payload = _last_user_payload(conversation)
        task = str(payload.get("task", "")).lower()
        if task == "intake_schema":
            return ModelResponse(text=json.dumps({"fields": []}, ensure_ascii=False))
        if task != "intake_extract":
            return ModelResponse(text="{}")

        text = str(payload.get("applicant_text", ""))
        properties = (payload.get("schema") or {}).get("properties") or {}
        values: dict[str, Any] = {}

        age = re.search(r"(?:만\s*)?(\d{1,3})\s*(?:세|살)", text)
        if age and "age" in properties:
            values["age"] = int(age.group(1))
        if "sex" in properties:
            if re.search(r"(?:여성|여자|female)", text, re.IGNORECASE):
                values["sex"] = "female"
            elif re.search(r"(?:남성|남자|male)", text, re.IGNORECASE):
                values["sex"] = "male"

        for name, spec in properties.items():
            if name in values:
                continue
            title = str(spec.get("title", ""))
            match = re.search(
                rf"(?:{re.escape(name)}|{re.escape(title)})\s*[:：]\s*([^\n,]+)",
                text,
                re.IGNORECASE,
            )
            if match:
                values[name] = match.group(1).strip()
        return ModelResponse(
            text=json.dumps({"values": values}, ensure_ascii=False)
        )
