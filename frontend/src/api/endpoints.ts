/**
 * `backend/api/app/main.py` 의 라우트와 1:1 로 대응하는 함수들.
 *
 * VITE_USE_MOCK=true 면 같은 시그니처의 목업 어댑터로 갈아끼운다. 화면 코드는
 * 어느 쪽인지 알 필요가 없다.
 */

import { config, http } from './client'
import { mockApi } from './mock'
import type {
  AnswerRequest,
  AnswerResponse,
  ApplicationIntake,
  ApplicationSchema,
  AuditEvent,
  BaseApplicationSchema,
  CohortResponse,
  EvidencePacket,
  EvidenceRequest,
  HealthResponse,
  IntakeResult,
  PatientDetail,
  PatientListResponse,
  RecommendationRun,
  ReviewTicket,
  ScreeningRun,
  TimelineResponse,
  TrialDetail,
  TrialReviewItem,
  TrialReviewStatus,
  TrialSummary,
} from './types'

const mock = config.useMock

export const api = {
  // -- system ---------------------------------------------------------------

  health: (): Promise<HealthResponse> =>
    mock ? mockApi.health() : http.get<HealthResponse>('/health'),

  // -- patients -------------------------------------------------------------

  listPatients: (limit = 20, offset = 0): Promise<PatientListResponse> =>
    http.get<PatientListResponse>('/api/v1/patients', { limit, offset }),

  getPatient: (personId: number): Promise<PatientDetail> =>
    mock
      ? mockApi.getPatient(personId)
      : http.get<PatientDetail>(`/api/v1/patients/${personId}`),

  getTimeline: (personId: number): Promise<TimelineResponse> =>
    http.get<TimelineResponse>(`/api/v1/patients/${personId}/timeline`),

  // -- trials ---------------------------------------------------------------

  listTrials: (): Promise<TrialSummary[]> =>
    mock ? mockApi.listTrials() : http.get<TrialSummary[]>('/api/v1/trials'),

  getTrial: (trialId: string): Promise<TrialDetail> =>
    http.get<TrialDetail>(`/api/v1/trials/${encodeURIComponent(trialId)}`),

  listTrialReviews: (
    status: TrialReviewStatus = 'pending_review',
  ): Promise<TrialReviewItem[]> =>
    http.get<TrialReviewItem[]>('/api/v1/admin/trials', { status }),

  decideTrialReview: (
    trialId: string,
    sourceKey: string,
    decision: 'approved' | 'rejected',
    note: string,
  ): Promise<TrialReviewItem> =>
    http.patch<TrialReviewItem>(
      `/api/v1/admin/trials/${encodeURIComponent(trialId)}/review`,
      { source_key: sourceKey, decision, note },
    ),

  // -- 자연어 지원서 ---------------------------------------------------------
  //
  // 지원서 수집은 스크리닝 판정과 분리되어 있다. 이 단계는 값을 모을 뿐
  // 적격 여부를 판단하지 않는다.

  getBaseApplicationSchema: (): Promise<BaseApplicationSchema> =>
    mock
      ? mockApi.getBaseApplicationSchema()
      : http.get<BaseApplicationSchema>('/api/v1/application-schemas/base'),

  /**
   * 공고의 선정·제외 기준으로 지원서 스키마를 준비한다.
   *
   * 추천 공고를 클릭했을 때 부르는 경로다. 공고문을 화면에서 조립해 보낼 필요가
   * 없고, 물어볼 항목이 기준에서 파생되므로 수집한 값이 반드시 어떤 기준의
   * 입력이 된다. 같은 공고·같은 기준이면 같은 `schema_id` 가 오므로 여러 번
   * 불러도 스키마가 늘어나지 않는다. LLM 을 부르지 않아 Bedrock 없이도 된다.
   */
  prepareTrialApplicationSchema: (trialId: string): Promise<ApplicationSchema> =>
    mock
      ? mockApi.prepareTrialApplicationSchema(trialId)
      : http.post<ApplicationSchema>(
          `/api/v1/trials/${encodeURIComponent(trialId)}/application-schema`,
        ),

  /**
   * 공고문 자유 텍스트에서 LLM 으로 확장 필드를 만들어 스키마를 얻는다.
   * `BEDROCK_ENABLED=false` 인 로컬에서는 공고 내용을 추측하지 않으므로
   * `additional_fields` 를 명시적으로 넘겨야 한다. 기준이 이미 있는 공고라면
   * `prepareTrialApplicationSchema` 쪽이 낫다.
   */
  createApplicationSchema: (
    trialId: string,
    noticeText: string,
    additionalFields?: Record<string, unknown>[],
  ): Promise<ApplicationSchema> =>
    mock
      ? mockApi.createApplicationSchema(trialId, noticeText)
      : http.post<ApplicationSchema>('/api/v1/application-schemas', {
          trial_id: trialId,
          notice_text: noticeText,
          additional_fields: additionalFields ?? null,
        }),

  getApplicationSchema: (schemaId: string): Promise<ApplicationSchema> =>
    http.get<ApplicationSchema>(
      `/api/v1/application-schemas/${encodeURIComponent(schemaId)}`,
    ),

  /** 첫 자연어 지원서 제출. */
  startApplication: (
    schemaId: string,
    applicationText: string,
  ): Promise<ApplicationIntake> =>
    mock
      ? mockApi.startApplication(schemaId, applicationText)
      : http.post<ApplicationIntake>('/api/v1/applications', {
          schema_id: schemaId,
          application_text: applicationText,
        }),

  /**
   * 누락 항목 추가 답변. 실제로 발행한 재질문 기준 최대 5회다.
   * 종료된 세션에 답변을 추가하면 백엔드가 409 를 돌려준다.
   */
  answerApplication: (
    applicationId: string,
    responseText: string,
  ): Promise<ApplicationIntake> =>
    mock
      ? mockApi.answerApplication(applicationId, responseText)
      : http.post<ApplicationIntake>(
          `/api/v1/applications/${encodeURIComponent(applicationId)}/responses`,
          { response_text: responseText },
        ),

  getApplication: (applicationId: string): Promise<ApplicationIntake> =>
    mock
      ? mockApi.getApplication(applicationId)
      : http.get<ApplicationIntake>(
          `/api/v1/applications/${encodeURIComponent(applicationId)}`,
        ),

  /** 완성 지원서를 환자 기록과 연결해 오케스트레이터를 실행한다. COMPLETE 에서만 된다. */
  screenApplication: (
    applicationId: string,
    personId: number,
  ): Promise<ScreeningRun> =>
    mock
      ? mockApi.screenApplication(applicationId, personId)
      : http.post<ScreeningRun>(
          `/api/v1/applications/${encodeURIComponent(applicationId)}/screening`,
          { person_id: personId, actor: config.actor },
        ),

  // -- 추천 -----------------------------------------------------------------

  /** 후보 공고 전체를 판정하고 A2A 를 반영해 정렬한다. 홈·결과 화면의 주 호출. */
  runRecommendations: (
    personId: number,
    topK = 3,
    trialIds?: string[],
    applicationId?: string,
  ): Promise<RecommendationRun> =>
    mock
      ? mockApi.runRecommendations(personId, topK)
      : http.post<RecommendationRun>('/api/v1/recommendations/run', {
          person_id: personId,
          trial_ids: trialIds ?? null,
          application_id: applicationId ?? null,
          top_k: topK,
          actor: config.actor,
        }),

  getRecommendation: (recommendationId: string): Promise<RecommendationRun> =>
    mock
      ? mockApi.runRecommendations(config.demoPersonId, 3)
      : http.get<RecommendationRun>(
          `/api/v1/recommendations/${encodeURIComponent(recommendationId)}`,
        ),

  // -- screening ------------------------------------------------------------

  runScreening: (personId: number, trialId: string): Promise<ScreeningRun> =>
    mock
      ? mockApi.runScreening(personId, trialId)
      : http.post<ScreeningRun>('/api/v1/screening/run', {
          person_id: personId,
          trial_id: trialId,
          actor: config.actor,
        }),

  getScreening: (runId: string): Promise<ScreeningRun> =>
    mock
      ? mockApi.getScreening(runId)
      : http.get<ScreeningRun>(`/api/v1/screening/${encodeURIComponent(runId)}`),

  /** 참여자 답변을 반영해 다시 판정한다. 응답의 supplements 에 반영 내역이 남는다. */
  rerunScreening: (runId: string): Promise<ScreeningRun> =>
    mock
      ? mockApi.getScreening(runId)
      : http.post<ScreeningRun>(
          `/api/v1/screening/${encodeURIComponent(runId)}/rerun`,
        ),

  getEvidence: (runId: string): Promise<EvidencePacket> =>
    mock
      ? mockApi.getScreening(runId).then((run) => run.packet)
      : http.get<EvidencePacket>(
          `/api/v1/screening/${encodeURIComponent(runId)}/evidence`,
        ),

  // -- participant ----------------------------------------------------------

  getQuestions: (personId: number, trialId?: string): Promise<EvidenceRequest[]> =>
    mock
      ? mockApi.getQuestions(personId, trialId)
      : http.get<EvidenceRequest[]>(`/api/v1/patients/${personId}/questions`, {
          trial_id: trialId,
        }),

  submitAnswer: (personId: number, body: AnswerRequest): Promise<AnswerResponse> =>
    mock
      ? mockApi.submitAnswer(personId, body)
      : http.post<AnswerResponse>(`/api/v1/patients/${personId}/answers`, {
          submitted_by: 'participant',
          ...body,
        }),

  // -- intake (자유 문장 정규화) --------------------------------------------

  normalizeIntake: (text: string, referenceDate?: string | null): Promise<IntakeResult> =>
    mock
      ? mockApi.normalizeIntake(text, referenceDate)
      : http.post<IntakeResult>('/api/v1/intake/normalize', {
          text,
          reference_date: referenceDate ?? null,
        }),

  // -- cohort · review · audit ----------------------------------------------

  getCohort: (trialId: string): Promise<CohortResponse> =>
    mock
      ? mockApi.getCohort(trialId)
      : http.get<CohortResponse>(`/api/v1/cohort/${encodeURIComponent(trialId)}`),

  listReviewQueue: (status?: string, trialId?: string): Promise<ReviewTicket[]> =>
    http.get<ReviewTicket[]>('/api/v1/review-queue', {
      status,
      trial_id: trialId,
    }),

  getAudit: (runId: string): Promise<AuditEvent[]> =>
    http.get<AuditEvent[]>(`/api/v1/audit/${encodeURIComponent(runId)}`),
}
