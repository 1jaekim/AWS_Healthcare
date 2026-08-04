"""Bedrock Converse API 계약 검증.

실제 AWS 호출 없이, 그러나 **우리 코드가 아니라 AWS 스펙을 기준으로** 검증한다.
`botocore` 에는 bedrock-runtime 의 API 정의가 들어 있으므로, 우리가 만든 요청을
AWS 가 쓰는 그 정의로 검증한다. 우리가 기대하는 형식과 비교하는 순환 검증을 피하기
위한 방법이다.

여기서 확인하는 것과 확인하지 못하는 것을 구분해 둔다.

확인한다
- 요청이 실제 `Converse` 입력 스펙을 만족하는가 (필수 항목, 타입, 구조)
- 우리가 읽는 응답 필드명이 실제 출력 스펙에 존재하는가
- 도구 스키마가 실제 `toolSpec` 스펙을 만족하는가
- 실패·이상 응답이 폴백 경로로 이어지는가

확인하지 못한다
- 모델이 실제로 그 프롬프트에 어떻게 응답하는지
- 자격 증명·IAM 권한·리전별 모델 접근 가능 여부
- 실제 지연시간, 토큰 과금, 쓰로틀링 동작

위 세 가지는 자격 증명이 있는 환경에서 `scripts/verify_bedrock.py` 로 확인한다.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import botocore.session
import pytest
from botocore.exceptions import ClientError, ParamValidationError
from botocore.validate import validate_parameters

from agent.model import (
    BedrockModelClient,
    Conversation,
    ModelError,
    ModelResponse,
)
from agent.prompts import EVIDENCE_PLANNER, NLI_VERIFIER
from agent.toolspec import AGENT_TOOLS

# ---------------------------------------------------------------------------
# AWS API 정의 로딩
# ---------------------------------------------------------------------------

_SESSION = botocore.session.get_session()
_SERVICE = _SESSION.get_service_model("bedrock-runtime")
CONVERSE = _SERVICE.operation_model("Converse")


class CapturingBedrock:
    """요청을 기록하고 정해진 응답을 돌려주는 bedrock-runtime 대역.

    `messages` 는 Conversation 의 리스트를 그대로 넘겨받으므로 호출 시점에 깊은 복사를
    떠 둔다. 그러지 않으면 응답 파싱이 어시스턴트 턴을 덧붙인 뒤의 상태를 보게 된다.
    """

    def __init__(self, response: dict[str, Any] | None = None) -> None:
        self.kwargs: dict[str, Any] = {}
        self._response = response if response is not None else _text_response("확인")

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        self.kwargs = deepcopy(kwargs)
        return self._response


def _text_response(text: str) -> dict[str, Any]:
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
        "stopReason": "end_turn",
        "usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15},
    }


def build_client(
    fake: CapturingBedrock, **overrides: Any
) -> BedrockModelClient:
    settings: dict[str, Any] = {
        "model_id": "anthropic.claude-3-5-sonnet-20241022-v2:0",
        "region": "us-east-1",
        "max_tokens": 512,
        "temperature": 0.0,
        "client": fake,
    }
    settings.update(overrides)
    return BedrockModelClient(**settings)


def assert_valid_request(request: dict[str, Any]) -> None:
    """실제 Converse 입력 스펙으로 요청을 검증한다."""
    validate_parameters(request, CONVERSE.input_shape)


# ---------------------------------------------------------------------------
# 검증이 무력하지 않은지 먼저 확인한다
# ---------------------------------------------------------------------------


def test_스펙_검증은_잘못된_요청을_실제로_걸러낸다() -> None:
    """음성 대조. 이게 통과하면 아래 검증들이 의미를 갖는다."""
    with pytest.raises(ParamValidationError):
        assert_valid_request({"messages": [{"role": "assistant"}]})  # modelId 없음
    with pytest.raises(ParamValidationError):
        assert_valid_request({"modelId": "m", "messages": "문자열"})
    with pytest.raises(ParamValidationError):
        assert_valid_request(
            {"modelId": "m", "inferenceConfig": {"maxTokens": "숫자아님"}}
        )


# ---------------------------------------------------------------------------
# 요청 계약
# ---------------------------------------------------------------------------


def test_기본_요청이_실제_스펙을_만족한다() -> None:
    fake = CapturingBedrock()
    client = build_client(fake)
    conversation = Conversation()
    conversation.user_text("판정 근거를 확인하라.")

    client.converse(conversation=conversation, system=NLI_VERIFIER)

    assert_valid_request(fake.kwargs)
    assert fake.kwargs["modelId"] == "anthropic.claude-3-5-sonnet-20241022-v2:0"
    assert fake.kwargs["system"] == [{"text": NLI_VERIFIER}]
    assert fake.kwargs["inferenceConfig"] == {"maxTokens": 512, "temperature": 0.0}


def test_도구를_붙인_요청이_실제_스펙을_만족한다() -> None:
    fake = CapturingBedrock()
    client = build_client(fake)
    conversation = Conversation()
    conversation.user_text("근거 수집 계획을 세워라.")

    client.converse(
        conversation=conversation, system=EVIDENCE_PLANNER, tools=AGENT_TOOLS
    )

    assert_valid_request(fake.kwargs)
    assert fake.kwargs["toolConfig"] == {"tools": AGENT_TOOLS}


def test_가드레일을_붙인_요청이_실제_스펙을_만족한다() -> None:
    fake = CapturingBedrock()
    client = build_client(fake, guardrail_id="gr-1", guardrail_version="3")
    conversation = Conversation()
    conversation.user_text("설명을 작성하라.")

    client.converse(conversation=conversation, system="시스템")

    assert_valid_request(fake.kwargs)
    assert fake.kwargs["guardrailConfig"] == {
        "guardrailIdentifier": "gr-1",
        "guardrailVersion": "3",
    }


def test_도구_결과를_포함한_다음_턴도_스펙을_만족한다() -> None:
    """tool-use 루프의 두 번째 턴이 역할 교대 규칙과 블록 형식을 지키는지 본다."""
    fake = CapturingBedrock(
        {
            "output": {
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "toolUse": {
                                "toolUseId": "tu-1",
                                "name": "evidence_retrieval_tool",
                                "input": {"terms": ["임신 중"], "top_k": 3},
                            }
                        }
                    ],
                }
            },
            "stopReason": "tool_use",
            "usage": {"inputTokens": 20, "outputTokens": 8},
        }
    )
    client = build_client(fake)
    conversation = Conversation()
    conversation.user_text("계획을 세워라.")

    first = client.converse(
        conversation=conversation, system=EVIDENCE_PLANNER, tools=AGENT_TOOLS
    )
    assert first.wants_tools

    conversation.tool_results(
        [(first.tool_uses[0].tool_use_id, {"match_count": 0}, True)],
        trailing_text="남은 항목을 확인하라.",
    )
    client.converse(
        conversation=conversation, system=EVIDENCE_PLANNER, tools=AGENT_TOOLS
    )

    assert_valid_request(fake.kwargs)
    roles = [message["role"] for message in fake.kwargs["messages"]]
    assert roles == ["user", "assistant", "user"], "역할이 번갈아 나와야 한다"


def test_도구_실패도_스펙을_만족하는_블록으로_전달된다() -> None:
    fake = CapturingBedrock()
    client = build_client(fake)
    conversation = Conversation()
    conversation.user_text("계획")
    conversation.assistant_raw(
        [
            {
                "toolUse": {
                    "toolUseId": "tu-9",
                    "name": "timeline_graph_tool",
                    "input": {"fields": ["hba1c"]},
                }
            }
        ]
    )
    conversation.tool_results([("tu-9", {"error": "PermissionDenied"}, False)])

    client.converse(conversation=conversation, system="시스템", tools=AGENT_TOOLS)

    assert_valid_request(fake.kwargs)
    block = fake.kwargs["messages"][-1]["content"][0]["toolResult"]
    assert block["status"] == "error"


def test_설정하지_않은_항목은_요청에_넣지_않는다() -> None:
    """toolConfig 와 guardrailConfig 는 값이 있을 때만 보낸다."""
    fake = CapturingBedrock()
    client = build_client(fake)
    conversation = Conversation()
    conversation.user_text("x")

    client.converse(conversation=conversation, system="s", tools=None)
    assert "toolConfig" not in fake.kwargs
    assert "guardrailConfig" not in fake.kwargs

    client.converse(conversation=conversation, system="s", tools=[])
    assert "toolConfig" not in fake.kwargs, "빈 목록도 보내지 않는다"


def test_도구_스키마가_실제_toolSpec_스펙을_만족한다() -> None:
    """AGENT_TOOLS 를 toolConfig 스펙으로 직접 검증한다."""
    shape = CONVERSE.input_shape.members["toolConfig"]
    validate_parameters({"tools": AGENT_TOOLS}, shape)

    tool_spec_shape = shape.members["tools"].member.members["toolSpec"]
    assert set(tool_spec_shape.required_members) == {"name", "inputSchema"}
    for spec in AGENT_TOOLS:
        body = spec["toolSpec"]
        assert body["name"]
        assert "json" in body["inputSchema"], "inputSchema 는 json 문서를 담는다"


# ---------------------------------------------------------------------------
# 응답 계약
# ---------------------------------------------------------------------------


def test_우리가_읽는_응답_필드가_실제_출력_스펙에_있다() -> None:
    """필드명이 바뀌거나 오타가 있으면 여기서 걸린다."""
    members = CONVERSE.output_shape.members
    assert "output" in members
    assert "stopReason" in members
    assert "usage" in members
    assert set(members["usage"].members) >= {"inputTokens", "outputTokens"}

    message = members["output"].members["message"]
    assert set(message.members) >= {"role", "content"}

    block = message.members["content"].member
    assert {"text", "toolUse"} <= set(block.members)
    tool_use = block.members["toolUse"]
    assert set(tool_use.members) >= {"toolUseId", "name", "input"}


def test_텍스트_블록이_여러_개면_이어붙인다() -> None:
    fake = CapturingBedrock(
        {
            "output": {
                "message": {
                    "role": "assistant",
                    "content": [{"text": "첫째"}, {"text": "둘째"}],
                }
            },
            "stopReason": "end_turn",
            "usage": {"inputTokens": 1, "outputTokens": 1},
        }
    )
    response = _single_turn(fake)
    assert response.text == "첫째\n둘째"


def test_모르는_블록_종류는_무시한다() -> None:
    """reasoningContent 처럼 나중에 늘어난 블록이 와도 죽지 않아야 한다."""
    fake = CapturingBedrock(
        {
            "output": {
                "message": {
                    "role": "assistant",
                    "content": [
                        {"reasoningContent": {"reasoningText": {"text": "생각"}}},
                        {"text": '{"entailment": "SUPPORTED"}'},
                        {"citationsContent": {"citations": []}},
                    ],
                }
            },
            "stopReason": "end_turn",
            "usage": {"inputTokens": 1, "outputTokens": 1},
        }
    )
    response = _single_turn(fake)
    assert response.json_payload() == {"entailment": "SUPPORTED"}


def test_usage가_없어도_0으로_읽는다() -> None:
    fake = CapturingBedrock(
        {
            "output": {"message": {"role": "assistant", "content": [{"text": "x"}]}},
            "stopReason": "end_turn",
        }
    )
    response = _single_turn(fake)
    assert (response.input_tokens, response.output_tokens) == (0, 0)


def test_응답이_비어도_예외를_내지_않는다() -> None:
    response = _single_turn(CapturingBedrock({}))
    assert response.text == ""
    assert response.tool_uses == ()
    assert response.json_payload() is None


def test_toolUse_입력이_없으면_빈_인자로_읽는다() -> None:
    fake = CapturingBedrock(
        {
            "output": {
                "message": {
                    "role": "assistant",
                    "content": [
                        {"toolUse": {"toolUseId": "tu-1", "name": "timeline_graph_tool"}}
                    ],
                }
            },
            "stopReason": "tool_use",
        }
    )
    response = _single_turn(fake)
    assert response.tool_uses[0].arguments == {}


def test_원본_블록이_대화에_그대로_누적된다() -> None:
    """다음 턴에 toolResult 를 붙이려면 어시스턴트 블록이 원본이어야 한다."""
    fake = CapturingBedrock(
        {
            "output": {
                "message": {
                    "role": "assistant",
                    "content": [
                        {"text": "찾아보겠습니다"},
                        {
                            "toolUse": {
                                "toolUseId": "tu-1",
                                "name": "evidence_retrieval_tool",
                                "input": {"terms": ["임신 중"]},
                            }
                        },
                    ],
                }
            },
            "stopReason": "tool_use",
        }
    )
    conversation = Conversation()
    conversation.user_text("계획")
    build_client(fake).converse(conversation=conversation, system="s")

    last = conversation.messages[-1]
    assert last["role"] == "assistant"
    assert last["content"][1]["toolUse"]["toolUseId"] == "tu-1"


# ---------------------------------------------------------------------------
# 실패 매핑
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
            "Converse",
        ),
        ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "no access"}},
            "Converse",
        ),
        ClientError(
            {
                "Error": {
                    "Code": "ValidationException",
                    "Message": "model not supported",
                }
            },
            "Converse",
        ),
        ClientError(
            {"Error": {"Code": "ModelTimeoutException", "Message": "timeout"}},
            "Converse",
        ),
        RuntimeError("network down"),
    ],
)
def test_모든_호출_실패는_ModelError로_감싼다(error: Exception) -> None:
    """호출부가 예외 종류를 알 필요 없이 폴백할 수 있어야 한다."""

    class Boom:
        def converse(self, **kwargs: Any) -> dict[str, Any]:
            raise error

    conversation = Conversation()
    conversation.user_text("x")
    with pytest.raises(ModelError):
        build_client(Boom()).converse(conversation=conversation, system="s")  # type: ignore[arg-type]


def test_boto3가_없으면_생성_시점에_ModelError를_낸다(monkeypatch) -> None:
    """배포 환경에 의존성이 빠졌을 때 조용히 죽지 않게 한다."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any):
        if name == "boto3":
            raise ImportError("no boto3")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ModelError):
        BedrockModelClient(model_id="m", region="us-east-1")


# ---------------------------------------------------------------------------
# 보조
# ---------------------------------------------------------------------------


def _single_turn(fake: CapturingBedrock) -> ModelResponse:
    conversation = Conversation()
    conversation.user_text("x")
    return build_client(fake).converse(conversation=conversation, system="s")
