"""Guardrail 검사 범위 표시(`guardContent`)의 안전한 기본 동작.

Bedrock 은 `guardContent` 블록이 있는데 `guardrailConfig` 가 없으면 요청을
거부한다. 판정 경로는 Guardrail 을 켜고 끌 수 있어야 하므로, 그 조합을 모델
클라이언트가 흡수해야 한다. 흡수하지 않으면 Guardrail 을 끄는 순간 모든 판정이
`UNKNOWN` 으로 죽는다. 실제로 그렇게 죽는 것을 한 번 겪었다.
"""

from __future__ import annotations

from typing import Any

from agent.model import BedrockModelClient, Conversation


class FakeBedrock:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        self.kwargs = kwargs
        return {
            "output": {"message": {"role": "assistant", "content": [{"text": "{}"}]}},
            "stopReason": "end_turn",
            "usage": {"inputTokens": 1, "outputTokens": 1},
        }


def _conversation() -> Conversation:
    conversation = Conversation()
    conversation.user_blocks(
        [
            {"text": '{"role": "evidence_reviewer"}'},
            Conversation.guarded("환자 근거 서술"),
        ]
    )
    return conversation


def _blocks(fake: FakeBedrock) -> list[dict[str, Any]]:
    return fake.kwargs["messages"][0]["content"]


def test_guardrail_있으면_검사_범위를_그대로_보낸다() -> None:
    fake = FakeBedrock()
    client = BedrockModelClient(
        model_id="m", region="r", guardrail_id="gr-1", guardrail_version="1", client=fake
    )

    client.converse(conversation=_conversation(), system="s")

    blocks = _blocks(fake)
    assert blocks[0] == {"text": '{"role": "evidence_reviewer"}'}
    assert blocks[1]["guardContent"]["text"]["text"] == "환자 근거 서술"
    assert fake.kwargs["guardrailConfig"]["guardrailIdentifier"] == "gr-1"


def test_guardrail_없으면_guardContent_를_평문으로_되돌린다() -> None:
    """이 변환이 없으면 Bedrock 이 ValidationException 으로 요청을 거부한다."""
    fake = FakeBedrock()
    client = BedrockModelClient(model_id="m", region="r", client=fake)

    client.converse(conversation=_conversation(), system="s")

    blocks = _blocks(fake)
    assert "guardrailConfig" not in fake.kwargs
    assert all("guardContent" not in block for block in blocks)
    assert blocks == [
        {"text": '{"role": "evidence_reviewer"}'},
        {"text": "환자 근거 서술"},
    ]


def test_빈_검사_블록은_요청에서_사라진다() -> None:
    """빈 텍스트 블록만 남으면 Bedrock 이 거부하므로 자리표시자를 남긴다."""
    fake = FakeBedrock()
    client = BedrockModelClient(model_id="m", region="r", client=fake)
    conversation = Conversation()
    conversation.user_blocks([Conversation.guarded("   ")])

    client.converse(conversation=conversation, system="s")

    assert _blocks(fake) == [{"text": ""}]
