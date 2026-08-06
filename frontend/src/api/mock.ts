/**
 * 목업 어댑터.
 *
 * 백엔드는 `outputs/longitudinal_emr_v2/` 합성 EMR 데이터셋을 읽는데, 그 폴더는
 * .gitignore 에 걸려 있어 클론한 환경에는 없다. 데이터셋이 없으면 uvicorn 이
 * 기동 단계에서 FileNotFoundError 로 죽는다. 그래서 백엔드 없이도 화면 전체를
 * 돌려볼 수 있게 같은 타입을 돌려주는 어댑터를 둔다.
 *
 * 여기 값은 디자인 시안(`로그인 회원가입 UI.dc.html`)의 예시 데이터를 그대로 옮긴
 * 것이다. 실제 판정 결과가 아니다. VITE_USE_MOCK=false 로 끄면 곧장 실제 API 를 탄다.
 */

import type {
  AnswerResponse,
  ApplicationIntake,
  ApplicationSchema,
  BaseApplicationSchema,
  CohortResponse,
  Decision,
  EvidencePacket,
  EvidenceRequest,
  IntakeResult,
  JudgmentItem,
  MissingApplicationField,
  PacketItem,
  PatientDetail,
  RecommendationRun,
  RecommendedTrial,
  ScreeningRun,
  TrialSummary,
} from './types'

const INDEX_DATE = '2026-07-20'

interface MockTrial {
  trial: TrialSummary
  /** 시안의 공고 메타 한 줄. 실제 API 에는 없는 필드라 여기서만 쓴다. */
  meta: string
  status: EligibilityLike
  items: PacketItem[]
}

type EligibilityLike = ScreeningRun['eligibility_status']

function item(partial: Partial<PacketItem> & Pick<PacketItem, 'criterion_id' | 'label' | 'status'>): PacketItem {
  return {
    criterion_type: 'INCLUSION',
    field: '',
    kind: 'measurement',
    status_label: '',
    observed_value: null,
    expected_condition: '',
    unit: null,
    observed_at: INDEX_DATE,
    confidence: 0.9,
    confidence_band: 'HIGH',
    explanation: '',
    source_ids: [],
    conflicts: [],
    narrative: [],
    ...partial,
  }
}

const AGE_OK = item({
  criterion_id: 'inc_age_001',
  label: '만 19세 이상',
  field: 'age',
  status: 'EVIDENCE_FOUND',
  status_label: '충족',
  observed_value: '36',
  expected_condition: '>= 19',
  unit: '세',
  confidence: 0.99,
  explanation: '1990년생으로 나이 기준을 충족합니다.',
  source_ids: ['profile.birth_year'],
})

const HBA1C_OK = item({
  criterion_id: 'inc_hba1c_001',
  label: '최근 180일 이내 HbA1c 7.0% 이상',
  field: 'hba1c_pct',
  status: 'EVIDENCE_FOUND',
  status_label: '충족',
  observed_value: '7.8',
  expected_condition: '>= 7.0',
  unit: '%',
  confidence: 0.91,
  explanation: '2026.07.20 검사에서 7.8%로 확인되었습니다.',
  source_ids: ['patient.note_20260720_01'],
  narrative: [
    {
      note_id: 'note_20260720_01',
      encounter_id: 'ENC-20260720',
      note_date: INDEX_DATE,
      snippet: 'HbA1c 7.8%',
      matched_terms: ['HbA1c'],
      score: 0.91,
    },
  ],
})

const PREGNANCY_UNKNOWN = item({
  criterion_id: 'exc_pregnancy_001',
  criterion_type: 'EXCLUSION',
  label: '임신 중인 대상자 제외',
  field: 'pregnancy',
  kind: 'boolean',
  status: 'UNKNOWN',
  status_label: '확인필요',
  expected_condition: '임신 아님',
  observed_at: null,
  confidence: 0.42,
  confidence_band: 'LOW',
  explanation:
    '기록에 임신 여부 근거가 없어 판단을 보류했습니다. 기록 부재만으로 임신이 아니라고 확정하지 않습니다.',
})

const METFORMIN_OK = item({
  criterion_id: 'inc_med_002',
  label: 'metformin 단독 복용 중인 2형 당뇨 환자',
  field: 'medications',
  kind: 'medication',
  status: 'EVIDENCE_FOUND',
  status_label: '충족',
  observed_value: 'metformin',
  expected_condition: 'metformin 복용 중',
  confidence: 0.88,
  explanation: '직접 입력한 복용 정보와 2019년 진단 기록이 일치합니다.',
  source_ids: ['patient.med_metformin_01'],
})

const EGFR_UNKNOWN = item({
  criterion_id: 'inc_egfr_003',
  label: '최근 3개월 이내 eGFR 검사 결과',
  field: 'egfr_ml_min_1_73m2',
  status: 'UNKNOWN',
  status_label: '확인필요',
  expected_condition: '>= 45',
  unit: 'mL/min/1.73m2',
  observed_at: null,
  confidence: 0.35,
  confidence_band: 'LOW',
  explanation: '소견서에 eGFR 값이 없어 신기능 기준을 판단하지 못했습니다.',
})

/**
 * 관심 분야 밖 공고의 질환 확인 기준. 당뇨 기록만 있는 환자에게는 판단할 근거가
 * 없으므로 UNKNOWN 으로 남는다. 이게 없으면 나이 기준 하나만 충족한 공고가
 * 만점으로 올라와, 당뇨 환자에게 소화불량 임상이 1순위로 뜬다.
 */
function domainUnknown(condition: string): PacketItem {
  return item({
    criterion_id: `inc_condition_${condition}`,
    label: `${condition} 진단을 받은 대상자`,
    field: 'conditions',
    kind: 'condition',
    status: 'UNKNOWN',
    status_label: '확인필요',
    expected_condition: `${condition} 진단 있음`,
    observed_at: null,
    confidence: 0.3,
    confidence_band: 'LOW',
    explanation: `기록에 ${condition} 관련 근거가 없어 판단을 보류했습니다.`,
  })
}

const INSULIN_FAIL = item({
  criterion_id: 'inc_insulin_001',
  label: '인슐린 치료를 6개월 이상 받은 대상자',
  field: 'medications',
  kind: 'medication',
  status: 'CONTRADICTED',
  status_label: '미충족',
  observed_value: '기록 없음',
  expected_condition: '인슐린 6개월 이상',
  confidence: 0.86,
  explanation: '인슐린 치료 기록이 없어 선정 기준을 충족하지 못합니다.',
  source_ids: ['patient.timeline_med'],
})

const TRIALS: MockTrial[] = [
  {
    trial: {
      trial_id: 'medi25-10842',
      trial_name: '2형 당뇨 디지털치료제 확증 임상',
      description: '2형 당뇨 환자의 혈당 관리 앱 사용 12주 후 HbA1c 변화를 확인합니다.',
      purpose: '내분비',
      synthetic_trial: true,
      criteria_count: 7,
    },
    meta: 'medi25-10842 · 경기 · 4회 방문 · 앱 12주',
    status: 'ELIGIBLE',
    items: [AGE_OK, HBA1C_OK, METFORMIN_OK],
  },
  {
    trial: {
      trial_id: 'medi25-10713',
      trial_name: '당뇨병 대상 경구제 3상 · OO병원',
      description: '기존 경구제로 조절되지 않는 2형 당뇨 환자를 대상으로 합니다.',
      purpose: '내분비',
      synthetic_trial: true,
      criteria_count: 7,
    },
    meta: 'medi25-10713 · 서울 · 8회 방문 · 만 19세 이상',
    status: 'NEEDS_MORE_EVIDENCE',
    items: [AGE_OK, HBA1C_OK, PREGNANCY_UNKNOWN],
  },
  {
    trial: {
      trial_id: 'medi25-10908',
      trial_name: '당뇨병성 신경통증 주사제 2상',
      description: '당뇨병성 말초신경병증 통증의 개선 정도를 평가합니다.',
      purpose: '신경',
      synthetic_trial: true,
      criteria_count: 7,
    },
    meta: 'medi25-10908 · 서울 · 6회 방문',
    status: 'NEEDS_MORE_EVIDENCE',
    items: [AGE_OK, EGFR_UNKNOWN, PREGNANCY_UNKNOWN],
  },
  {
    trial: {
      trial_id: 'medi25-10655',
      trial_name: '인슐린 병용 요법 장기 안전성 연구',
      description: '인슐린 치료 중인 환자의 장기 안전성을 추적합니다.',
      purpose: '내분비',
      synthetic_trial: true,
      criteria_count: 5,
    },
    meta: 'medi25-10655 · 서울 · 12회 방문',
    status: 'INELIGIBLE',
    items: [AGE_OK, INSULIN_FAIL],
  },
  {
    trial: {
      trial_id: 'medi25-10771',
      trial_name: '고혈압 복합제 장기 안전성 추적',
      description: '고혈압 복합제를 복용하는 성인의 장기 안전성을 관찰합니다.',
      purpose: '순환기',
      synthetic_trial: true,
      criteria_count: 5,
    },
    meta: 'medi25-10771 · 서울 · 4회 방문',
    status: 'NEEDS_MORE_EVIDENCE',
    items: [AGE_OK, EGFR_UNKNOWN],
  },
  {
    trial: {
      trial_id: 'medi25-10688',
      trial_name: '기능성 소화불량 경구제 3상',
      description: '식후 팽만·조기 만복감이 반복되는 환자를 대상으로 합니다.',
      purpose: '소화기',
      synthetic_trial: true,
      criteria_count: 6,
    },
    meta: 'medi25-10688 · 경기 · 6회 방문',
    status: 'NEEDS_MORE_EVIDENCE',
    items: [AGE_OK, domainUnknown('기능성 소화불량')],
  },
  {
    trial: {
      trial_id: 'medi25-10820',
      trial_name: '무릎 골관절염 통증 개선 주사제 2상',
      description: '기존 치료로 통증이 남아 있는 환자의 유효성과 안전성을 확인합니다.',
      purpose: '근골격',
      synthetic_trial: true,
      criteria_count: 6,
    },
    meta: 'medi25-10820 · 서울 · 5회 방문',
    status: 'NEEDS_MORE_EVIDENCE',
    items: [AGE_OK, domainUnknown('무릎 골관절염')],
  },
  {
    trial: {
      trial_id: 'medi25-10744',
      trial_name: '알레르기 비염 코 스프레이 2상',
      description: '중등증 알레르기 비염 성인의 증상 개선을 평가합니다.',
      purpose: '호흡기',
      synthetic_trial: true,
      criteria_count: 5,
    },
    meta: 'medi25-10744 · 인천 · 3회 방문',
    status: 'NEEDS_MORE_EVIDENCE',
    items: [AGE_OK, domainUnknown('알레르기 비염')],
  },
  {
    trial: {
      trial_id: 'medi25-10512',
      trial_name: '중등증 아토피 피부염 국소도포제 2b상',
      description: '중등증 아토피 피부염의 국소 도포 치료 효과를 확인합니다.',
      purpose: '피부',
      synthetic_trial: true,
      criteria_count: 6,
    },
    meta: 'medi25-10512 · 서울 · 6회 방문 · 모집 마감',
    status: 'NEEDS_MORE_EVIDENCE',
    items: [AGE_OK, domainUnknown('아토피 피부염')],
  },
  {
    trial: {
      trial_id: 'medi25-10931',
      trial_name: '갑상선 기능저하증 병용요법 관찰연구',
      description: '갑상선 호르몬 병용 요법의 실제 사용 양상을 관찰합니다.',
      purpose: '내분비',
      synthetic_trial: true,
      criteria_count: 4,
    },
    meta: 'medi25-10931 · 경기 · 2회 방문',
    status: 'NEEDS_MORE_EVIDENCE',
    items: [AGE_OK, domainUnknown('갑상선 기능저하증')],
  },
]

/** 공고 메타 한 줄. 실제 API 에는 없어서 UI 가 옵셔널로 취급한다. */
export const TRIAL_META: Record<string, string> = Object.fromEntries(
  TRIALS.map((entry) => [entry.trial.trial_id, entry.meta]),
)

const DECISION_LABEL: Record<EligibilityLike, string> = {
  ELIGIBLE: '추천',
  INELIGIBLE: '제외',
  NEEDS_MORE_EVIDENCE: '정보부족',
  REVIEW_REQUIRED: '검토 필요',
}

/** 제출된 답변을 실행별로 들고 있는다. 새로고침하면 사라진다. */
const answered = new Map<string, Set<string>>()

function delay<T>(value: T, ms = 260): Promise<T> {
  return new Promise((resolve) => setTimeout(() => resolve(value), ms))
}

function buildRun(personId: number, trialId: string): ScreeningRun {
  const entry = TRIALS.find((candidate) => candidate.trial.trial_id === trialId)
  if (!entry) throw new Error(`Trial not found: ${trialId}`)

  const done = answered.get(`${personId}:${trialId}`) ?? new Set<string>()
  // 답변된 기준은 충족으로 넘긴다. 실제 백엔드도 재실행 시 같은 방향으로 움직인다.
  const items = entry.items.map((packetItem) =>
    done.has(packetItem.criterion_id) && packetItem.status === 'UNKNOWN'
      ? {
          ...packetItem,
          status: 'EVIDENCE_FOUND' as const,
          status_label: '충족',
          confidence: 0.9,
          confidence_band: 'HIGH',
          explanation: '참여자 답변으로 확인되었습니다.',
          source_ids: ['participant.answer'],
        }
      : packetItem,
  )

  const open = items
    .filter((packetItem) => packetItem.status === 'UNKNOWN')
    .map((packetItem) => packetItem.criterion_id)
  const blocking = items
    .filter((packetItem) => packetItem.status === 'CONTRADICTED')
    .map((packetItem) => packetItem.criterion_id)

  const status: EligibilityLike = blocking.length
    ? 'INELIGIBLE'
    : open.length
      ? 'NEEDS_MORE_EVIDENCE'
      : 'ELIGIBLE'

  const total = entry.trial.criteria_count
  const packet: EvidencePacket = {
    run_id: `mock_${trialId}`,
    person_id: personId,
    trial_id: trialId,
    criteria_version: '2026-08-04.1',
    index_encounter_id: 'ENC-20260720',
    index_date: INDEX_DATE,
    eligibility_status: status,
    decision_label: DECISION_LABEL[status],
    criteria_total: total,
    // 상세로 보여주는 항목 외의 나머지는 충족으로 접어둔다 (시안과 같은 처리).
    criteria_met: total - open.length - blocking.length,
    items,
    uncertainty: {
      blocking_criteria: blocking,
      open_criteria: open,
      review_criteria: [],
      unresolved_count: open.length,
    },
  }

  const requests: EvidenceRequest[] = items
    .filter((packetItem) => packetItem.status === 'UNKNOWN')
    .map((packetItem, index) => ({
      request_id: `req_${packetItem.criterion_id}`,
      criterion_id: packetItem.criterion_id,
      field: packetItem.field,
      label: packetItem.label,
      reason: packetItem.explanation,
      question:
        packetItem.criterion_id === 'exc_pregnancy_001'
          ? '현재 임신 중이거나 임신 가능성이 있나요?'
          : packetItem.criterion_id === 'inc_egfr_003'
            ? '최근 3개월 안에 신장 기능(eGFR) 검사를 받으셨나요?'
            : `${packetItem.label} 을(를) 확인할 수 있는 정보가 있나요?`,
      information_value: 0.8 - index * 0.1,
      effort: 0.2,
      priority: index + 1,
      target: 'participant',
    }))

  // 기준별 판단 요약. 실제 백엔드는 `app/reasoning/judgment.py` 가 채운다.
  const judgmentItems: JudgmentItem[] = items.map((packetItem) => {
    const proposed: Decision =
      packetItem.status === 'EVIDENCE_FOUND'
        ? 'OK'
        : packetItem.status === 'CONTRADICTED'
          ? 'NOT_OK'
          : 'UNKNOWN'
    const grounded = packetItem.source_ids.length > 0
    return {
      criterion_id: packetItem.criterion_id,
      label: packetItem.label,
      proposed,
      final_status: packetItem.status,
      route: proposed === 'UNKNOWN' ? 'HUMAN_REVIEW' : 'DECIDED',
      used_evidence_ids: packetItem.source_ids,
      verification: {
        passed: grounded,
        checks: [
          { id: 'V-EVIDENCE', passed: grounded, detail: grounded ? '' : '인용한 출처가 없습니다' },
          { id: 'V-WINDOW', passed: Boolean(packetItem.observed_at) },
          { id: 'V-UNIT', passed: true },
          { id: 'V-OPERATOR', passed: true },
          { id: 'V-TYPE', passed: true },
          { id: 'V-PII', passed: true },
          { id: 'V-CONFIDENCE', passed: packetItem.confidence >= 0.55 },
        ],
      },
    }
  })

  const decision: Decision =
    status === 'ELIGIBLE' ? 'OK' : status === 'INELIGIBLE' ? 'NOT_OK' : 'UNKNOWN'

  return {
    run_id: packet.run_id,
    person_id: personId,
    trial_id: trialId,
    criteria_version: packet.criteria_version,
    index_encounter_id: packet.index_encounter_id,
    index_date: packet.index_date,
    eligibility_status: status,
    screening_decision: decision,
    judgment: { items: judgmentItems },
    deliberation: {},
    supplements: null,
    decision_label: packet.decision_label,
    criteria_total: packet.criteria_total,
    criteria_met: packet.criteria_met,
    blocking_criteria: blocking,
    open_criteria: open,
    review_criteria: [],
    human_review_criteria: [],
    mode: 'mock',
    agent: {},
    packet,
    requests,
    explanations: {
      participant: {
        audience: 'participant',
        summary:
          status === 'ELIGIBLE'
            ? '기록된 정보로는 참여 가능한 조건을 모두 만족합니다. 최종 확인은 실시기관에서 진행합니다.'
            : status === 'INELIGIBLE'
              ? '선정 기준 중 충족하지 못한 항목이 있어 이 시험에는 참여가 어렵습니다.'
              : '몇 가지 정보가 더 필요합니다. 확인 질문에 답하시면 판단을 이어갑니다.',
        highlights: items.map(
          (packetItem) => `${packetItem.label} — ${packetItem.status_label}`,
        ),
        blocked: false,
        guardrail_findings: [],
      },
    },
    trace: {
      run_id: packet.run_id,
      span_count: 6,
      total_duration_ms: 412,
      error_count: 0,
      by_kind: { tool: 4, model: 0, agent: 2 },
    },
    limitations: [
      '목업 데이터 기반의 사전 스크리닝 결과이며 의료적 판단이 아닙니다.',
      'VITE_USE_MOCK=false 로 두면 실제 백엔드 판정을 사용합니다.',
    ],
  }
}

// ---------------------------------------------------------------------------
// 자연어 지원서 흐름
// ---------------------------------------------------------------------------

/**
 * 목업 지원서 필드.
 *
 * 실제 백엔드는 기본 스키마(`app/intake/service.py` 의 BASE_PROPERTIES)에 공고
 * 기준에서 파생된 필드(`app/intake/trial_schema.py`)를 더해 스키마를 만든다.
 * 목업도 같은 모양을 흉내내야 화면이 공고별로 다른 질문을 받는 흐름을 확인할 수
 * 있다.
 */
interface MockField extends MissingApplicationField {
  /** 이 항목을 물을 때 쓰는 문장. */
  ask: string
  /** 숫자 추출 규칙. capture group 1 을 값으로 쓴다. */
  match?: RegExp
  /** 불리언 판단용 키워드. 문장에 있으면 부정 표현 여부로 값을 정한다. */
  keyword?: RegExp
  /** 기준 필드 매핑. 실제 스키마의 `x-criterion-field` 자리. */
  criterionField?: string
  unit?: string
}

/** 기본 스키마 v1 의 필수 필드. 백엔드 README 의 목록과 같다. */
const BASE_FIELDS: MockField[] = [
  {
    name: 'age',
    title: '나이',
    description: '만 나이',
    type: 'integer',
    ask: '만 나이가 어떻게 되시나요?',
  },
  {
    name: 'sex',
    title: '성별',
    description: '남 또는 여',
    type: 'string',
    ask: '성별을 알려주세요.',
  },
  {
    name: 'diagnosed_conditions',
    title: '진단받은 질환',
    description: '진단명과 시기',
    type: 'array',
    ask: '진단받으신 질환과 시기를 알려주세요.',
  },
  {
    name: 'current_medications',
    title: '복용 중인 약',
    description: '약 이름과 용량',
    type: 'array',
    ask: '현재 복용 중인 약이 있으면 알려주세요. 없으면 없다고 적어주세요.',
  },
  {
    name: 'allergies',
    title: '알레르기',
    description: '없으면 없음',
    type: 'array',
    ask: '알레르기가 있으신가요? 없으면 없다고 적어주세요.',
  },
  {
    name: 'prior_trial_participation',
    title: '임상시험 참여 경험',
    description: '있음 또는 없음',
    type: 'boolean',
    ask: '이전에 임상시험에 참여한 적이 있나요?',
  },
]

const HBA1C: MockField = {
  name: 'hba1c',
  title: 'HbA1c',
  description: '가장 최근에 확인된 HbA1c 값 (단위: %)',
  type: 'number',
  ask: '가장 최근 검사에서 당화혈색소(HbA1c)가 얼마였나요?',
  match: /(?:당화혈색소|hba1c|a1c)\D{0,8}([0-9]+(?:\.[0-9]+)?)/i,
  criterionField: 'hba1c',
  unit: '%',
}

const EGFR: MockField = {
  name: 'egfr',
  title: 'eGFR',
  description: '가장 최근에 확인된 eGFR 값 (단위: mL/min/1.73m2)',
  type: 'number',
  ask: '신장기능 검사(eGFR) 수치를 알고 계시면 알려주세요.',
  match: /(?:egfr|사구체여과율|신장기능)\D{0,8}([0-9]+(?:\.[0-9]+)?)/i,
  criterionField: 'egfr',
  unit: 'mL/min/1.73m2',
}

const PREGNANCY: MockField = {
  name: 'active_pregnancy',
  title: '임신 여부',
  description: '임신 여부에 해당하는지 여부. 이 공고의 제외 조건 확인에 사용합니다.',
  type: 'boolean',
  ask: '현재 임신 중이거나 수유 중이신가요?',
  keyword: /임신|수유/,
  criterionField: 'active_pregnancy',
}

const UNCONTROLLED_BP: MockField = {
  name: 'uncontrolled_bp',
  title: '조절되지 않는 고혈압',
  description:
    '조절되지 않는 고혈압에 해당하는지 여부. 이 공고의 제외 조건 확인에 사용합니다.',
  type: 'boolean',
  ask: '혈압이 약으로도 잘 조절되지 않는다고 들으신 적이 있나요?',
  keyword: /고혈압|혈압/,
  criterionField: 'uncontrolled_bp',
}

function numericField(
  name: string,
  title: string,
  ask: string,
  match: RegExp,
  unit?: string,
): MockField {
  return {
    name,
    title,
    description: `가장 최근에 확인된 ${title} 값${unit ? ` (단위: ${unit})` : ''}`,
    type: 'number',
    ask,
    match,
    criterionField: name,
    unit,
  }
}

/**
 * 공고별 추가 질문. 실제로는 `TrialCriteriaTable` 의 기준에서 파생된다.
 *
 * 기간·횟수를 계산해야 하는 기준(인슐린 6개월 이상 등)은 지원자에게 묻지 않는다.
 * 기록에서 세는 값이라 백엔드도 질문 대상에서 뺀다. 그래서 목업에서도 일부
 * 공고는 추가 질문이 없다.
 */
const TRIAL_FIELDS: Record<string, MockField[]> = {
  'medi25-10842': [HBA1C],
  'medi25-10713': [HBA1C, PREGNANCY],
  'medi25-10908': [EGFR, PREGNANCY],
  'medi25-10655': [],
  'medi25-10771': [EGFR, UNCONTROLLED_BP],
  'medi25-10688': [
    numericField(
      'dyspepsia_weeks',
      '소화불량 증상 기간',
      '식후 팽만이나 조기 만복감이 몇 주째 이어지고 있나요?',
      /([0-9]{1,3})\s*주/,
      'weeks',
    ),
  ],
  'medi25-10820': [
    numericField(
      'knee_pain_score',
      '무릎 통증 점수',
      '무릎 통증이 0~10 중 어느 정도인가요?',
      /통증\D{0,8}([0-9]{1,2})/,
      'score',
    ),
  ],
  'medi25-10744': [
    numericField(
      'nasal_symptom_score',
      '코 증상 점수',
      '코막힘·콧물 증상이 0~12 중 어느 정도인가요?',
      /(?:코\s*증상|코막힘|콧물)\D{0,8}([0-9]{1,2})/,
      'score',
    ),
  ],
  'medi25-10512': [
    numericField(
      'easi_score',
      'EASI 점수',
      '아토피 중증도(EASI) 점수를 들으신 적이 있으면 알려주세요.',
      /easi\D{0,8}([0-9]+(?:\.[0-9]+)?)/i,
      'score',
    ),
  ],
  'medi25-10931': [
    numericField(
      'tsh',
      'TSH',
      '가장 최근 갑상선 검사(TSH) 수치를 알려주세요.',
      /tsh\D{0,8}([0-9]+(?:\.[0-9]+)?)/i,
      'mIU/L',
    ),
  ],
}

/** 목업 스키마 id 규약. 공고 하나당 하나다. */
function schemaIdOf(trialId: string): string {
  return `schema_${trialId}`
}

function trialIdOf(schemaId: string): string {
  return schemaId.replace(/^schema_/, '')
}

/** 이 공고에서 물어야 하는 전체 항목. 기본 필드 + 공고 기준 필드. */
function fieldsFor(trialId: string): MockField[] {
  return [...BASE_FIELDS, ...(TRIAL_FIELDS[trialId] ?? [])]
}

const DENIAL = /없|아니|아닙|해당\s*없|음성|모르/

/** 공고 요약. 백엔드 `TrialSchemaBuilder` 가 만드는 notice_text 와 같은 모양. */
function noticeTextOf(trialId: string): string {
  const entry = TRIALS.find((candidate) => candidate.trial.trial_id === trialId)
  if (!entry) return ''
  const lines = [
    entry.trial.trial_name,
    '',
    `분야: ${entry.trial.purpose}`,
    entry.trial.description,
    '',
    '[공고 기준]',
    ...entry.items.map(
      (packetItem) =>
        `- ${packetItem.label}${
          packetItem.expected_condition ? `: ${packetItem.expected_condition}` : ''
        }`,
    ),
  ]
  return lines.join('\n')
}

/** 지원서 JSON Schema. 화면이 항목 목록과 단위를 읽는 데 쓴다. */
function jsonSchemaOf(trialId: string): Record<string, unknown> {
  const fields = fieldsFor(trialId)
  return {
    $schema: 'https://json-schema.org/draft/2020-12/schema',
    type: 'object',
    title: `${trialId} 임상시험 지원서`,
    additionalProperties: false,
    required: fields.map((field) => field.name),
    properties: Object.fromEntries(
      fields.map((field) => [
        field.name,
        {
          type: field.type,
          title: field.title,
          description: field.description,
          'x-source': field.criterionField ? 'trial_notice' : 'base',
          ...(field.criterionField
            ? { 'x-criterion-field': field.criterionField }
            : {}),
          ...(field.unit ? { 'x-unit': field.unit } : {}),
        },
      ]),
    ),
  }
}

interface MockApplication {
  intake: ApplicationIntake
}

const applications = new Map<string, MockApplication>()
let applicationSeq = 0

/**
 * 서술 문장에서 기본 필드를 아주 얕게 채운다. 실제 백엔드는
 * `app/intake/service.py` 가 LLM 추출 + 규칙 병합으로 처리한다.
 */
function extractFields(
  text: string,
  into: Record<string, unknown>,
  trialId: string,
): Record<string, unknown> {
  const data = { ...into }

  const age = text.match(/(?:만\s*)?([0-9]{1,3})\s*세/)
  if (age) data.age = Number(age[1])

  const birth = text.match(/((?:19|20)[0-9]{2})\s*년\s*생/)
  if (birth && data.age === undefined) {
    data.age = 2026 - Number(birth[1])
  }

  if (/여자|여성|\b여\b/.test(text)) data.sex = '여'
  else if (/남자|남성|\b남\b/.test(text)) data.sex = '남'

  const conditions = [...new Set(
    ['당뇨', '고혈압', '천식', '아토피', '신경병증'].filter((term) => text.includes(term)),
  )]
  if (conditions.length) {
    const prev = Array.isArray(data.diagnosed_conditions)
      ? (data.diagnosed_conditions as string[])
      : []
    data.diagnosed_conditions = [...new Set([...prev, ...conditions])]
  }

  const meds = [...new Set(
    ['메트포르민', '인슐린', '아스피린'].filter((term) => text.includes(term)),
  )]
  if (meds.length) {
    const prev = Array.isArray(data.current_medications)
      ? (data.current_medications as string[])
      : []
    data.current_medications = [...new Set([...prev, ...meds])]
  }

  // 명시적인 "없음" 은 빈 배열로 확정한다. 누락이 아니라 유효한 답변이다.
  if (/알레르기[^.]*없/.test(text)) data.allergies = []
  else {
    const allergy = text.match(/([가-힣A-Za-z]+)\s*알레르기/)
    if (allergy) data.allergies = [allergy[1]]
  }

  if (/참여한?\s*적[^.]*없|임상시험[^.]*없/.test(text)) {
    data.prior_trial_participation = false
  } else if (/참여한?\s*적[^.]*있|이전에[^.]*참여/.test(text)) {
    data.prior_trial_participation = true
  }

  // 공고 기준에서 파생된 필드. 수치는 규칙으로 뽑고, 여부는 키워드가 있는 문장의
  // 부정 표현으로 판단한다. 실제 백엔드는 이 자리를 LLM 추출이 담당한다.
  for (const field of TRIAL_FIELDS[trialId] ?? []) {
    if (data[field.name] !== undefined) continue

    if (field.match) {
      const hit = text.match(field.match)
      if (hit) data[field.name] = Number(hit[1])
      continue
    }
    if (field.keyword && field.keyword.test(text)) {
      const sentence =
        text
          .split(/[.\n,·]/)
          .find((part) => field.keyword?.test(part)) ?? text
      data[field.name] = !DENIAL.test(sentence)
    }
  }

  return data
}

function missingOf(
  data: Record<string, unknown>,
  trialId: string,
): MissingApplicationField[] {
  return fieldsFor(trialId).filter((field) => {
    const value = data[field.name]
    if (value === undefined || value === null) return true
    // 빈 배열과 false 는 유효한 답변이다. 누락으로 보지 않는다.
    return false
  })
}

/** 지금 물을 문장. 남은 항목이 여러 개면 하나씩 순서대로 묻는다. */
function askFor(trialId: string, missing: MissingApplicationField[]): string | null {
  const next = missing[0]
  if (!next) return null
  const field = fieldsFor(trialId).find((item) => item.name === next.name)
  const rest = missing.length - 1
  const tail = rest > 0 ? ` (남은 항목 ${rest}개)` : ''
  return `${field?.ask ?? `${next.title}을(를) 알려주세요.`}${tail}`
}

function buildIntake(
  id: string,
  schemaId: string,
  data: Record<string, unknown>,
  followUpCount: number,
  iteration: number,
): ApplicationIntake {
  const trialId = trialIdOf(schemaId)
  const missing = missingOf(data, trialId)
  const exhausted = followUpCount >= 5
  const status: ApplicationIntake['status'] = missing.length
    ? exhausted
      ? 'MAX_FOLLOW_UPS_REACHED'
      : 'NEEDS_MORE_INFO'
    : 'COMPLETE'

  return {
    application_id: id,
    schema_id: schemaId,
    trial_id: trialId,
    status,
    data,
    missing_fields: missing,
    follow_up_prompt: status === 'NEEDS_MORE_INFO' ? askFor(trialId, missing) : null,
    notice_text: noticeTextOf(trialId),
    iteration,
    follow_up_count: followUpCount,
    max_follow_ups: 5,
    updated_at: new Date().toISOString(),
  }
}

export const mockApi = {
  getBaseApplicationSchema: (): Promise<BaseApplicationSchema> =>
    delay({
      version: 'v1',
      json_schema: {
        type: 'object',
        required: BASE_FIELDS.map((field) => field.name),
        properties: Object.fromEntries(
          BASE_FIELDS.map((field) => [
            field.name,
            { title: field.title, description: field.description, type: field.type },
          ]),
        ),
      },
    }),

  /** 공고 기준으로 스키마를 준비한다. 같은 공고면 같은 schema_id 다. */
  prepareTrialApplicationSchema: (trialId: string): Promise<ApplicationSchema> => {
    const known = TRIALS.some((candidate) => candidate.trial.trial_id === trialId)
    if (!known) {
      // 실제 백엔드는 404 를 돌려준다.
      return Promise.reject(new Error(`모집공고를 찾을 수 없습니다: ${trialId}`))
    }
    return delay({
      schema_id: schemaIdOf(trialId),
      trial_id: trialId,
      version: 'mock',
      base_schema_version: 'v1',
      notice_text: noticeTextOf(trialId),
      json_schema: jsonSchemaOf(trialId),
      mode: 'mock',
      created_at: new Date().toISOString(),
    })
  },

  createApplicationSchema: (
    trialId: string,
    noticeText: string,
  ): Promise<ApplicationSchema> =>
    delay({
      schema_id: schemaIdOf(trialId),
      trial_id: trialId,
      version: 'v1',
      base_schema_version: 'v1',
      notice_text: noticeText || noticeTextOf(trialId),
      json_schema: jsonSchemaOf(trialId),
      mode: 'mock',
      created_at: new Date().toISOString(),
    }),

  startApplication: (
    schemaId: string,
    applicationText: string,
  ): Promise<ApplicationIntake> => {
    applicationSeq += 1
    const id = `app_mock_${applicationSeq}`
    const intake = buildIntake(
      id,
      schemaId,
      extractFields(applicationText, {}, trialIdOf(schemaId)),
      0,
      1,
    )
    applications.set(id, { intake })
    return delay(intake, 620)
  },

  answerApplication: (
    applicationId: string,
    responseText: string,
  ): Promise<ApplicationIntake> => {
    const entry = applications.get(applicationId)
    if (!entry) return Promise.reject(new Error('지원서를 찾을 수 없습니다.'))
    if (entry.intake.status !== 'NEEDS_MORE_INFO') {
      // 실제 백엔드는 409 Conflict 를 돌려준다.
      return Promise.reject(new Error('이미 종료된 지원서입니다.'))
    }

    // 답변이 어느 필드에 대한 것인지 모를 때를 대비해, 지금 묻고 있던 필드에
    // 자유 문장을 그대로 넣는다. 그러지 않으면 같은 질문이 반복된다.
    const trialId = trialIdOf(entry.intake.schema_id)
    const asking = entry.intake.missing_fields[0]
    let data = extractFields(responseText, entry.intake.data, trialId)
    if (asking && data[asking.name] === undefined) {
      const denial = DENIAL.test(responseText)
      const digits = responseText.match(/([0-9]+(?:\.[0-9]+)?)/)
      data = {
        ...data,
        [asking.name]:
          asking.type === 'array'
            ? denial
              ? []
              : [responseText.trim()]
            : asking.type === 'boolean'
              ? !denial
              : asking.type === 'integer' || asking.type === 'number'
                ? Number(digits?.[1] ?? 0)
                : responseText.trim(),
      }
    }

    const next = buildIntake(
      applicationId,
      entry.intake.schema_id,
      data,
      entry.intake.follow_up_count + 1,
      entry.intake.iteration + 1,
    )
    applications.set(applicationId, { intake: next })
    return delay(next, 480)
  },

  getApplication: (applicationId: string): Promise<ApplicationIntake> => {
    const entry = applications.get(applicationId)
    return entry
      ? delay(entry.intake, 120)
      : Promise.reject(new Error('지원서를 찾을 수 없습니다.'))
  },

  screenApplication: (applicationId: string) => {
    const entry = applications.get(applicationId)
    const trialId = entry?.intake.trial_id ?? TRIALS[1].trial.trial_id
    const known = TRIALS.some((candidate) => candidate.trial.trial_id === trialId)
    const run = buildRun(1, known ? trialId : TRIALS[1].trial.trial_id)
    return delay(
      {
        ...run,
        person_id: null,
        application_id: applicationId,
        packet: { ...run.packet, person_id: null },
        supplements: {
          applied_fields: Object.keys(entry?.intake.data ?? {}),
          excluded_fields: [],
          source_application_id: applicationId,
          retrieval_mode: 'mock:keyword',
        },
      },
      520,
    )
  },

  runRecommendations: (personId: number, topK: number): Promise<RecommendationRun> => {
    const evaluated = TRIALS.map((entry) => {
      const run = buildRun(personId, entry.trial.trial_id)
      const decision: Decision = run.screening_decision
      // 추천 점수는 코드에 고정된 보수적 값이다. 모델 생성값이 아니다.
      const score = decision === 'OK' ? 1.0 : decision === 'UNKNOWN' ? 0.4 : 0.0
      const trial: RecommendedTrial = {
        rank: null,
        run_id: run.run_id,
        trial_id: entry.trial.trial_id,
        title: entry.trial.trial_name,
        description: entry.trial.description,
        rank_score: score,
        overall_status:
          decision === 'OK'
            ? 'MATCHED'
            : decision === 'UNKNOWN'
              ? 'NEEDS_MORE_INFO'
              : 'EXCLUDED',
        screening_decision: decision,
        recommendation_decision: decision,
        criteria_met: run.criteria_met,
        criteria_total: run.criteria_total,
        unresolved_criteria: run.open_criteria,
        human_review_required: run.open_criteria.length > 0,
        human_review_criteria: run.open_criteria,
        selection_reason:
          decision === 'OK'
            ? '모든 기준이 근거와 함께 충족되었습니다.'
            : decision === 'UNKNOWN'
              ? `확인이 필요한 기준 ${run.open_criteria.length}건이 남아 있습니다.`
              : '제외 기준에 해당해 추천에서 제외했습니다.',
        a2a: {},
        criteria: run.packet.items.map((packetItem) => {
          const criterionDecision: Decision =
            packetItem.status === 'EVIDENCE_FOUND'
              ? 'OK'
              : packetItem.status === 'CONTRADICTED'
                ? 'NOT_OK'
                : 'UNKNOWN'
          return {
            criterion_id: packetItem.criterion_id,
            criterion_type: packetItem.criterion_type,
            label: packetItem.label,
            screening_status: criterionDecision,
            recommendation_status: criterionDecision,
            reason: packetItem.explanation,
            evidence_ids: packetItem.source_ids,
            a2a_applied: false,
            next_question:
              criterionDecision === 'UNKNOWN'
                ? run.requests.find(
                    (request) => request.criterion_id === packetItem.criterion_id,
                  )?.question ?? null
                : null,
          }
        }),
      }
      return trial
    })

    // 확정 NOT_OK 는 추천에서 빼고, 나머지는 OK 우선 · UNKNOWN 차순으로 정렬한다.
    const excluded = evaluated.filter((trial) => trial.recommendation_decision === 'NOT_OK')
    const ranked = evaluated
      .filter((trial) => trial.recommendation_decision !== 'NOT_OK')
      .sort((left, right) => right.rank_score - left.rank_score)
      .map((trial, index) => ({ ...trial, rank: index + 1 }))

    return delay(
      {
        recommendation_id: `rec_mock_${personId}`,
        person_id: personId,
        evaluated_trials: evaluated.length,
        recommended_trials: ranked.slice(0, topK),
        excluded_trials: excluded,
        remaining_candidate_count: Math.max(0, ranked.length - topK),
        limits: { top_k: topK, a2a_max_rounds: 2, a2a_max_criteria_per_trial: 5 },
      },
      760,
    )
  },

  health: () =>
    delay({
      status: 'ok' as const,
      service: 'Clinical Trial Screening API (mock)',
      version: '0.3.0-mock',
      data_counts: {
        patients: 1,
        encounters: 12,
        canonical_notes: 4,
        trials: TRIALS.length,
        criteria: TRIALS.reduce((sum, entry) => sum + entry.trial.criteria_count, 0),
      },
      rag_status: 'not_configured',
      graph_status: 'not_configured',
    }),

  listTrials: () => delay(TRIALS.map((entry) => entry.trial)),

  getPatient: (personId: number): Promise<PatientDetail> =>
    delay({
      patient: {
        person_id: personId,
        sex: 'F',
        birth_date: '1990-01-01',
        followup_start_date: '2019-03-11',
        index_date: INDEX_DATE,
        encounter_count: 12,
        synthetic_followup_count: 4,
        data_label: 'mock',
      },
      latest_event: null,
    }),

  runScreening: (personId: number, trialId: string) =>
    delay(buildRun(personId, trialId), 420),

  getScreening: (runId: string) => {
    const trialId = runId.replace(/^mock_/, '')
    return delay(buildRun(1, trialId), 120)
  },

  getQuestions: (personId: number, trialId?: string): Promise<EvidenceRequest[]> => {
    const target = trialId ?? TRIALS[1].trial.trial_id
    return delay(buildRun(personId, target).requests, 120)
  },

  submitAnswer: (
    personId: number,
    body: { run_id: string; criterion_id: string; value: string; request_id?: string | null },
  ): Promise<AnswerResponse> => {
    const trialId = body.run_id.replace(/^mock_/, '')
    const key = `${personId}:${trialId}`
    const set = answered.get(key) ?? new Set<string>()
    set.add(body.criterion_id)
    answered.set(key, set)
    return delay({
      answer_id: `ans_${set.size}`,
      run_id: body.run_id,
      person_id: personId,
      criterion_id: body.criterion_id,
      request_id: body.request_id ?? null,
      value: body.value,
      submitted_by: 'participant',
      submitted_at: new Date().toISOString(),
      intake: null,
    })
  },

  /**
   * 규칙 추출기의 아주 얕은 흉내. 실제 백엔드는 `agent/intake.py` 가
   * 근거 구간 검증까지 하고, 구간이 원문에 없으면 dropped 로 뺀다.
   */
  normalizeIntake: (text: string, referenceDate?: string | null): Promise<IntakeResult> => {
    const events: IntakeResult['events'] = []
    const reference = referenceDate ?? new Date().toISOString().slice(0, 10)

    const hba1c = text.match(/(?:당화혈색소|HbA1c|hba1c)\s*(?:가|이|는)?\s*([0-9]+(?:\.[0-9]+)?)/)
    if (hba1c) {
      events.push({
        event_type: 'MEASUREMENT',
        term: 'HbA1c',
        label: '당화혈색소',
        value: Number(hba1c[1]),
        unit: '%',
        occurred_at: null,
        date_precision: null,
        field: 'hba1c_pct',
        source_span: hba1c[0],
        confidence: 0.92,
        needs_review: true,
        origin: 'rule',
        notes: ['검사일이 문장에 없어 기준 기간 판단을 보류했습니다.'],
      })
    }

    for (const [pattern, term, label] of [
      [/메트포르민|metformin/i, 'metformin', '메트포르민'],
      [/인슐린|insulin/i, 'insulin', '인슐린'],
    ] as const) {
      const found = text.match(pattern)
      if (found) {
        events.push({
          event_type: 'MEDICATION',
          term,
          label,
          value: true,
          unit: null,
          occurred_at: null,
          date_precision: null,
          field: 'medications',
          source_span: found[0],
          confidence: 0.88,
          needs_review: false,
          origin: 'rule',
          notes: [],
        })
      }
    }

    const diagnosed = text.match(/((?:19|20)[0-9]{2})\s*년.{0,12}?(당뇨|고혈압|천식|아토피)/)
    if (diagnosed) {
      events.push({
        event_type: 'CONDITION',
        term: diagnosed[2],
        label: `${diagnosed[2]} 진단`,
        value: diagnosed[1],
        unit: null,
        occurred_at: `${diagnosed[1]}-01-01`,
        date_precision: 'APPROX',
        field: 'conditions',
        source_span: diagnosed[0],
        confidence: 0.96,
        needs_review: false,
        origin: 'rule',
        notes: [],
      })
    }

    const symptom = text.match(/(발이?\s*저[리린림][^.,]*|저림|어지러[움운][^.,]*)/)
    if (symptom) {
      events.push({
        event_type: 'ADVERSE_EVENT',
        term: symptom[0].trim(),
        label: '증상',
        value: null,
        unit: null,
        occurred_at: null,
        date_precision: null,
        field: null,
        source_span: symptom[0].trim(),
        confidence: 0.71,
        needs_review: true,
        origin: 'rule',
        notes: ['카탈로그에 없는 용어라 기록만 남깁니다.'],
      })
    }

    return delay(
      {
        text,
        reference_date: reference,
        mode: 'mock:rule',
        event_count: events.length,
        needs_review: events.some((event) => event.needs_review),
        events,
        dropped: [],
        input_tokens: 0,
        output_tokens: 0,
        error: null,
      },
      560,
    )
  },

  getCohort: (trialId: string): Promise<CohortResponse> =>
    delay({
      trial_id: trialId,
      total_screened: 0,
      funnel: [],
      bottlenecks: [],
      review_priority: [],
    }),
}
