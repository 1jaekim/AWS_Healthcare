from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware

from agent.toolspec import tool_names
from .auth import (
    AdminUser,
    CurrentUser,
    build_auth_guard,
    ensure_person_access,
    enforce_auth,
)
from .config import settings
from .container import Container, build_container
from .criteria_repository import InvalidApproval, ReviewAlreadyDecided
from .intake import (
    ApplicationNotComplete,
    ApplicationNotFound,
    ApplicationSchemaNotFound,
    FollowUpLimitReached,
    IntakeExtractionError,
)
from .intake.screening import ApplicationSupplementBuilder
from .intake.service import InvalidGeneratedSchema
from .intake.trial_schema import TrialSchemaBuilder
from .orchestration.runtime import PatientNotFound, TrialNotFound
from .reasoning.supplements import SupplementBuilder
from .repository import DatasetRepository
from .schemas import (
    AnswerRequest,
    AnswerResponse,
    ApplicationAdditionalResponse,
    ApplicationIntakeResponse,
    ApplicationSchemaCreateRequest,
    ApplicationSchemaResponse,
    ApplicationScreeningRequest,
    ApplicationStartRequest,
    ArchitectureResponse,
    AuditEventOut,
    BaseApplicationSchemaResponse,
    CohortResponse,
    CohortRunRequest,
    EvidencePacketOut,
    EvidenceRequestOut,
    HealthResponse,
    IntakeRequest,
    IntakeResultOut,
    PatientDetail,
    PatientListResponse,
    PrincipalOut,
    RecommendationRunRequest,
    RecommendationRunResponse,
    ReviewDecisionRequest,
    ReviewTicketOut,
    ScreeningRequest,
    ScreeningResult,
    ScreeningRunRequest,
    ScreeningRunResponse,
    TimelineResponse,
    TrialDetail,
    TrialReviewDecisionRequest,
    TrialReviewItemOut,
    TrialSummary,
)

_LIMITATIONS = [
    "합성 데이터 기반의 사전 스크리닝 결과이며 의료적 판단이 아닙니다.",
    "판정은 인덱스 방문 시점의 관찰값을 기준으로 계산되었습니다.",
    "Bedrock FM 검증은 아직 로컬 규칙 검증기로 대체되어 있습니다.",
]


def _trial_catalog(container: Container):
    catalog = getattr(container, "trial_catalog", None)
    if catalog is not None:
        return catalog
    from .criteria_repository import LocalTrialCatalog

    return LocalTrialCatalog(container.repository)


logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.container = build_container(settings.data_dir)
    app.state.repository = app.state.container.repository
    # 인증 Guard 는 도메인 조립(container)과 분리한다. API 진입 계층의 관심사다.
    app.state.auth = build_auth_guard(settings.auth)
    if app.state.auth.mode == "open":
        # 무인증으로 열려 있다는 사실이 로그에 남아야 한다. 배포 환경에서
        # 설정을 빼먹고 띄운 경우를 알아챌 수 있는 유일한 신호다.
        logger.warning(
            "인증이 비활성 상태입니다. COGNITO_USER_POOL_ID / COGNITO_CLIENT_ID "
            "가 설정되지 않아 /api/v1/* 가 토큰 없이 열려 있습니다. "
            "배포 환경에서는 AUTH_REQUIRED=true 를 설정하세요."
        )
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
    # 인증을 전역 기본값으로 둔다. 새 엔드포인트가 추가될 때 보호되는 쪽이
    # 기본이어야 한다. 공개 경로는 auth/dependencies.py 의 PUBLIC_PATHS 에만 적는다.
    dependencies=[Depends(enforce_auth)],
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_allow_origins),
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
def health(request: Request, container: Ctx) -> dict:
    return {
        "status": "ok",
        "service": settings.app_name,
        "version": settings.app_version,
        "data_counts": container.repository.counts(),
        "rag_status": container.retrieval_mode,
        "graph_status": (
            "configured"
            if container.retrieval_mode == "bedrock_graphrag"
            else "not_configured"
        ),
        "auth_mode": request.app.state.auth.mode,
    }


@app.get("/api/v1/auth/me", response_model=PrincipalOut, tags=["auth"])
def whoami(principal: CurrentUser) -> dict:
    """토큰이 유효한지, 어떤 주체로 인식되는지 확인한다.

    프론트엔드가 로그인 직후 백엔드 연결을 확인하는 데 쓴다. Cognito 미설정
    개방 모드에서는 `anonymous: true` 인 개발 주체가 돌아온다.
    """
    return principal.to_dict()


@app.get(
    "/api/v1/architecture",
    response_model=ArchitectureResponse,
    tags=["system"],
)
def architecture(request: Request, container: Ctx, _: AdminUser) -> dict:
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
        "retrieval": {
            "tool": "evidence_retrieval_tool",
            "mode": container.retrieval_mode,
            "patient_key_filter_required": (
                container.retrieval_mode == "bedrock_graphrag"
            ),
        },
        "auth": request.app.state.auth.describe(),
    }


# ---------------------------------------------------------------------------
# 환자 · 시험 조회
# ---------------------------------------------------------------------------


@app.get("/api/v1/patients", response_model=PatientListResponse, tags=["patients"])
def list_patients(
    repository: Repository,
    _: AdminUser,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict:
    """전체 환자 목록. 본인 데이터가 아니므로 관리자만 조회한다."""
    items, total = repository.list_patients(offset=offset, limit=limit)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@app.get("/api/v1/patients/{person_id}", response_model=PatientDetail, tags=["patients"])
def get_patient(
    person_id: int, repository: Repository, principal: CurrentUser
) -> dict:
    ensure_person_access(principal, person_id)
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
def get_patient_timeline(
    person_id: int, repository: Repository, principal: CurrentUser
) -> dict:
    ensure_person_access(principal, person_id)
    if person_id not in repository.patients:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found")
    events = [repository.timeline_event(row) for row in repository.timelines[person_id]]
    return {"person_id": person_id, "events": events}


@app.get("/api/v1/trials", response_model=list[TrialSummary], tags=["trials"])
def list_trials(container: Ctx) -> list[dict]:
    return _trial_catalog(container).list_trials()


@app.get("/api/v1/trials/{trial_id}", response_model=TrialDetail, tags=["trials"])
def get_trial(trial_id: str, container: Ctx) -> dict:
    catalog = _trial_catalog(container)
    trial = catalog.get_trial(trial_id)
    if not trial:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trial not found")
    return {
        **trial,
        "criteria": catalog.trial_criteria(trial_id),
    }


# ---------------------------------------------------------------------------
# 공고 기반 자연어 지원서 수집
# ---------------------------------------------------------------------------


@app.get(
    "/api/v1/application-schemas/base",
    response_model=BaseApplicationSchemaResponse,
    tags=["applications"],
)
def get_base_application_schema(container: Ctx) -> dict:
    """공고 확장 전의 고정 기본 스키마 v1을 반환한다."""
    return container.application_intake.base_schema()


@app.post(
    "/api/v1/application-schemas",
    response_model=ApplicationSchemaResponse,
    tags=["applications"],
)
def create_application_schema(
    payload: ApplicationSchemaCreateRequest, container: Ctx
) -> dict:
    """기본 스키마에 공고별 필드를 추가해 버전이 고정된 스키마를 만든다."""
    try:
        return container.application_intake.generate_schema(
            trial_id=payload.trial_id,
            notice_text=payload.notice_text,
            additional_fields=(
                [item.model_dump() for item in payload.additional_fields]
                if payload.additional_fields is not None
                else None
            ),
        )
    except InvalidGeneratedSchema as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


@app.post(
    "/api/v1/trials/{trial_id}/application-schema",
    response_model=ApplicationSchemaResponse,
    tags=["applications", "trials"],
)
def prepare_trial_application_schema(trial_id: str, container: Ctx) -> dict:
    """공고의 선정·제외 기준으로 지원서 스키마를 준비한다.

    화면에서 추천 공고를 클릭하면 이 호출 하나로 챗을 시작할 수 있다. 공고문을
    클라이언트가 조립해 보내지 않아도 되고, 물어볼 항목이 기준에서 파생되므로
    수집한 값이 반드시 어떤 기준의 입력이 된다.

    같은 공고·같은 기준이면 같은 `schema_id` 가 나온다(내용 지문 기반). 여러 번
    불러도 스키마가 늘어나지 않으므로 화면이 캐시를 관리할 필요가 없다.

    공고문 자유 텍스트에서 LLM 으로 필드를 만들고 싶으면
    `POST /api/v1/application-schemas` 를 쓴다. 이 경로는 모델을 부르지 않는다.
    """
    catalog = _trial_catalog(container)
    trial = catalog.get_trial(trial_id)
    if trial is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Trial not found"
        )

    request = TrialSchemaBuilder().build(
        trial=trial,
        criteria=catalog.trial_criteria(trial_id),
    )
    try:
        return container.application_intake.generate_schema(
            trial_id=trial_id,
            notice_text=request.notice_text,
            additional_fields=request.additional_fields,
        )
    except InvalidGeneratedSchema as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


@app.get(
    "/api/v1/application-schemas/{schema_id}",
    response_model=ApplicationSchemaResponse,
    tags=["applications"],
)
def get_application_schema(schema_id: str, container: Ctx) -> dict:
    try:
        return container.application_intake.get_schema(schema_id)
    except ApplicationSchemaNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Application schema not found"
        ) from exc


@app.post(
    "/api/v1/applications",
    response_model=ApplicationIntakeResponse,
    tags=["applications"],
)
def start_application(
    payload: ApplicationStartRequest, container: Ctx, principal: CurrentUser
) -> dict:
    """첫 자연어 지원서를 추출하고 누락된 필드의 추가 작성 요청을 반환한다."""
    try:
        return container.application_intake.start_application(
            schema_id=payload.schema_id,
            application_text=payload.application_text,
            owner_sub=principal.subject,
        )
    except ApplicationSchemaNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Application schema not found"
        ) from exc
    except IntakeExtractionError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc


@app.post(
    "/api/v1/applications/{application_id}/responses",
    response_model=ApplicationIntakeResponse,
    tags=["applications"],
)
def add_application_response(
    application_id: str,
    payload: ApplicationAdditionalResponse,
    container: Ctx,
    principal: CurrentUser,
) -> dict:
    """추가 자연어 답변을 기존 값에 병합하고 완성 여부를 다시 검사한다."""
    try:
        return container.application_intake.add_response(
            application_id, payload.response_text, owner_sub=principal.subject
        )
    except ApplicationNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Application not found"
        ) from exc
    except FollowUpLimitReached as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Maximum of 5 follow-up questions has been reached",
        ) from exc
    except IntakeExtractionError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc


@app.get(
    "/api/v1/applications/{application_id}",
    response_model=ApplicationIntakeResponse,
    tags=["applications"],
)
def get_application(
    application_id: str, container: Ctx, principal: CurrentUser
) -> dict:
    try:
        return container.application_intake.get_application(
            application_id, owner_sub=principal.subject
        )
    except ApplicationNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Application not found"
        ) from exc


@app.post(
    "/api/v1/applications/{application_id}/screening",
    response_model=ScreeningRunResponse,
    tags=["applications", "screening"],
)
def screen_completed_application(
    application_id: str,
    payload: ApplicationScreeningRequest,
    container: Ctx,
    principal: CurrentUser,
) -> dict:
    """완성 지원서 JSON을 보충 근거로 넣고 GraphRAG 스크리닝을 실행한다."""
    ensure_person_access(principal, payload.person_id)
    if payload.person_id not in container.repository.patients:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found"
        )
    try:
        application, schema = container.application_intake.completed_application(
            application_id, owner_sub=principal.subject
        )
    except ApplicationNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Application not found"
        ) from exc
    except ApplicationNotComplete as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Application must be COMPLETE before screening",
        ) from exc

    trial_id = str(application["trial_id"])
    if not _trial_catalog(container).has_trial(trial_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Trial not found"
        )
    supplements = ApplicationSupplementBuilder().build(
        application=application,
        json_schema=schema["json_schema"],
    )
    try:
        output = container.orchestrator.run(
            person_id=payload.person_id,
            trial_id=trial_id,
            actor=payload.actor,
            supplements=supplements.observations,
        )
    except (PatientNotFound, TrialNotFound) as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc

    container.audit.record(
        "APPLICATION_SCREENING_LINKED",
        actor=payload.actor,
        run_id=output.run.run_id,
        person_id=payload.person_id,
        trial_id=trial_id,
        application_id=application_id,
        supplement_fields=sorted(supplements.observations),
        retrieval_mode=container.retrieval_mode,
    )

    body = _run_response(container, output)
    body["supplements"] = {
        **supplements.to_dict(),
        **output.run.metadata.get("supplements", {}),
        "source_application_id": application_id,
        "retrieval_mode": container.retrieval_mode,
    }
    return body


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
        "screening_decision": outcome.screening_decision,
        "decision_label": outcome.decision_label,
        "criteria_total": outcome.criteria_total,
        "criteria_met": outcome.criteria_met,
        "blocking_criteria": list(outcome.blocking_criteria),
        "open_criteria": list(outcome.open_criteria),
        "review_criteria": list(outcome.review_criteria),
        "review_ticket_id": output.review_ticket_id,
        "mode": output.run.metadata.get("mode", "deterministic"),
        "agent": output.run.metadata.get("agent", {}),
        "deliberation": output.run.metadata.get("deliberation", {}),
        "judgment": output.run.metadata.get("judgment", {}),
        "packet": output.packet,
        "requests": output.requests,
        "explanations": output.explanations,
        "trace": output.trace_summary,
        "limitations": _LIMITATIONS,
    }


def _screening_decision(eligibility_status: str | None) -> str:
    if eligibility_status == "ELIGIBLE":
        return "OK"
    if eligibility_status == "INELIGIBLE":
        return "NOT_OK"
    return "UNKNOWN"


@app.post(
    "/api/v1/screening/run",
    response_model=ScreeningRunResponse,
    tags=["screening"],
)
def run_screening(
    payload: ScreeningRunRequest, container: Ctx, principal: CurrentUser
) -> dict:
    """환자 x 시험 한 건을 실행하고 근거·질문·설명을 반환한다."""
    ensure_person_access(principal, payload.person_id)
    if payload.person_id not in container.repository.patients:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found")
    if not _trial_catalog(container).has_trial(payload.trial_id):
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
def get_screening(run_id: str, container: Ctx, principal: CurrentUser) -> dict:
    """저장된 실행 결과를 재조회한다."""
    run = container.run_store.get_run(run_id)
    artifacts = container.run_store.get_artifacts(run_id)
    if run is None or artifacts is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    ensure_person_access(principal, run.person_id)

    packet = artifacts.packet
    return {
        "run_id": run.run_id,
        "person_id": run.person_id,
        "trial_id": run.trial_id,
        "criteria_version": run.criteria_version,
        "index_encounter_id": run.index_encounter_id,
        "index_date": run.index_date,
        "eligibility_status": run.eligibility_status,
        "screening_decision": _screening_decision(run.eligibility_status),
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
        "deliberation": run.metadata.get("deliberation", {}),
        "judgment": run.metadata.get("judgment", {}),
        "packet": packet,
        "requests": artifacts.requests,
        "explanations": artifacts.explanations,
        "trace": container.trace.summary_for(run_id),
        "limitations": _LIMITATIONS,
    }


@app.post(
    "/api/v1/recommendations/run",
    response_model=RecommendationRunResponse,
    tags=["recommendations"],
)
def run_recommendations(
    payload: RecommendationRunRequest, container: Ctx, principal: CurrentUser
) -> dict:
    """후보 공고를 모두 판정하고 제한형 A2A 결과까지 반영해 순위를 만든다."""
    ensure_person_access(principal, payload.person_id)
    if payload.person_id not in container.repository.patients:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Patient not found",
        )

    trial_ids = list(
        dict.fromkeys(
            payload.trial_ids
            or [item["trial_id"] for item in _trial_catalog(container).list_trials()]
        )
    )
    missing = [
        trial_id
        for trial_id in trial_ids
        if not _trial_catalog(container).has_trial(trial_id)
    ]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": "Trial not found", "trial_ids": missing},
        )

    try:
        return container.recommendation_orchestrator.run(
            person_id=payload.person_id,
            trial_ids=trial_ids,
            top_k=payload.top_k,
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


@app.get(
    "/api/v1/recommendations/{recommendation_id}",
    response_model=RecommendationRunResponse,
    tags=["recommendations"],
)
def get_recommendations(
    recommendation_id: str, container: Ctx, principal: CurrentUser
) -> dict:
    """저장된 추천 결과를 재조회한다."""
    payload = container.run_store.get_recommendation(recommendation_id)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Recommendation not found",
        )
    owner = payload.get("person_id")
    if isinstance(owner, int):
        ensure_person_access(principal, owner)
    return payload


@app.get(
    "/api/v1/screening/{run_id}/evidence",
    response_model=EvidencePacketOut,
    tags=["screening"],
)
def get_screening_evidence(run_id: str, container: Ctx, _: AdminUser) -> dict:
    """기준별 근거 상세만 반환한다. 관리자 근거 패널용."""
    artifacts = container.run_store.get_artifacts(run_id)
    if artifacts is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    return artifacts.packet


@app.get(
    "/api/v1/screening/{run_id}/trace",
    tags=["screening"],
)
def get_screening_trace(run_id: str, container: Ctx, _: AdminUser) -> dict:
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
def run_cohort(payload: CohortRunRequest, container: Ctx, _: AdminUser) -> dict:
    """여러 환자를 배치 실행한 뒤 퍼널·병목을 집계한다. 연구 담당자용."""
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
def get_cohort(trial_id: str, container: Ctx, _: AdminUser) -> dict:
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
    principal: CurrentUser,
    trial_id: Annotated[str | None, Query()] = None,
) -> list[dict]:
    """참여자에게 보낼 확인 질문. 최신 실행 기준."""
    ensure_person_access(principal, person_id)
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
def submit_answer(
    person_id: int,
    payload: AnswerRequest,
    container: Ctx,
    principal: CurrentUser,
) -> dict:
    """확인 질문에 대한 답변을 저장하고 감사 이벤트를 남긴다."""
    ensure_person_access(principal, person_id)
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
    # 정규화 결과를 답변에 붙여 둔다. 재판정이 이 이벤트를 관찰값으로 승격한다.
    answer.events = [item.to_dict() for item in intake.events]
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
    "/api/v1/screening/{run_id}/rerun",
    response_model=ScreeningRunResponse,
    tags=["screening"],
)
def rerun_screening(
    run_id: str, container: Ctx, principal: CurrentUser, actor: str = "system"
) -> dict:
    """제출된 답변을 반영해 다시 판정한다.

    답변에 딸린 Intake 이벤트 중 기준 필드로 연결된 것을 관찰값으로 승격해
    그래프에 값이 없던 필드를 채운다. 기록으로 확인된 값은 덮어쓰지 않는다.

    참여자 진술은 판정을 확정하지 않는다. 미해소(UNKNOWN) 기준이 검토 필요
    (REVIEW_REQUIRED)로 올라가고, 적합·부적합 확정은 연구 담당자가 한다.
    """
    run = container.run_store.get_run(run_id)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Run not found"
        )
    ensure_person_access(principal, run.person_id)

    answers = container.run_store.answers_for(run_id)
    if not answers:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No answers submitted for this run",
        )

    supplements = SupplementBuilder().from_answers(answers)
    try:
        output = container.orchestrator.run(
            person_id=run.person_id,
            trial_id=run.trial_id,
            actor=actor,
            supplements=supplements.observations,
        )
    except (PatientNotFound, TrialNotFound) as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc

    body = _run_response(container, output)
    body["supplements"] = {
        **supplements.to_dict(),
        **output.run.metadata.get("supplements", {}),
        "source_run_id": run_id,
    }
    return body


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
    "/api/v1/admin/trials",
    response_model=list[TrialReviewItemOut],
    tags=["admin trials"],
)
def list_trial_reviews(
    container: Ctx,
    _: AdminUser,
    review_status: Annotated[
        str, Query(alias="status", pattern="^(pending_review|approved|rejected)$")
    ] = "pending_review",
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> list[dict]:
    catalog = _trial_catalog(container)
    if not hasattr(catalog, "list_for_review"):
        return []
    return catalog.list_for_review(status=review_status, limit=limit)


@app.patch(
    "/api/v1/admin/trials/{trial_id}/review",
    response_model=TrialReviewItemOut,
    tags=["admin trials"],
)
def decide_trial_review(
    trial_id: str,
    payload: TrialReviewDecisionRequest,
    container: Ctx,
    principal: AdminUser,
) -> dict:
    """관리자가 파서 결과 한 버전을 승인 또는 반려한다."""
    catalog = _trial_catalog(container)
    if not hasattr(catalog, "decide_review"):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="운영 공고 저장소가 구성되지 않았습니다.",
        )
    try:
        item = catalog.decide_review(
            trial_id=trial_id,
            source_key=payload.source_key,
            decision=payload.decision,
            reviewed_by=principal.actor,
            note=payload.note.strip(),
        )
    except InvalidApproval as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except ReviewAlreadyDecided as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="공고를 찾을 수 없습니다.")
    container.audit.record(
        "TRIAL_REVIEW_DECIDED",
        actor=principal.actor,
        trial_id=trial_id,
        source_key=payload.source_key,
        status=payload.decision,
        note=payload.note.strip(),
    )
    return item


@app.get(
    "/api/v1/review-queue",
    response_model=list[ReviewTicketOut],
    tags=["review"],
)
def list_review_queue(
    container: Ctx,
    _: AdminUser,
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
    ticket_id: str, payload: ReviewDecisionRequest, container: Ctx, _: AdminUser
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
def get_run_audit(run_id: str, container: Ctx, _: AdminUser) -> list[dict]:
    """실행 한 건의 판정 이력 전체."""
    events = container.audit.for_run(run_id)
    if not events:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No audit events")
    return events


@app.get("/api/v1/audit", response_model=list[AuditEventOut], tags=["audit"])
def query_audit(
    container: Ctx,
    _: AdminUser,
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
def screen_patient(
    payload: ScreeningRequest, repository: Repository, principal: CurrentUser
) -> dict:
    """생성 데이터셋의 저장된 판정 스냅샷을 반환한다.

    v0.2 오케스트레이터(`POST /api/v1/screening/run`)로 대체되었다.
    기존 클라이언트 호환을 위해 유지한다.
    """
    ensure_person_access(principal, payload.person_id)
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
