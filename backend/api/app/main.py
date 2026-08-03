from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware

from agent.toolspec import tool_names
from .config import settings
from .container import Container, build_container
from .orchestration.runtime import PatientNotFound, TrialNotFound
from .repository import DatasetRepository
from .schemas import (
    AnswerRequest,
    AnswerResponse,
    ArchitectureResponse,
    AuditEventOut,
    CohortResponse,
    CohortRunRequest,
    EvidencePacketOut,
    EvidenceRequestOut,
    HealthResponse,
    IntakeRequest,
    IntakeResultOut,
    PatientDetail,
    PatientListResponse,
    ReviewDecisionRequest,
    ReviewTicketOut,
    ScreeningRequest,
    ScreeningResult,
    ScreeningRunRequest,
    ScreeningRunResponse,
    TimelineResponse,
    TrialDetail,
    TrialSummary,
)

_LIMITATIONS = [
    "합성 데이터 기반의 사전 스크리닝 결과이며 의료적 판단이 아닙니다.",
    "판정은 인덱스 방문 시점의 관찰값을 기준으로 계산되었습니다.",
    "Bedrock FM 검증은 아직 로컬 규칙 검증기로 대체되어 있습니다.",
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.container = build_container(settings.data_dir)
    app.state.repository = app.state.container.repository
    yield


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description=(
        "임상시험 적격성 스크리닝 오케스트레이터입니다. "
        "기준별 근거를 수집·검증한 뒤 다섯 가지 상태로 판정하고, "
        "근거 패킷·확인 질문·대상별 설명을 함께 반환합니다."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_container(request: Request) -> Container:
    return request.app.state.container


def get_repository(request: Request) -> DatasetRepository:
    return request.app.state.container.repository


Ctx = Annotated[Container, Depends(get_container)]
Repository = Annotated[DatasetRepository, Depends(get_repository)]


@app.get("/", include_in_schema=False)
def root() -> dict[str, str]:
    return {"service": settings.app_name, "docs": "/docs", "health": "/health"}


@app.get("/health", response_model=HealthResponse, tags=["system"])
def health(repository: Repository) -> dict:
    return {
        "status": "ok",
        "service": settings.app_name,
        "version": settings.app_version,
        "data_counts": repository.counts(),
        "rag_status": "not_configured",
        "graph_status": "not_configured",
    }


@app.get(
    "/api/v1/architecture",
    response_model=ArchitectureResponse,
    tags=["system"],
)
def architecture(container: Ctx) -> dict:
    """등록된 Tool 과 유효 권한, 저장소 카운트, 에이전트 상태를 반환한다."""
    agent_status = {
        **container.agent.__dict__,
        "managed_agents": [
            item.__dict__ for item in container.agent.managed_agents
        ],
        "exposed_tools": tool_names(),
    }
    return {
        "tools": container.gateway.registry(),
        "stores": container.run_store.counts(),
        "audit_events": container.audit.size,
        "agent": agent_status,
    }


# ---------------------------------------------------------------------------
# 환자 · 시험 조회
# ---------------------------------------------------------------------------


@app.get("/api/v1/patients", response_model=PatientListResponse, tags=["patients"])
def list_patients(
    repository: Repository,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict:
    items, total = repository.list_patients(offset=offset, limit=limit)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@app.get("/api/v1/patients/{person_id}", response_model=PatientDetail, tags=["patients"])
def get_patient(person_id: int, repository: Repository) -> dict:
    patient = repository.patients.get(person_id)
    if not patient:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found")
    events = repository.timelines.get(person_id, [])
    latest = repository.timeline_event(events[-1]) if events else None
    return {"patient": repository.patient_summary(patient), "latest_event": latest}


@app.get(
    "/api/v1/patients/{person_id}/timeline",
    response_model=TimelineResponse,
    tags=["patients"],
)
def get_patient_timeline(person_id: int, repository: Repository) -> dict:
    if person_id not in repository.patients:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found")
    events = [repository.timeline_event(row) for row in repository.timelines[person_id]]
    return {"person_id": person_id, "events": events}


@app.get("/api/v1/trials", response_model=list[TrialSummary], tags=["trials"])
def list_trials(repository: Repository) -> list[dict]:
    return repository.list_trials()


@app.get("/api/v1/trials/{trial_id}", response_model=TrialDetail, tags=["trials"])
def get_trial(trial_id: str, repository: Repository) -> dict:
    trial = repository.trials.get(trial_id)
    if not trial:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trial not found")
    return {
        **repository.trial_summary(trial),
        "criteria": repository.trial_criteria(trial_id),
    }


# ---------------------------------------------------------------------------
# 스크리닝 실행 (3~9계층)
# ---------------------------------------------------------------------------


def _run_response(container: Container, output) -> dict:
    """ScreeningOutput 을 API 응답 형태로 변환한다."""
    outcome = output.outcome
    return {
        "run_id": output.run.run_id,
        "person_id": output.run.person_id,
        "trial_id": output.run.trial_id,
        "criteria_version": output.run.criteria_version,
        "index_encounter_id": output.run.index_encounter_id,
        "index_date": output.run.index_date,
        "eligibility_status": str(outcome.eligibility_status),
        "decision_label": outcome.decision_label,
        "criteria_total": outcome.criteria_total,
        "criteria_met": outcome.criteria_met,
        "blocking_criteria": list(outcome.blocking_criteria),
        "open_criteria": list(outcome.open_criteria),
        "review_criteria": list(outcome.review_criteria),
        "review_ticket_id": output.review_ticket_id,
        "mode": output.run.metadata.get("mode", "deterministic"),
        "agent": output.run.metadata.get("agent", {}),
        "packet": output.packet,
        "requests": output.requests,
        "explanations": output.explanations,
        "trace": output.trace_summary,
        "limitations": _LIMITATIONS,
    }


@app.post(
    "/api/v1/screening/run",
    response_model=ScreeningRunResponse,
    tags=["screening"],
)
def run_screening(payload: ScreeningRunRequest, container: Ctx) -> dict:
    """환자 x 시험 한 건을 실행하고 근거·질문·설명을 반환한다."""
    if payload.person_id not in container.repository.patients:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found")
    if payload.trial_id not in container.repository.trials:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trial not found")
    try:
        output = container.orchestrator.run(
            person_id=payload.person_id,
            trial_id=payload.trial_id,
            actor=payload.actor,
        )
    except PatientNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except TrialNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    return _run_response(container, output)


@app.get(
    "/api/v1/screening/{run_id}",
    response_model=ScreeningRunResponse,
    tags=["screening"],
)
def get_screening(run_id: str, container: Ctx) -> dict:
    """저장된 실행 결과를 재조회한다."""
    run = container.run_store.get_run(run_id)
    artifacts = container.run_store.get_artifacts(run_id)
    if run is None or artifacts is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

    packet = artifacts.packet
    return {
        "run_id": run.run_id,
        "person_id": run.person_id,
        "trial_id": run.trial_id,
        "criteria_version": run.criteria_version,
        "index_encounter_id": run.index_encounter_id,
        "index_date": run.index_date,
        "eligibility_status": run.eligibility_status,
        "decision_label": packet.get("decision_label", ""),
        "criteria_total": packet.get("criteria_total", 0),
        "criteria_met": packet.get("criteria_met", 0),
        "blocking_criteria": packet.get("uncertainty", {}).get("blocking_criteria", []),
        "open_criteria": packet.get("uncertainty", {}).get("open_criteria", []),
        "review_criteria": packet.get("uncertainty", {}).get("review_criteria", []),
        "review_ticket_id": next(
            (
                ticket.ticket_id
                for ticket in container.run_store.list_tickets()
                if ticket.run_id == run_id
            ),
            None,
        ),
        "mode": run.metadata.get("mode", "deterministic"),
        "agent": run.metadata.get("agent", {}),
        "packet": packet,
        "requests": artifacts.requests,
        "explanations": artifacts.explanations,
        "trace": container.trace.summary_for(run_id),
        "limitations": _LIMITATIONS,
    }


@app.get(
    "/api/v1/screening/{run_id}/evidence",
    response_model=EvidencePacketOut,
    tags=["screening"],
)
def get_screening_evidence(run_id: str, container: Ctx) -> dict:
    """기준별 근거 상세만 반환한다. 관리자 근거 패널용."""
    artifacts = container.run_store.get_artifacts(run_id)
    if artifacts is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    return artifacts.packet


@app.get(
    "/api/v1/screening/{run_id}/trace",
    tags=["screening"],
)
def get_screening_trace(run_id: str, container: Ctx) -> dict:
    """Agent · Tool · Model 스팬 전체. Observability 확인용."""
    if container.run_store.get_run(run_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    return {
        "summary": container.trace.summary_for(run_id),
        "spans": container.trace.spans_for(run_id),
        "tool_calls": [
            {
                "tool_name": record.tool_name,
                "granted": list(record.granted),
                "ok": record.ok,
                "detail": record.detail,
            }
            for record in container.gateway.calls_for(run_id)
        ],
    }


# ---------------------------------------------------------------------------
# 코호트 (8계층 Cohort Selector)
# ---------------------------------------------------------------------------


@app.post("/api/v1/cohort/run", response_model=CohortResponse, tags=["cohort"])
def run_cohort(payload: CohortRunRequest, container: Ctx) -> dict:
    """여러 환자를 배치 실행한 뒤 퍼널·병목을 집계한다."""
    if payload.trial_id not in container.repository.trials:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trial not found")

    person_ids = payload.person_ids
    if not person_ids:
        person_ids = sorted(container.repository.patients)[: payload.limit]

    container.orchestrator.run_batch(
        person_ids=person_ids, trial_id=payload.trial_id, actor=payload.actor
    )
    view = container.cohort_selector.build(
        payload.trial_id, container.run_store.runs_for_trial(payload.trial_id)
    )
    return view.__dict__


@app.get("/api/v1/cohort/{trial_id}", response_model=CohortResponse, tags=["cohort"])
def get_cohort(trial_id: str, container: Ctx) -> dict:
    """이미 실행된 결과로 코호트 현황을 조회한다."""
    if trial_id not in container.repository.trials:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trial not found")
    view = container.cohort_selector.build(
        trial_id, container.run_store.runs_for_trial(trial_id)
    )
    return view.__dict__


# ---------------------------------------------------------------------------
# 참여자 확인 질문 · 답변
# ---------------------------------------------------------------------------


@app.get(
    "/api/v1/patients/{person_id}/questions",
    response_model=list[EvidenceRequestOut],
    tags=["participant"],
)
def get_patient_questions(
    person_id: int,
    container: Ctx,
    trial_id: Annotated[str | None, Query()] = None,
) -> list[dict]:
    """참여자에게 보낼 확인 질문. 최신 실행 기준."""
    runs = container.run_store.runs_for_person(person_id)
    if trial_id:
        runs = [run for run in runs if run.trial_id == trial_id]
    if not runs:
        return []
    latest = max(runs, key=lambda run: run.started_at)
    artifacts = container.run_store.get_artifacts(latest.run_id)
    if artifacts is None:
        return []
    return [
        item for item in artifacts.requests if item.get("target") == "participant"
    ]


@app.post(
    "/api/v1/patients/{person_id}/answers",
    response_model=AnswerResponse,
    tags=["participant"],
)
def submit_answer(person_id: int, payload: AnswerRequest, container: Ctx) -> dict:
    """확인 질문에 대한 답변을 저장하고 감사 이벤트를 남긴다."""
    run = container.run_store.get_run(payload.run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    if run.person_id != person_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Run does not belong to this patient",
        )

    answer = container.run_store.add_answer(
        run_id=payload.run_id,
        person_id=person_id,
        criterion_id=payload.criterion_id,
        value=payload.value,
        submitted_by=payload.submitted_by,
        request_id=payload.request_id,
    )
    # 자유 문장 답변을 이벤트로 정규화한다. 원문은 그대로 보관하고 해석은 덧붙인다.
    # 기준일은 실행의 인덱스 방문일이다. '3개월 전' 같은 표현이 판정 시점을
    # 기준으로 계산되어야 하기 때문이다.
    intake = container.intake.normalize(
        payload.value,
        reference_date=run.index_date,
        run_id=payload.run_id,
    )
    container.audit.record(
        "ANSWER_SUBMITTED",
        actor=payload.submitted_by,
        run_id=payload.run_id,
        person_id=person_id,
        trial_id=run.trial_id,
        criterion_id=payload.criterion_id,
        answer_id=answer.answer_id,
        intake_event_count=len(intake.events),
        intake_needs_review=intake.needs_review,
    )
    return {**answer.to_dict(), "intake": intake.to_dict()}


@app.post(
    "/api/v1/intake/normalize",
    response_model=IntakeResultOut,
    tags=["intake"],
)
def normalize_intake(payload: IntakeRequest, container: Ctx) -> dict:
    """자유 문장을 측정값·약물·이상반응·상태 이벤트로 정규화한다.

    판정하지 않는다. 문장에 적힌 사실만 구조화해 돌려준다. 근거 구간이 원문에
    없는 항목은 통과시키지 않고 `dropped` 에 이유와 함께 남긴다.
    """
    result = container.intake.normalize(
        payload.text, reference_date=payload.reference_date
    )
    return result.to_dict()


# ---------------------------------------------------------------------------
# 검토 큐
# ---------------------------------------------------------------------------


@app.get(
    "/api/v1/review-queue",
    response_model=list[ReviewTicketOut],
    tags=["review"],
)
def list_review_queue(
    container: Ctx,
    ticket_status: Annotated[str | None, Query(alias="status")] = None,
    trial_id: Annotated[str | None, Query()] = None,
) -> list[dict]:
    tickets = container.run_store.list_tickets(
        status=ticket_status, trial_id=trial_id
    )
    return [ticket.to_dict() for ticket in tickets]


@app.patch(
    "/api/v1/review-queue/{ticket_id}",
    response_model=ReviewTicketOut,
    tags=["review"],
)
def decide_review(
    ticket_id: str, payload: ReviewDecisionRequest, container: Ctx
) -> dict:
    """검토 항목을 승인·반려·재실행 요청으로 처리한다."""
    ticket = container.run_store.decide_ticket(
        ticket_id,
        decision=payload.decision,
        decided_by=payload.decided_by,
        note=payload.note,
    )
    if ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")
    container.audit.record(
        "REVIEW_DECIDED",
        actor=payload.decided_by,
        run_id=ticket.run_id,
        person_id=ticket.person_id,
        trial_id=ticket.trial_id,
        ticket_id=ticket.ticket_id,
        status=ticket.status,
        note=payload.note,
    )
    return ticket.to_dict()


# ---------------------------------------------------------------------------
# 감사 로그
# ---------------------------------------------------------------------------


@app.get(
    "/api/v1/audit/{run_id}",
    response_model=list[AuditEventOut],
    tags=["audit"],
)
def get_run_audit(run_id: str, container: Ctx) -> list[dict]:
    """실행 한 건의 판정 이력 전체."""
    events = container.audit.for_run(run_id)
    if not events:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No audit events")
    return events


@app.get("/api/v1/audit", response_model=list[AuditEventOut], tags=["audit"])
def query_audit(
    container: Ctx,
    person_id: Annotated[int | None, Query(gt=0)] = None,
    trial_id: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[dict]:
    return container.audit.query(
        person_id=person_id, trial_id=trial_id, limit=limit
    )


# ---------------------------------------------------------------------------
# v0.1 호환 엔드포인트 (데이터셋 스냅샷 판정)
# ---------------------------------------------------------------------------


@app.post(
    "/api/v1/screenings",
    response_model=ScreeningResult,
    tags=["screening (v0.1 호환)"],
    deprecated=True,
)
def screen_patient(payload: ScreeningRequest, repository: Repository) -> dict:
    """생성 데이터셋의 저장된 판정 스냅샷을 반환한다.

    v0.2 오케스트레이터(`POST /api/v1/screening/run`)로 대체되었다.
    기존 클라이언트 호환을 위해 유지한다.
    """
    if payload.person_id not in repository.patients:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found")
    if payload.trial_id not in repository.trials:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trial not found")

    result = repository.screening(payload.person_id, payload.trial_id)
    if not result:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No screening snapshot exists for this patient and trial",
        )

    decision_labels = {
        "ELIGIBLE": "사전 적합",
        "INELIGIBLE": "사전 부적합",
        "UNKNOWN": "추가 정보 필요",
    }
    if result["eligibility_status"] == "ELIGIBLE":
        next_actions = ["연구 담당자의 최종 검토와 참여 동의 절차를 진행하세요."]
    elif result["eligibility_status"] == "INELIGIBLE":
        failed = ", ".join(result["failed_criteria"])
        next_actions = [f"미충족 기준({failed})과 원본 기록을 연구 담당자가 확인하세요."]
    else:
        next_actions = ["UNKNOWN 기준에 필요한 추가 검사나 기록을 확인하세요."]

    return {
        "screening_id": (
            f"SCR-{payload.person_id}-{payload.trial_id}-{result['index_encounter_id']}"
        ),
        "person_id": payload.person_id,
        "trial_id": payload.trial_id,
        **result,
        "decision_label": decision_labels[result["eligibility_status"]],
        "next_actions": next_actions,
        "limitations": [
            "합성 데이터 기반의 사전 스크리닝 결과이며 의료적 판단이 아닙니다.",
            "현재 결과는 저장된 규칙 판정 스냅샷이며 Bedrock/RAG 판정이 아닙니다.",
        ],
        "evaluated_by": "deterministic_dataset_snapshot",
        "metadata": {"rag_used": False, "graph_used": False},
    }
