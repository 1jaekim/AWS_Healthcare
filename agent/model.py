"""모델 클라이언트: Bedrock Converse API 어댑터와 오프라인 스텁.

두 구현이 같은 계약을 만족한다. 자격 증명이 없으면 스텁이 결정론적으로 응답하므로
테스트와 로컬 개발이 네트워크 없이 돌아간다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol


class ModelError(RuntimeError):
    """모델 호출 실패. 호출부는 결정론적 경로로 폴백해야 한다."""


@dataclass(frozen=True)
class ToolUse:
    """모델이 요청한 도구 호출."""

    tool_use_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ModelResponse:
    """모델 한 턴의 응답."""

    text: str
    tool_uses: tuple[ToolUse, ...] = ()
    stop_reason: str = "end_turn"
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_uses)

    def json_payload(self) -> dict[str, Any] | None:
        """응답 본문에서 JSON 객체를 추출한다.

        모델이 코드 블록이나 설명을 덧붙이는 경우가 있어 첫 중괄호 블록을 찾는다.
        """
        text = self.text.strip()
        if not text:
            return None
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None


@dataclass
class Conversation:
    """Converse API 메시지 누적기."""

    messages: list[dict[str, Any]] = field(default_factory=list)

    def user_text(self, text: str) -> None:
        self.messages.append({"role": "user", "content": [{"text": text}]})

    def assistant_raw(self, content: list[dict[str, Any]]) -> None:
        self.messages.append({"role": "assistant", "content": content})

    def ensure_assistant(self, response: "ModelResponse") -> None:
        """어시스턴트 턴이 비어 있으면 응답으로 채운다.

        Converse API 는 user 와 assistant 가 번갈아 나와야 한다. 어댑터가
        원본 블록을 넣어줬으면 그대로 두고, 넣지 않았으면 응답에서 복원한다.
        클라이언트 구현에 따라 역할 교대가 조용히 깨지는 일을 막는 장치다.
        """
        if self.messages and self.messages[-1].get("role") == "assistant":
            return
        blocks: list[dict[str, Any]] = []
        if response.text.strip():
            blocks.append({"text": response.text})
        for use in response.tool_uses:
            blocks.append(
                {
                    "toolUse": {
                        "toolUseId": use.tool_use_id,
                        "name": use.name,
                        "input": use.arguments,
                    }
                }
            )
        if not blocks:
            blocks.append({"text": ""})
        self.messages.append({"role": "assistant", "content": blocks})

    def tool_results(
        self,
        results: list[tuple[str, Any, bool]],
        *,
        trailing_text: str | None = None,
    ) -> None:
        """도구 결과를 한 메시지로 묶어 전달한다.

        trailing_text 는 같은 메시지 안에 붙인다. 별도 user 메시지로 보내면
        Converse API 의 역할 교대 규칙을 깨기 때문이다.
        """
        blocks: list[dict[str, Any]] = []
        for tool_use_id, payload, ok in results:
            blocks.append(
                {
                    "toolResult": {
                        "toolUseId": tool_use_id,
                        "content": [{"json": payload}],
                        "status": "success" if ok else "error",
                    }
                }
            )
        if trailing_text:
            blocks.append({"text": trailing_text})
        self.messages.append({"role": "user", "content": blocks})


class ModelClient(Protocol):
    """모델 호출 계약."""

    mode: str

    def converse(
        self,
        *,
        conversation: Conversation,
        system: str,
        tools: list[dict[str, Any]] | None = None,
    ) -> ModelResponse: ...


class BedrockModelClient:
    """Amazon Bedrock Converse API 어댑터."""

    mode = "bedrock"

    def __init__(
        self,
        *,
        model_id: str,
        region: str,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        guardrail_id: str | None = None,
        guardrail_version: str = "DRAFT",
        client: Any | None = None,
    ) -> None:
        self._model_id = model_id
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._guardrail_id = guardrail_id
        self._guardrail_version = guardrail_version
        if client is not None:
            self._client = client
            return
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover - 배포 환경 의존
            raise ModelError("boto3 가 설치되지 않았습니다.") from exc
        self._client = boto3.client("bedrock-runtime", region_name=region)

    def converse(
        self,
        *,
        conversation: Conversation,
        system: str,
        tools: list[dict[str, Any]] | None = None,
    ) -> ModelResponse:
        request: dict[str, Any] = {
            "modelId": self._model_id,
            "messages": conversation.messages,
            "system": [{"text": system}],
            "inferenceConfig": {
                "maxTokens": self._max_tokens,
                "temperature": self._temperature,
            },
        }
        if tools:
            request["toolConfig"] = {"tools": tools}
        if self._guardrail_id:
            request["guardrailConfig"] = {
                "guardrailIdentifier": self._guardrail_id,
                "guardrailVersion": self._guardrail_version,
            }

        try:
            raw = self._client.converse(**request)
        except Exception as exc:  # noqa: BLE001 - 폴백을 위해 단일 예외로 감싼다
            raise ModelError(f"Bedrock 호출 실패: {exc}") from exc

        return self._parse(raw, conversation)

    @staticmethod
    def _parse(raw: dict[str, Any], conversation: Conversation) -> ModelResponse:
        message = raw.get("output", {}).get("message", {})
        content = message.get("content", []) or []
        conversation.assistant_raw(content)

        texts: list[str] = []
        tool_uses: list[ToolUse] = []
        for block in content:
            if "text" in block:
                texts.append(str(block["text"]))
            elif "toolUse" in block:
                use = block["toolUse"]
                tool_uses.append(
                    ToolUse(
                        tool_use_id=str(use.get("toolUseId", "")),
                        name=str(use.get("name", "")),
                        arguments=dict(use.get("input", {}) or {}),
                    )
                )

        usage = raw.get("usage", {}) or {}
        return ModelResponse(
            text="\n".join(texts),
            tool_uses=tuple(tool_uses),
            stop_reason=str(raw.get("stopReason", "end_turn")),
            input_tokens=int(usage.get("inputTokens", 0) or 0),
            output_tokens=int(usage.get("outputTokens", 0) or 0),
        )


def _last_user_payload(conversation: Conversation) -> dict[str, Any]:
    """마지막 사용자 메시지의 JSON 블록을 읽는다. 스텁이 의도를 파악하는 통로."""
    for message in reversed(conversation.messages):
        if message.get("role") != "user":
            continue
        for block in message.get("content", []):
            text = block.get("text")
            if not text:
                continue
            start = text.find("{")
            end = text.rfind("}")
            if start == -1 or end <= start:
                continue
            try:
                parsed = json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
    return {}


class StubModelClient:
    """오프라인 결정론적 스텁.

    Bedrock 자격 증명이 없을 때 사용한다. 같은 입력에 같은 출력을 내며,
    실제 FM 의 역할을 흉내내되 판정을 바꾸지 않는 보수적 응답을 낸다.
    """

    mode = "stub"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def converse(
        self,
        *,
        conversation: Conversation,
        system: str,
        tools: list[dict[str, Any]] | None = None,
    ) -> ModelResponse:
        payload = _last_user_payload(conversation)
        task = str(payload.get("task", "")).lower()
        self.calls.append(task or "unknown")

        handler = {
            "nli": self._nli,
            "evaluate_trial_criterion": self._judge,
            "explain": self._explain,
            "question": self._question,
            "plan": self._plan,
            "intake": self._intake,
        }.get(task)
        if handler is None:
            return ModelResponse(text="{}")
        return handler(payload)

    # -- 개별 태스크 -------------------------------------------------------

    def _nli(self, payload: dict[str, Any]) -> ModelResponse:
        """규칙 계산 결과를 그대로 지지한다. 스텁은 판정을 뒤집지 않는다."""
        satisfied = payload.get("rule_satisfied")
        has_observation = payload.get("has_observation", False)
        if not has_observation:
            verdict = {
                "entailment": "NOT_ENOUGH_INFO",
                "confidence": 0.0,
                "rationale": "관찰값이 없어 판단할 수 없습니다.",
                "conflicts": [],
            }
        elif satisfied is True:
            verdict = {
                "entailment": "SUPPORTED",
                "confidence": 0.85,
                "rationale": "구조화 관찰값이 기준을 충족합니다.",
                "conflicts": [],
            }
        elif satisfied is False:
            verdict = {
                "entailment": "CONTRADICTED",
                "confidence": 0.85,
                "rationale": "구조화 관찰값이 기준과 어긋납니다.",
                "conflicts": [],
            }
        else:
            verdict = {
                "entailment": "NOT_ENOUGH_INFO",
                "confidence": 0.2,
                "rationale": "규칙 계산이 판정 불가입니다.",
                "conflicts": [],
            }
        return ModelResponse(text=json.dumps(verdict, ensure_ascii=False))

    def _judge(self, payload: dict[str, Any]) -> ModelResponse:
        """규칙 계산 결과를 그대로 제안한다. 스텁은 판정을 뒤집지 않는다.

        인용 근거는 입력에 실제로 들어 있는 `evidence_id` 만 쓴다. 스텁이 없는
        출처를 만들어내면 Verifier 의 근거 존재 검사가 그것을 잡아내야 하는데,
        그러면 스텁 모드의 판정이 규칙 경로와 달라진다.
        """
        criterion = payload.get("criterion") or {}
        evidence = payload.get("evidence") or []
        evidence_ids = [
            str(item.get("evidence_id"))
            for item in evidence
            if isinstance(item, dict) and item.get("evidence_id")
        ]
        satisfied = (payload.get("rule_outcome") or {}).get("satisfied")

        if not evidence_ids or satisfied is None:
            verdict = {
                "criterion_id": criterion.get("criterion_id"),
                "proposed_status": "UNKNOWN",
                "confidence": 0.2,
                "reason": "규칙 계산이 판정 불가이거나 인용할 근거가 없습니다.",
                "used_evidence_ids": [],
                "missing_information": ["구조화 관찰값 또는 측정 시점"],
                "needs_a2a": False,
            }
        else:
            verdict = {
                "criterion_id": criterion.get("criterion_id"),
                "proposed_status": "OK" if satisfied else "NOT_OK",
                "confidence": 0.85,
                "reason": "규칙 계산 결과와 인용 근거가 일치합니다.",
                "used_evidence_ids": evidence_ids[:1],
                "missing_information": [],
                "needs_a2a": False,
            }
        return ModelResponse(text=json.dumps(verdict, ensure_ascii=False))

    def _explain(self, payload: dict[str, Any]) -> ModelResponse:
        audience = str(payload.get("audience", "admin"))
        status = str(payload.get("eligibility_status", ""))
        met = payload.get("criteria_met", 0)
        total = payload.get("criteria_total", 0)
        if audience == "patient":
            summary = "확인된 기록을 기준으로 판단한 참여 가능성 안내입니다."
        else:
            summary = f"{total}개 기준 중 {met}개에서 근거가 확인되었습니다. 상태 {status}."
        return ModelResponse(
            text=json.dumps(
                {"summary": summary, "highlights": []}, ensure_ascii=False
            )
        )

    def _question(self, payload: dict[str, Any]) -> ModelResponse:
        label = str(payload.get("label", "해당 항목"))
        return ModelResponse(
            text=json.dumps(
                {"question": f"{label} 관련 최근 기록을 확인해 주실 수 있나요?"},
                ensure_ascii=False,
            )
        )

    def _intake(self, payload: dict[str, Any]) -> ModelResponse:
        """추출을 시도하지 않고 빈 목록을 낸다.

        스텁이 문장을 해석하면 규칙 추출기와 결과가 갈려 재현성이 흔들린다.
        Intake 는 규칙 추출기만으로도 완결되므로, 스텁은 아무것도 덧붙이지 않는다.
        """
        return ModelResponse(text=json.dumps({"events": []}, ensure_ascii=False))

    def _plan(self, payload: dict[str, Any]) -> ModelResponse:
        """자유서술 확인이 필요한 기준이 남아 있으면 검색을 한 번 요청한다."""
        pending = payload.get("pending_narrative") or []
        if not pending:
            return ModelResponse(
                text=json.dumps({"done": True}, ensure_ascii=False),
                stop_reason="end_turn",
            )
        first = pending[0]
        return ModelResponse(
            text="",
            tool_uses=(
                ToolUse(
                    tool_use_id=f"stub-{first.get('criterion_id', 'x')}",
                    name="evidence_retrieval_tool",
                    arguments={
                        "terms": list(first.get("terms", [])),
                        "top_k": 3,
                    },
                ),
            ),
            stop_reason="tool_use",
        )
