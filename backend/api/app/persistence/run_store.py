"""Run Store: 실행·답변·검토 상태를 보관한다.

DynamoDB screening_executions 테이블로 교체할 지점.
쓰기 경로가 있는 유일한 저장소이므로, 감사 이벤트와 함께 기록되도록 설계한다.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from ..domain.models import ScreeningRun

ReviewDecision = Literal["APPROVED", "REJECTED", "RERUN_REQUESTED"]


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class Answer:
    """참여자 또는 연구자가 제출한 확인 답변."""

    answer_id: str
    run_id: str
    person_id: int
    criterion_id: str
    request_id: str | None
    value: str
    submitted_by: str
    submitted_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "answer_id": self.answer_id,
            "run_id": self.run_id,
            "person_id": self.person_id,
            "criterion_id": self.criterion_id,
            "request_id": self.request_id,
            "value": self.value,
            "submitted_by": self.submitted_by,
            "submitted_at": self.submitted_at,
        }


@dataclass
class ReviewTicket:
    """검토 큐 항목."""

    ticket_id: str
    run_id: str
    person_id: int
    trial_id: str
    criterion_ids: list[str]
    status: Literal["PENDING", "APPROVED", "REJECTED", "RERUN_REQUESTED"]
    created_at: str
    decided_at: str | None = None
    decided_by: str | None = None
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticket_id": self.ticket_id,
            "run_id": self.run_id,
            "person_id": self.person_id,
            "trial_id": self.trial_id,
            "criterion_ids": list(self.criterion_ids),
            "status": self.status,
            "created_at": self.created_at,
            "decided_at": self.decided_at,
            "decided_by": self.decided_by,
            "note": self.note,
        }


@dataclass
class RunArtifacts:
    """실행에 딸린 산출물. 패킷·질문·설명을 함께 보관한다."""

    packet: dict[str, Any] = field(default_factory=dict)
    requests: list[dict[str, Any]] = field(default_factory=list)
    explanations: dict[str, Any] = field(default_factory=dict)


class RunStore:
    """실행 레코드와 부속 상태의 저장소."""

    def __init__(self) -> None:
        self._runs: dict[str, ScreeningRun] = {}
        self._artifacts: dict[str, RunArtifacts] = {}
        self._answers: dict[str, list[Answer]] = {}
        self._tickets: dict[str, ReviewTicket] = {}
        self._latest: dict[tuple[int, str], str] = {}

    @staticmethod
    def new_run_id() -> str:
        return f"RUN-{uuid.uuid4().hex[:16]}"

    def save_run(self, run: ScreeningRun) -> None:
        self._runs[run.run_id] = run
        self._latest[(run.person_id, run.trial_id)] = run.run_id

    def save_artifacts(
        self,
        run_id: str,
        *,
        packet: dict[str, Any],
        requests: list[dict[str, Any]],
        explanations: dict[str, Any],
    ) -> None:
        self._artifacts[run_id] = RunArtifacts(
            packet=packet, requests=requests, explanations=explanations
        )

    def get_run(self, run_id: str) -> ScreeningRun | None:
        return self._runs.get(run_id)

    def get_artifacts(self, run_id: str) -> RunArtifacts | None:
        return self._artifacts.get(run_id)

    def latest_run_id(self, person_id: int, trial_id: str) -> str | None:
        return self._latest.get((person_id, trial_id))

    def runs_for_trial(self, trial_id: str) -> list[ScreeningRun]:
        """시험별 최신 실행만 모은다. 코호트 집계에서 중복을 막는다."""
        latest_ids = {
            run_id
            for (_, tid), run_id in self._latest.items()
            if tid == trial_id
        }
        return [self._runs[run_id] for run_id in latest_ids if run_id in self._runs]

    def runs_for_person(self, person_id: int) -> list[ScreeningRun]:
        return [
            run for run in self._runs.values() if run.person_id == person_id
        ]

    def add_answer(
        self,
        *,
        run_id: str,
        person_id: int,
        criterion_id: str,
        value: str,
        submitted_by: str,
        request_id: str | None = None,
    ) -> Answer:
        answer = Answer(
            answer_id=f"ANS-{uuid.uuid4().hex[:12]}",
            run_id=run_id,
            person_id=person_id,
            criterion_id=criterion_id,
            request_id=request_id,
            value=value,
            submitted_by=submitted_by,
            submitted_at=_now(),
        )
        self._answers.setdefault(run_id, []).append(answer)
        return answer

    def answers_for(self, run_id: str) -> list[Answer]:
        return list(self._answers.get(run_id, []))

    def open_ticket(
        self, *, run_id: str, person_id: int, trial_id: str, criterion_ids: list[str]
    ) -> ReviewTicket:
        """검토 필요 실행에 대해 큐 항목을 만든다. 실행당 하나만 유지한다."""
        for ticket in self._tickets.values():
            if ticket.run_id == run_id and ticket.status == "PENDING":
                ticket.criterion_ids = criterion_ids
                return ticket
        ticket = ReviewTicket(
            ticket_id=f"RVW-{uuid.uuid4().hex[:12]}",
            run_id=run_id,
            person_id=person_id,
            trial_id=trial_id,
            criterion_ids=list(criterion_ids),
            status="PENDING",
            created_at=_now(),
        )
        self._tickets[ticket.ticket_id] = ticket
        return ticket

    def get_ticket(self, ticket_id: str) -> ReviewTicket | None:
        return self._tickets.get(ticket_id)

    def list_tickets(
        self, *, status: str | None = None, trial_id: str | None = None
    ) -> list[ReviewTicket]:
        items = [
            ticket
            for ticket in self._tickets.values()
            if (status is None or ticket.status == status)
            and (trial_id is None or ticket.trial_id == trial_id)
        ]
        items.sort(key=lambda item: item.created_at, reverse=True)
        return items

    def decide_ticket(
        self,
        ticket_id: str,
        *,
        decision: ReviewDecision,
        decided_by: str,
        note: str | None = None,
    ) -> ReviewTicket | None:
        ticket = self._tickets.get(ticket_id)
        if ticket is None:
            return None
        ticket.status = decision
        ticket.decided_at = _now()
        ticket.decided_by = decided_by
        ticket.note = note
        return ticket

    def counts(self) -> dict[str, int]:
        return {
            "runs": len(self._runs),
            "answers": sum(len(items) for items in self._answers.values()),
            "review_tickets": len(self._tickets),
            "pending_reviews": sum(
                1 for item in self._tickets.values() if item.status == "PENDING"
            ),
        }
