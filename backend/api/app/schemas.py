from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class DataCounts(BaseModel):
    patients: int
    encounters: int
    canonical_notes: int
    trials: int
    criteria: int


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str
    version: str
    data_counts: DataCounts
    rag_status: Literal["local_keyword", "bedrock_graphrag"]
    graph_status: Literal["not_configured", "configured"]
    auth_mode: Literal["cognito", "open", "blocked"]
    """`cognito` 면 토큰이 필요하다. 프론트엔드가 안내 문구를 가르는 데 쓴다."""


class PrincipalOut(BaseModel):
    """검증된 토큰에서 파생된 요청 주체. 임상 정보는 담지 않는다."""

    subject: str
    email: str | None = None
    groups: list[str] = Field(default_factory=list)
    person_id: int | None = None
    token_use: str
    is_admin: bool
    anonymous: bool


class AuthStatusResponse(BaseModel):
    """인증 설정 요약. 비밀값은 담지 않는다."""

    mode: Literal["cognito", "open", "blocked"]
    user_pool_id: str | None = None
    issuer: str | None = None
    admin_group: str
    client_count: int


class PatientSummary(BaseModel):
    person_id: int
    sex: str
    birth_date: str
    followup_start_date: str
    index_date: str
    encounter_count: int
    synthetic_followup_count: int
    data_label: str


class PatientListResponse(BaseModel):
    items: list[PatientSummary]
    total: int
    limit: int
    offset: int


class Measurements(BaseModel):
    hba1c_pct: float | None = None
    fasting_glucose_mg_dl: float | None = None
    random_glucose_mg_dl: float | None = None
    bmi_kg_m2: float | None = None
    systolic_bp_mmhg: float | None = None
    diastolic_bp_mmhg: float | None = None
    creatinine_mg_dl: float | None = None
    egfr_ml_min_1_73m2: float | None = None
    uacr_mg_g: float | None = None


class TimelineEvent(BaseModel):
    encounter_id: str
    sequence_no: int
    encounter_date: str
    synthetic_visit: bool
    age: int
    diabetes_status: str
    measurements: Measurements
    regimen: str
    adherence_level: str
    stable_regimen_days: int
    treatment_change: str
    conditions: list[str]
    medications: list[str]
    canonical_note: str | None = None


class PatientDetail(BaseModel):
    patient: PatientSummary
    latest_event: TimelineEvent | None


class TimelineResponse(BaseModel):
    person_id: int
    events: list[TimelineEvent]


class TrialCriterion(BaseModel):
    criterion_id: str
    criterion_type: Literal["INCLUSION", "EXCLUSION"]
    field: str
    operator: str
    value_low: str | None = None
    value_high: str | None = None
    unit: str | None = None


class TrialSummary(BaseModel):
    trial_id: str
    trial_name: str
    description: str
    purpose: str
    synthetic_trial: bool
    criteria_count: int


class TrialDetail(TrialSummary):
    criteria: list[TrialCriterion]


class ScreeningRequest(BaseModel):
    person_id: int = Field(gt=0)
    trial_id: str = Field(min_length=1)


class CriterionEvidence(BaseModel):
    criterion_id: str
    criterion_type: str
    criterion_name: str
    status: str
    observed_value: str | None = None
    expected_condition: str | None = None
    unit: str | None = None
    snapshot_date: str
    encounter_id: str


class ScreeningResult(BaseModel):
    screening_id: str
    person_id: int
    trial_id: str
    index_encounter_id: str
    index_date: str
    eligibility_status: Literal["ELIGIBLE", "INELIGIBLE", "UNKNOWN"]
    decision_label: str
    criteria_passed: int
    criteria_total: int
    failed_criteria: list[str]
    evidence: list[CriterionEvidence]
    next_actions: list[str]
    limitations: list[str]
    evaluated_by: Literal["deterministic_dataset_snapshot"]
    metadata: dict[str, Any] = Field(default_factory=dict)



# ---------------------------------------------------------------------------
# v0.2 — 9계층 스크리닝 오케스트레이터 응답 모델
# ---------------------------------------------------------------------------


class NarrativeEvidenceItem(BaseModel):
    """자유서술 근거 문장."""

    note_id: str
    encounter_id: str
    note_date: str
    snippet: str
    matched_terms: list[str]
    score: float


class PacketItem(BaseModel):
    """기준 한 건의 근거 표시 항목."""

    criterion_id: str
    criterion_type: str
    label: str
    field: str
    kind: str
    status: str
    status_label: str
    observed_value: str | None = None
    expected_condition: str
    unit: str | None = None
    observed_at: str | None = None
    confidence: float
    confidence_band: str
    explanation: str
    source_ids: list[str]
    conflicts: list[str] = Field(default_factory=list)
    narrative: list[NarrativeEvidenceItem] = Field(default_factory=list)


class PacketUncertainty(BaseModel):
    blocking_criteria: list[str]
    open_criteria: list[str]
    review_criteria: list[str]
    unresolved_count: int


class EvidencePacketOut(BaseModel):
    run_id: str
    person_id: int
    trial_id: str
    criteria_version: str
    index_encounter_id: str
    index_date: str
    eligibility_status: str
    decision_label: str
    criteria_total: int
    criteria_met: int
    items: list[PacketItem]
    uncertainty: PacketUncertainty


class EvidenceRequestOut(BaseModel):
    """Next-Best-Evidence 가 제안한 확인 항목."""

    request_id: str
    criterion_id: str
    field: str
    label: str
    reason: str
    question: str
    information_value: float
    effort: float
    priority: int
    target: Literal["participant", "researcher"]


class ExplanationOut(BaseModel):
    audience: str
    summary: str
    highlights: list[str]
    blocked: bool
    guardrail_findings: list[dict[str, Any]] = Field(default_factory=list)


class TraceSummary(BaseModel):
    run_id: str
    span_count: int
    total_duration_ms: float
    error_count: int
    by_kind: dict[str, Any]


class ScreeningRunRequest(BaseModel):
    person_id: int = Field(gt=0)
    trial_id: str = Field(min_length=1)
    actor: str = Field(default="system", min_length=1)


class ScreeningRunResponse(BaseModel):
    """POST /screening/run 응답."""

    run_id: str
    person_id: int
    trial_id: str
    criteria_version: str
    index_encounter_id: str
    index_date: str
    eligibility_status: Literal[
        "ELIGIBLE", "INELIGIBLE", "NEEDS_MORE_EVIDENCE", "REVIEW_REQUIRED"
    ]
    screening_decision: Literal["OK", "NOT_OK", "UNKNOWN"]
    decision_label: str
    criteria_total: int
    criteria_met: int
    blocking_criteria: list[str]
    open_criteria: list[str]
    review_criteria: list[str]
    review_ticket_id: str | None = None
    mode: str = "deterministic"
    agent: dict[str, Any] = Field(default_factory=dict)
    deliberation: dict[str, Any] = Field(default_factory=dict)
    judgment: dict[str, Any] = Field(default_factory=dict)
    """기준별 LLM 판단·검증 항목·최종 경로 요약. 판단자가 꺼져 있으면 비어 있다."""
    packet: EvidencePacketOut
    requests: list[EvidenceRequestOut]
    explanations: dict[str, ExplanationOut]
    trace: TraceSummary
    supplements: dict[str, Any] | None = None
    """재판정에서 참여자 답변이 반영된 내역. 일반 실행에서는 비어 있다."""
    limitations: list[str]


class RecommendationRunRequest(BaseModel):
    """환자와 비교할 임상시험 후보군. 없으면 활성 공고 전체를 사용한다."""

    person_id: int = Field(gt=0)
    trial_ids: list[str] | None = Field(default=None, min_length=1, max_length=50)
    application_id: str | None = Field(default=None, min_length=1, max_length=128)
    top_k: int = Field(default=3, ge=1, le=20)
    actor: str = Field(default="system", min_length=1)


class RecommendationCriterionOut(BaseModel):
    criterion_id: str
    criterion_type: str
    label: str
    screening_status: Literal["OK", "NOT_OK", "UNKNOWN"]
    recommendation_status: Literal["OK", "NOT_OK", "UNKNOWN"]
    reason: str
    evidence_ids: list[str]
    a2a_applied: bool
    next_question: str | None = None


class RecommendedTrialOut(BaseModel):
    rank: int | None = None
    run_id: str
    trial_id: str
    title: str
    description: str
    rank_score: float = Field(ge=0.0, le=1.0)
    overall_status: Literal["MATCHED", "NEEDS_MORE_INFO", "EXCLUDED"]
    screening_decision: Literal["OK", "NOT_OK", "UNKNOWN"]
    recommendation_decision: Literal["OK", "NOT_OK", "UNKNOWN"]
    criteria_met: int
    criteria_total: int
    unresolved_criteria: list[str]
    human_review_required: bool
    review_ticket_id: str | None = None
    selection_reason: str
    a2a: dict[str, Any] = Field(default_factory=dict)
    criteria: list[RecommendationCriterionOut]


class RecommendationLimits(BaseModel):
    top_k: int
    a2a_max_rounds: Literal[2]
    a2a_max_criteria_per_trial: int = Field(ge=1, le=5)
    # 승인된 후보 공고가 하나도 없으면 실행한 worker 수도 0이다. 이 값은
    # 설정값이 아니라 이번 추천 실행에서 실제 사용한 worker 수이므로 0을
    # 정상적인 빈 결과로 허용한다.
    recommendation_max_workers: int = Field(ge=0, le=5)


class RecommendationRunResponse(BaseModel):
    recommendation_id: str
    person_id: int
    evaluated_trials: int
    recommended_trials: list[RecommendedTrialOut]
    excluded_trials: list[RecommendedTrialOut]
    remaining_candidate_count: int
    limits: RecommendationLimits


class CohortRunRequest(BaseModel):
    """코호트 배치 실행 요청."""

    trial_id: str = Field(min_length=1)
    person_ids: list[int] | None = None
    limit: int = Field(default=20, ge=1, le=200)
    actor: str = Field(default="system", min_length=1)


class FunnelStage(BaseModel):
    status: str
    label: str
    count: int
    share: float


class BottleneckOut(BaseModel):
    criterion_id: str
    label: str
    field: str
    evaluated: int
    contradicted: int
    unknown: int
    conflicting: int
    review_required: int
    unresolved: int
    block_rate: float


class ReviewPriorityItem(BaseModel):
    run_id: str
    person_id: int
    eligibility_status: str | None = None
    unresolved_count: int
    criteria_met: int
    criteria_total: int
    completion: float
    unresolved_criteria: list[str]


class CohortResponse(BaseModel):
    trial_id: str
    total_screened: int
    funnel: list[FunnelStage]
    bottlenecks: list[BottleneckOut]
    review_priority: list[ReviewPriorityItem]


class AnswerRequest(BaseModel):
    run_id: str = Field(min_length=1)
    criterion_id: str = Field(min_length=1)
    value: str = Field(min_length=1)
    submitted_by: str = Field(default="participant", min_length=1)
    request_id: str | None = None


class IntakeEventOut(BaseModel):
    event_type: Literal["MEASUREMENT", "MEDICATION", "ADVERSE_EVENT", "CONDITION"]
    term: str
    label: str
    value: float | str | bool | None = None
    unit: str | None = None
    occurred_at: str | None = None
    date_precision: Literal["DAY", "MONTH", "APPROX"] | None = None
    field: str | None = None
    source_span: str
    confidence: float
    needs_review: bool
    origin: Literal["rule", "model"]
    notes: list[str] = Field(default_factory=list)


class IntakeDroppedOut(BaseModel):
    term: str
    span: str
    origin: str
    reason: str


class IntakeResultOut(BaseModel):
    text: str
    reference_date: str
    mode: str
    event_count: int
    needs_review: bool
    events: list[IntakeEventOut] = Field(default_factory=list)
    dropped: list[IntakeDroppedOut] = Field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    error: str | None = None


class IntakeRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    reference_date: str | None = Field(
        default=None,
        description="상대 시점 표현의 기준일 (ISO). 없으면 오늘로 본다.",
    )


class AnswerResponse(BaseModel):
    answer_id: str
    run_id: str
    person_id: int
    criterion_id: str
    request_id: str | None = None
    value: str
    submitted_by: str
    submitted_at: str
    intake: IntakeResultOut | None = None


class ReviewTicketOut(BaseModel):
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


class ReviewDecisionRequest(BaseModel):
    decision: Literal["APPROVED", "REJECTED", "RERUN_REQUESTED"]
    decided_by: str = Field(min_length=1)
    note: str | None = None


class TrialReviewItemOut(BaseModel):
    trial_id: str
    source_key: str
    status: Literal["pending_review", "approved", "rejected"]
    trial_title: str
    condition: str
    phase: str
    intervention: str
    criteria: list[TrialCriterion]
    criteria_count: int
    created_at: int
    updated_at: int
    reviewed_at: int | None = None
    reviewed_by: str | None = None
    review_note: str | None = None


class TrialReviewDecisionRequest(BaseModel):
    source_key: str = Field(min_length=1)
    decision: Literal["approved", "rejected"]
    note: str = Field(default="", max_length=1000)


class AuditEventOut(BaseModel):
    event_id: str
    sequence_no: int
    occurred_at: str
    action: str
    run_id: str | None = None
    actor: str
    person_id: int | None = None
    trial_id: str | None = None
    criterion_id: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class ToolRegistryEntry(BaseModel):
    tool_name: str
    permissions: list[str]


class ManagedAgentOut(BaseModel):
    """에이전트 관리자에 등록된 개별 에이전트."""

    name: str
    role: str
    enabled: bool
    mode: str
    model_backed: bool = False
    exposed_tools: list[str] = Field(default_factory=list)
    source: str | None = None


class PromptVersionOut(BaseModel):
    """시스템 프롬프트 한 건의 버전 정보."""

    id: str
    version: str
    contract: str
    checksum: str
    length: int


class AgentStatusOut(BaseModel):
    """에이전트 계층 상태."""

    enabled: bool
    mode: str
    model_id: str | None = None
    region: str | None = None
    max_iterations: int
    guardrail_attached: bool
    fallback_reason: str | None = None
    exposed_tools: list[str] = Field(default_factory=list)
    managed_agents: list[ManagedAgentOut] = Field(default_factory=list)
    prompts: list[PromptVersionOut] = Field(default_factory=list)


class ArchitectureResponse(BaseModel):
    """등록된 Tool 과 유효 권한. 감사·문서화 용도."""

    tools: list[ToolRegistryEntry]
    stores: dict[str, int]
    audit_events: int
    agent: AgentStatusOut
    retrieval: dict[str, Any]
    auth: AuthStatusResponse


# ---------------------------------------------------------------------------
# 공고 기반 자연어 지원서 수집
# ---------------------------------------------------------------------------


class IntakeAdditionalField(BaseModel):
    """공고 담당 팀이 LLM 대신 직접 넘길 수도 있는 확장 필드 계약."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    type: Literal["string", "integer", "number", "boolean", "array"] = "string"
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    enum: list[str | int | float | bool] | None = None
    criterion_field: str | None = Field(
        default=None, pattern=r"^[a-z][a-z0-9_]*$"
    )
    unit: str | None = Field(default=None, min_length=1, max_length=32)


class ApplicationSchemaCreateRequest(BaseModel):
    trial_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    notice_text: str = Field(min_length=1, max_length=100_000)
    additional_fields: list[IntakeAdditionalField] | None = None


class ApplicationSchemaResponse(BaseModel):
    schema_id: str
    trial_id: str
    version: str
    base_schema_version: str
    notice_text: str
    json_schema: dict[str, Any]
    mode: str
    created_at: str


class BaseApplicationSchemaResponse(BaseModel):
    version: str
    json_schema: dict[str, Any]


class ApplicationStartRequest(BaseModel):
    schema_id: str = Field(min_length=1)
    application_text: str = Field(min_length=1, max_length=20_000)


class ApplicationAdditionalResponse(BaseModel):
    response_text: str = Field(min_length=1, max_length=10_000)


class ApplicationScreeningRequest(BaseModel):
    """완성 지원서를 기존 환자 기록과 연결해 오케스트레이터를 실행한다."""

    person_id: int = Field(gt=0)
    actor: str = Field(default="system", min_length=1, max_length=128)


class MissingApplicationField(BaseModel):
    name: str
    title: str
    description: str
    type: str


class ApplicationIntakeResponse(BaseModel):
    application_id: str
    schema_id: str
    trial_id: str
    status: Literal[
        "NEEDS_MORE_INFO", "COMPLETE", "MAX_FOLLOW_UPS_REACHED"
    ]
    data: dict[str, Any]
    missing_fields: list[MissingApplicationField]
    follow_up_prompt: str | None = None
    notice_text: str
    iteration: int
    follow_up_count: int = Field(ge=0, le=5)
    max_follow_ups: int = 5
    updated_at: str
