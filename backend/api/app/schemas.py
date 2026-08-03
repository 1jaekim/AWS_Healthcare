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
    rag_status: Literal["not_configured"]
    graph_status: Literal["not_configured"]


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
    decision_label: str
    criteria_total: int
    criteria_met: int
    blocking_criteria: list[str]
    open_criteria: list[str]
    review_criteria: list[str]
    review_ticket_id: str | None = None
    mode: str = "deterministic"
    agent: dict[str, Any] = Field(default_factory=dict)
    packet: EvidencePacketOut
    requests: list[EvidenceRequestOut]
    explanations: dict[str, ExplanationOut]
    trace: TraceSummary
    limitations: list[str]


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


class ArchitectureResponse(BaseModel):
    """등록된 Tool 과 유효 권한. 감사·문서화 용도."""

    tools: list[ToolRegistryEntry]
    stores: dict[str, int]
    audit_events: int
    agent: AgentStatusOut
