"""모델에 노출할 도구 스키마.

Converse API toolConfig 형식이다. 모델이 호출할 수 있는 도구는 읽기 전용으로 제한한다.
Rule Evaluator 와 저장소 쓰기는 모델에게 노출하지 않는다. 판정과 저장은 결정론적
경로가 담당해야 하므로 모델의 손이 닿지 않게 둔다.
"""

from __future__ import annotations

from typing import Any

EVIDENCE_RETRIEVAL_SPEC: dict[str, Any] = {
    "toolSpec": {
        "name": "evidence_retrieval_tool",
        "description": (
            "환자의 자유서술 EMR 에서 주어진 표현이 실제로 언급된 문장을 찾는다. "
            "측정값 나열이 아니라 조건을 주장하는 문장을 찾을 때 사용한다."
        ),
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "terms": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "검색할 표현. 일반적인 단어보다 조건을 주장하는 "
                            "구체적 어구를 쓴다. 예: '조절되지 않는 고혈압'"
                        ),
                        "minItems": 1,
                        "maxItems": 6,
                    },
                    "top_k": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 5,
                        "description": "반환할 문장 수",
                    },
                },
                "required": ["terms"],
            }
        },
    }
}

TIMELINE_GRAPH_SPEC: dict[str, Any] = {
    "toolSpec": {
        "name": "timeline_graph_tool",
        "description": (
            "환자 타임라인에서 지정한 필드의 관찰값을 조회한다. "
            "측정값, 기간, 범주형 상태를 가져올 때 사용한다."
        ),
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "조회할 필드명. 예: hba1c, egfr, bmi",
                        "minItems": 1,
                        "maxItems": 12,
                    }
                },
                "required": ["fields"],
            }
        },
    }
}

#: 에이전트 루프에서 모델이 호출할 수 있는 도구.
AGENT_TOOLS: list[dict[str, Any]] = [
    EVIDENCE_RETRIEVAL_SPEC,
    TIMELINE_GRAPH_SPEC,
]

#: 모델 호출이 허용된 도구 이름. Gateway 앞단에서 한 번 더 걸러낸다.
ALLOWED_TOOL_NAMES: frozenset[str] = frozenset(
    spec["toolSpec"]["name"] for spec in AGENT_TOOLS
)


def tool_names() -> list[str]:
    return sorted(ALLOWED_TOOL_NAMES)
