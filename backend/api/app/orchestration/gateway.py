"""AgentCore Gateway: Tool Registry 와 권한 통제.

모든 Tool 호출은 이 게이트웨이를 지나야 한다. Rule Evaluator 처럼 다른 Tool 의
출력을 입력으로 받는 경우에도 직접 호출하지 않고 여기를 경유해, 권한 검사와
호출 로깅이 빠지지 않도록 한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..safety.observability import TraceCollector
from ..tools.base import Action, DataStore, Permission, PermissionDenied, Tool, ToolContext


class ToolNotRegistered(KeyError):
    """등록되지 않은 Tool 을 호출했을 때 발생한다."""

    def __init__(self, name: str) -> None:
        super().__init__(f"등록되지 않은 Tool 입니다: {name}")
        self.tool_name = name


@dataclass(frozen=True)
class ToolCallRecord:
    """게이트웨이가 남기는 호출 기록."""

    tool_name: str
    granted: tuple[str, ...]
    ok: bool
    detail: str | None = None


class ToolGateway:
    """Tool 등록·권한 검사·호출 추적을 담당한다."""

    def __init__(self, trace: TraceCollector) -> None:
        self._tools: dict[str, Tool] = {}
        self._policy: dict[str, frozenset[Permission]] = {}
        self._trace = trace
        self._calls: dict[str, list[ToolCallRecord]] = {}

    def register(self, tool: Tool, *, allow: tuple[Permission, ...] | None = None) -> None:
        """Tool 을 등록한다. allow 를 주면 선언된 권한을 그 범위로 더 좁힌다."""
        declared = frozenset(tool.permissions)
        effective = declared if allow is None else declared & frozenset(allow)
        if allow is not None:
            excess = frozenset(allow) - declared
            if excess:
                raise ValueError(
                    f"Tool '{tool.name}' 이 선언하지 않은 권한을 허용할 수 없습니다: "
                    f"{sorted(str(item) for item in excess)}"
                )
        self._tools[tool.name] = tool
        self._policy[tool.name] = effective

    def registry(self) -> list[dict[str, Any]]:
        """등록된 Tool 과 유효 권한 목록. 감사·문서화 용도."""
        return [
            {
                "tool_name": name,
                "permissions": sorted(str(item) for item in self._policy[name]),
            }
            for name in sorted(self._tools)
        ]

    def invoke(
        self,
        tool_name: str,
        context: ToolContext,
        /,
        **kwargs: Any,
    ) -> Any:
        """권한을 검사한 뒤 Tool 을 호출하고 스팬을 남긴다."""
        tool = self._tools.get(tool_name)
        if tool is None:
            raise ToolNotRegistered(tool_name)

        granted = self._policy[tool_name]
        self._enforce(tool_name, tool, granted)

        with self._trace.span(
            context.run_id,
            f"tool:{tool_name}",
            "TOOL",
            tool_name=tool_name,
            person_id=context.person_id,
            trial_id=context.trial_id,
            permissions=sorted(str(item) for item in granted),
        ) as attributes:
            try:
                result = tool.invoke(context, **kwargs)
            except PermissionDenied as exc:
                self._record(
                    context.run_id,
                    ToolCallRecord(tool_name, self._as_strings(granted), False, str(exc)),
                )
                raise
            attributes["result_kind"] = type(result).__name__
            if isinstance(result, (list, dict)):
                attributes["result_size"] = len(result)

        self._record(
            context.run_id, ToolCallRecord(tool_name, self._as_strings(granted), True)
        )
        return result

    def _enforce(
        self, tool_name: str, tool: Tool, granted: frozenset[Permission]
    ) -> None:
        """Tool 이 선언한 권한이 정책 범위를 벗어나지 않는지 확인한다."""
        for permission in tool.permissions:
            if permission not in granted:
                raise PermissionDenied(tool_name, permission)

    @staticmethod
    def _as_strings(permissions: frozenset[Permission]) -> tuple[str, ...]:
        return tuple(sorted(str(item) for item in permissions))

    def _record(self, run_id: str, record: ToolCallRecord) -> None:
        self._calls.setdefault(run_id, []).append(record)

    def calls_for(self, run_id: str) -> list[ToolCallRecord]:
        return list(self._calls.get(run_id, []))


def default_policy() -> dict[str, tuple[Permission, ...]]:
    """Tool 별 최소 권한 정책. IAM Role 분리와 1:1로 대응한다."""
    return {
        "criteria_tool": (Permission(DataStore.CRITERIA_TABLE, Action.READ),),
        "evidence_retrieval_tool": (Permission(DataStore.KNOWLEDGE_BASE, Action.READ),),
        "timeline_graph_tool": (Permission(DataStore.TIMELINE_GRAPH, Action.READ),),
        "rule_evaluator": (),
    }
