"""Tool 계약과 권한 선언.

각 Tool 은 자신이 접근할 저장소와 동작(읽기/쓰기)을 선언한다.
Gateway 가 이 선언을 근거로 호출을 허용하거나 거부한다.
IAM Role 분리를 코드 레벨에서 미리 강제해 두는 장치다.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from agent.contracts import ToolPermissionDenied


class DataStore(StrEnum):
    """접근 대상 저장소. 실제 배포 시 IAM 리소스에 매핑된다."""

    CRITERIA_TABLE = "dynamodb:trial_definitions"
    RUN_TABLE = "dynamodb:screening_executions"
    KNOWLEDGE_BASE = "bedrock:knowledge_base"
    TIMELINE_GRAPH = "neptune:patient_timeline"
    OBJECT_STORE = "s3:artifacts"


class Action(StrEnum):
    READ = "READ"
    WRITE = "WRITE"


@dataclass(frozen=True)
class Permission:
    """Tool 이 요구하는 최소 권한 한 건."""

    store: DataStore
    action: Action

    def __str__(self) -> str:
        return f"{self.action}:{self.store}"


class PermissionDenied(ToolPermissionDenied):
    """Gateway 가 선언되지 않은 접근을 막았을 때 발생한다.

    에이전트 계층이 잡을 수 있도록 `agent.contracts.ToolPermissionDenied` 를
    상속한다. 에이전트는 권한 거부라는 사실만 알고, 어떤 저장소 권한이었는지는
    이 계층의 관심사로 남긴다.
    """

    def __init__(self, tool_name: str, permission: Permission) -> None:
        super().__init__(
            f"Tool '{tool_name}' 에 허용되지 않은 접근입니다: {permission}"
        )
        self.tool_name = tool_name
        self.permission = permission


@dataclass(frozen=True)
class ToolContext:
    """Tool 호출 단위 컨텍스트. 실행 추적에 필요한 식별자를 전달한다."""

    run_id: str
    person_id: int
    trial_id: str
    index_encounter_id: str
    index_date: str


@runtime_checkable
class Tool(Protocol):
    """Gateway 에 등록되는 Tool 의 최소 계약."""

    name: str
    permissions: tuple[Permission, ...]

    def invoke(self, context: ToolContext, /, **kwargs: Any) -> Any: ...


class BaseTool:
    """권한 선언과 자체 점검을 공유하는 Tool 기반 클래스."""

    name: str = "base"
    permissions: tuple[Permission, ...] = ()

    def assert_allowed(self, permission: Permission) -> None:
        """Tool 내부에서 저장소 접근 직전에 스스로 검사한다."""
        if permission not in self.permissions:
            raise PermissionDenied(self.name, permission)

    def invoke(self, context: ToolContext, /, **kwargs: Any) -> Any:
        raise NotImplementedError
