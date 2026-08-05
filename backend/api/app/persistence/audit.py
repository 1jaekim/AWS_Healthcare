"""실시간 감사 로그.

DynamoDB Streams -> EventBridge Pipes -> Kinesis Firehose -> S3 경로로 교체할 지점.
지금은 append-only 인메모리 스트림으로 같은 계약을 만족시킨다.
판정 이력은 수정하지 않는다. 상태가 바뀌면 새 이벤트를 추가한다.
"""

from __future__ import annotations

import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Iterable, Literal

AuditAction = Literal[
    "RUN_STARTED",
    "CRITERIA_LOADED",
    "TOOL_INVOKED",
    "TOOL_DENIED",
    "CRITERION_JUDGED",
    "CRITERION_RESOLVED",
    "RUN_COMPLETED",
    "ANSWER_SUBMITTED",
    "SUPPLEMENT_APPLIED",
    "REVIEW_DECIDED",
    "GUARDRAIL_TRIGGERED",
    "UNKNOWN_DELIBERATION_COMPLETED",
    "RECOMMENDATION_COMPLETED",
    "APPLICATION_SCREENING_LINKED",
    "TRIAL_REVIEW_DECIDED",
]


@dataclass(frozen=True)
class AuditEvent:
    """불변 감사 이벤트 한 건."""

    event_id: str
    sequence_no: int
    occurred_at: str
    action: AuditAction
    run_id: str | None
    actor: str
    person_id: int | None = None
    trial_id: str | None = None
    criterion_id: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AuditTrail:
    """append-only 감사 스트림."""

    def __init__(self, *, max_events: int = 20_000) -> None:
        self._events: deque[AuditEvent] = deque(maxlen=max_events)
        self._sequence = 0

    def record(
        self,
        action: AuditAction,
        *,
        actor: str,
        run_id: str | None = None,
        person_id: int | None = None,
        trial_id: str | None = None,
        criterion_id: str | None = None,
        **detail: Any,
    ) -> AuditEvent:
        """이벤트를 추가한다. 기존 이벤트는 절대 변경하지 않는다."""
        self._sequence += 1
        event = AuditEvent(
            event_id=f"AUD-{uuid.uuid4().hex[:14]}",
            sequence_no=self._sequence,
            occurred_at=datetime.now(UTC).isoformat(),
            action=action,
            run_id=run_id,
            actor=actor,
            person_id=person_id,
            trial_id=trial_id,
            criterion_id=criterion_id,
            detail=detail,
        )
        self._events.append(event)
        return event

    def for_run(self, run_id: str) -> list[dict[str, Any]]:
        return [
            event.to_dict() for event in self._events if event.run_id == run_id
        ]

    def query(
        self,
        *,
        person_id: int | None = None,
        trial_id: str | None = None,
        actions: Iterable[AuditAction] | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """필터 조건으로 감사 이력을 조회한다."""
        wanted = set(actions) if actions else None
        selected: list[dict[str, Any]] = []
        for event in reversed(self._events):
            if person_id is not None and event.person_id != person_id:
                continue
            if trial_id is not None and event.trial_id != trial_id:
                continue
            if wanted is not None and event.action not in wanted:
                continue
            selected.append(event.to_dict())
            if len(selected) >= limit:
                break
        return selected

    def export_ndjson(self) -> str:
        """S3 로 내보낼 NDJSON 문자열. Firehose 페이로드와 동일한 형태."""
        import json

        return "\n".join(
            json.dumps(event.to_dict(), ensure_ascii=False) for event in self._events
        )

    @property
    def size(self) -> int:
        return len(self._events)
