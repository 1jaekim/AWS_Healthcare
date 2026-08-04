/**
 * 백엔드 `backend/api/app/schemas.py` 의 Pydantic 모델과 1:1 로 대응한다.
 * 스키마가 바뀌면 이 파일도 같이 바꾼다. 여기 없는 필드는 UI 가 쓰지 않는다는 뜻이다.
 */

export type EligibilityStatus =
  | 'ELIGIBLE'
  | 'INELIGIBLE'
  | 'NEEDS_MORE_EVIDENCE'
  | 'REVIEW_REQUIRED'

/** 기준 한 건의 상태. `app/domain/states.py` 의 다섯 가지. */
export type CriterionStatus =
  | 'EVIDENCE_FOUND'
  | 'CONTRADICTED'
  | 'UNKNOWN'
  | 'CONFLICTING'
  | 'REVIEW_REQUIRED'

export interface DataCounts {
  patients: number
  encounters: number
  canonical_notes: number
  trials: number
  criteria: number
}

export interface HealthResponse {
  status: 'ok'
  service: string
  version: string
  data_counts: DataCounts
  rag_status: string
  graph_status: string
}

export interface PatientSummary {
  person_id: number
  sex: string
  birth_date: string
  followup_start_date: string
  index_date: string
  encounter_count: number
  synthetic_followup_count: number
  data_label: string
}

export interface PatientListResponse {
  items: PatientSummary[]
  total: number
  limit: number
  offset: number
}

export interface Measurements {
  hba1c_pct: number | null
  fasting_glucose_mg_dl: number | null
  random_glucose_mg_dl: number | null
  bmi_kg_m2: number | null
  systolic_bp_mmhg: number | null
  diastolic_bp_mmhg: number | null
  creatinine_mg_dl: number | null
  egfr_ml_min_1_73m2: number | null
  uacr_mg_g: number | null
}

export interface TimelineEvent {
  encounter_id: string
  sequence_no: number
  encounter_date: string
  synthetic_visit: boolean
  age: number
  diabetes_status: string
  measurements: Measurements
  regimen: string
  adherence_level: string
  stable_regimen_days: number
  treatment_change: string
  conditions: string[]
  medications: string[]
  canonical_note: string | null
}

export interface PatientDetail {
  patient: PatientSummary
  latest_event: TimelineEvent | null
}

export interface TimelineResponse {
  person_id: number
  events: TimelineEvent[]
}

export interface TrialCriterion {
  criterion_id: string
  criterion_type: 'INCLUSION' | 'EXCLUSION'
  field: string
  operator: string
  value_low: string | null
  value_high: string | null
  unit: string | null
}

export interface TrialSummary {
  trial_id: string
  trial_name: string
  description: string
  purpose: string
  synthetic_trial: boolean
  criteria_count: number
}

export interface TrialDetail extends TrialSummary {
  criteria: TrialCriterion[]
}

export interface NarrativeEvidenceItem {
  note_id: string
  encounter_id: string
  note_date: string
  snippet: string
  matched_terms: string[]
  score: number
}

export interface PacketItem {
  criterion_id: string
  criterion_type: string
  label: string
  field: string
  kind: string
  status: CriterionStatus | string
  status_label: string
  observed_value: string | null
  expected_condition: string
  unit: string | null
  observed_at: string | null
  confidence: number
  confidence_band: string
  explanation: string
  source_ids: string[]
  conflicts: string[]
  narrative: NarrativeEvidenceItem[]
}

export interface PacketUncertainty {
  blocking_criteria: string[]
  open_criteria: string[]
  review_criteria: string[]
  unresolved_count: number
}

export interface EvidencePacket {
  run_id: string
  person_id: number
  trial_id: string
  criteria_version: string
  index_encounter_id: string
  index_date: string
  eligibility_status: string
  decision_label: string
  criteria_total: number
  criteria_met: number
  items: PacketItem[]
  uncertainty: PacketUncertainty
}

export interface EvidenceRequest {
  request_id: string
  criterion_id: string
  field: string
  label: string
  reason: string
  question: string
  information_value: number
  effort: number
  priority: number
  target: 'participant' | 'researcher'
}

export interface Explanation {
  audience: string
  summary: string
  highlights: string[]
  blocked: boolean
  guardrail_findings: Record<string, unknown>[]
}

export interface TraceSummary {
  run_id: string
  span_count: number
  total_duration_ms: number
  error_count: number
  by_kind: Record<string, unknown>
}

export interface ScreeningRunRequest {
  person_id: number
  trial_id: string
  actor?: string
}

/** 종합 판정을 세 값으로 단순화한 것. 추천 정렬과 화면 표시에 쓴다. */
export type Decision = 'OK' | 'NOT_OK' | 'UNKNOWN'

export interface ScreeningRun {
  run_id: string
  person_id: number
  trial_id: string
  criteria_version: string
  index_encounter_id: string
  index_date: string
  eligibility_status: EligibilityStatus
  screening_decision: Decision
  decision_label: string
  criteria_total: number
  criteria_met: number
  blocking_criteria: string[]
  open_criteria: string[]
  review_criteria: string[]
  review_ticket_id: string | null
  mode: string
  agent: Record<string, unknown>
  /** 기준별 LLM 판단·검증 항목·최종 경로 요약. 판단자가 꺼져 있으면 비어 있다. */
  judgment: JudgmentSummary | Record<string, never>
  /** A2A 교차 검토 기록. 최대 2라운드. */
  deliberation: Record<string, unknown>
  /** 재판정에서 참여자 답변·지원서 값이 반영된 내역. 일반 실행에서는 null. */
  supplements: SupplementSummary | null
  packet: EvidencePacket
  requests: EvidenceRequest[]
  explanations: Record<string, Explanation>
  trace: TraceSummary
  limitations: string[]
}

/**
 * 기준별 판단 검증 7항목. 백엔드 `app/reasoning/judgment.py` 가 채운다.
 * ID 는 README 의 표와 같다: V-EVIDENCE / V-WINDOW / V-UNIT / V-OPERATOR /
 * V-TYPE / V-PII / V-CONFIDENCE.
 */
export interface VerificationCheck {
  id: string
  passed: boolean
  detail?: string
}

export interface JudgmentItem {
  criterion_id: string
  label?: string
  /** 모델이 제안한 값. 확정이 아니다. */
  proposed?: Decision
  /** 규칙 계층이 확정한 최종 상태. */
  final_status?: string
  /** DECIDED · A2A · HUMAN_REVIEW */
  route?: string
  used_evidence_ids?: string[]
  verification?: { checks: VerificationCheck[]; passed?: boolean }
}

export interface JudgmentSummary {
  items?: JudgmentItem[]
  [key: string]: unknown
}

export interface SupplementSummary {
  applied_fields?: string[]
  excluded_fields?: { name: string; reason: string }[]
  source_application_id?: string
  retrieval_mode?: string
  [key: string]: unknown
}

// ---------------------------------------------------------------------------
// 자연어 임상시험 지원서 (`POST /api/v1/applications` 계열)
// ---------------------------------------------------------------------------

export interface BaseApplicationSchema {
  version: string
  json_schema: Record<string, unknown>
}

export interface ApplicationSchema {
  schema_id: string
  trial_id: string
  version: string
  base_schema_version: string
  notice_text: string
  json_schema: Record<string, unknown>
  mode: string
  created_at: string
}

export interface MissingApplicationField {
  name: string
  title: string
  description: string
  type: string
}

export type ApplicationStatus =
  | 'NEEDS_MORE_INFO'
  | 'COMPLETE'
  | 'MAX_FOLLOW_UPS_REACHED'

export interface ApplicationIntake {
  application_id: string
  schema_id: string
  trial_id: string
  status: ApplicationStatus
  data: Record<string, unknown>
  missing_fields: MissingApplicationField[]
  /** 다음에 물을 문장. 종료 상태에서는 null. */
  follow_up_prompt: string | null
  notice_text: string
  iteration: number
  /** 실제로 발행한 재질문 횟수. 상한은 max_follow_ups. */
  follow_up_count: number
  max_follow_ups: number
  updated_at: string
}

// ---------------------------------------------------------------------------
// 임상시험 추천 (`POST /api/v1/recommendations/run`)
// ---------------------------------------------------------------------------

export interface RecommendationCriterion {
  criterion_id: string
  criterion_type: string
  label: string
  screening_status: Decision
  recommendation_status: Decision
  reason: string
  evidence_ids: string[]
  a2a_applied: boolean
  next_question: string | null
}

export interface RecommendedTrial {
  rank: number | null
  run_id: string
  trial_id: string
  title: string
  description: string
  /** 0.0~1.0. 모델 생성값이 아니라 코드에 고정된 보수적 점수다. */
  rank_score: number
  overall_status: 'MATCHED' | 'NEEDS_MORE_INFO' | 'EXCLUDED'
  /** 원래 스크리닝 판정. A2A 가 덮어쓰지 않는다. */
  screening_decision: Decision
  /** 추천 정렬에만 쓰는 판정. A2A 합의가 반영될 수 있다. */
  recommendation_decision: Decision
  criteria_met: number
  criteria_total: number
  unresolved_criteria: string[]
  human_review_required: boolean
  review_ticket_id: string | null
  selection_reason: string
  a2a: Record<string, unknown>
  criteria: RecommendationCriterion[]
}

export interface RecommendationLimits {
  top_k: number
  a2a_max_rounds: 2
  a2a_max_criteria_per_trial: number
}

export interface RecommendationRun {
  recommendation_id: string
  person_id: number
  evaluated_trials: number
  recommended_trials: RecommendedTrial[]
  excluded_trials: RecommendedTrial[]
  remaining_candidate_count: number
  limits: RecommendationLimits
}

export interface AnswerRequest {
  run_id: string
  criterion_id: string
  value: string
  submitted_by?: string
  request_id?: string | null
}

export type IntakeEventType =
  | 'MEASUREMENT'
  | 'MEDICATION'
  | 'ADVERSE_EVENT'
  | 'CONDITION'

export interface IntakeEvent {
  event_type: IntakeEventType
  term: string
  label: string
  value: number | string | boolean | null
  unit: string | null
  occurred_at: string | null
  date_precision: 'DAY' | 'MONTH' | 'APPROX' | null
  field: string | null
  source_span: string
  confidence: number
  needs_review: boolean
  origin: 'rule' | 'model'
  notes: string[]
}

export interface IntakeDropped {
  term: string
  span: string
  origin: string
  reason: string
}

export interface IntakeResult {
  text: string
  reference_date: string
  mode: string
  event_count: number
  needs_review: boolean
  events: IntakeEvent[]
  dropped: IntakeDropped[]
  input_tokens: number
  output_tokens: number
  error: string | null
}

export interface AnswerResponse {
  answer_id: string
  run_id: string
  person_id: number
  criterion_id: string
  request_id: string | null
  value: string
  submitted_by: string
  submitted_at: string
  intake: IntakeResult | null
}

export interface ReviewTicket {
  ticket_id: string
  run_id: string
  person_id: number
  trial_id: string
  criterion_ids: string[]
  status: 'PENDING' | 'APPROVED' | 'REJECTED' | 'RERUN_REQUESTED'
  created_at: string
  decided_at: string | null
  decided_by: string | null
  note: string | null
}

export interface AuditEvent {
  event_id: string
  sequence_no: number
  occurred_at: string
  action: string
  run_id: string | null
  actor: string
  person_id: number | null
  trial_id: string | null
  criterion_id: string | null
  detail: Record<string, unknown>
}

export interface FunnelStage {
  status: string
  label: string
  count: number
  share: number
}

export interface Bottleneck {
  criterion_id: string
  label: string
  field: string
  evaluated: number
  contradicted: number
  unknown: number
  conflicting: number
  review_required: number
  unresolved: number
  block_rate: number
}

export interface ReviewPriorityItem {
  run_id: string
  person_id: number
  eligibility_status: string | null
  unresolved_count: number
  criteria_met: number
  criteria_total: number
  completion: number
  unresolved_criteria: string[]
}

export interface CohortResponse {
  trial_id: string
  total_screened: number
  funnel: FunnelStage[]
  bottlenecks: Bottleneck[]
  review_priority: ReviewPriorityItem[]
}
