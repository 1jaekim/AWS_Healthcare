/**
 * 인증 계약.
 *
 * 계정 저장소는 Cognito User Pool 이다. 백엔드(`backend/api`)는 계정을 갖지 않고
 * 토큰만 검증하며, 환자는 합성 EMR 의 `person_id`(정수)로 식별된다. 계정과 환자를
 * 잇는 값은 커스텀 속성 `custom:person_id` 다.
 *
 * 제공자를 갈아끼울 때 손댈 곳은 `provider.ts` 한 곳이다.
 */

export interface Survey {
  /** 어떤 이유로 임상시험을 찾는가 (시안 2c · Q1) */
  purpose: string
  /** 관심 임상 분야 (시안 2c · Q2, 복수 선택) */
  areas: string[]
  /** 복용 중인 약 (시안 2c · Q3) */
  medication: '없음' | '있음' | '잘 모름' | ''
  /** 알레르기·특이사항 (시안 2c · Q4, 선택) */
  allergy: string
}

export interface Agreements {
  terms: boolean
  privacy: boolean
  health: boolean
  mkt: boolean
}

export interface Account {
  email: string
  name: string
  birth: string
  sex: 'M' | 'F' | ''
  tel: string
  agreements: Agreements
  survey: Survey | null
  /**
   * 이 계정이 바인딩된 합성 EMR 환자 번호.
   * 백엔드 API 는 전부 person_id 로 말하기 때문에 이 연결이 필요하다.
   */
  personId: number
  createdAt: string
}

export interface SignupInput {
  email: string
  password: string
  name: string
  birth: string
  sex: 'M' | 'F' | ''
  tel: string
  agreements: Agreements
}

/**
 * 가입 결과.
 *
 * Cognito 는 이메일로 보낸 코드를 확인해야 계정이 살아난다. 시안(2b → 2c)에는
 * 그 단계가 없어서 화면을 하나 더 뒀다. 로컬 제공자는 확인이 필요 없으므로
 * 항상 `CONFIRMED` 를 돌려주고, 화면은 그 값만 보고 분기한다.
 */
export type SignupOutcome =
  | { status: 'CONFIRMED'; account: Account }
  | { status: 'NEEDS_CONFIRMATION'; email: string }

export interface AuthProvider {
  /** 저장된 세션. 비동기 복원이 필요한 제공자를 위해 restore() 와 짝을 이룬다. */
  current(): Account | null
  /** 새로고침 직후 세션을 되살린다. 없으면 null. */
  restore(): Promise<Account | null>
  signUp(input: SignupInput): Promise<SignupOutcome>
  /** 이메일로 받은 인증 코드 확인. 로컬 제공자에서는 아무 일도 하지 않는다. */
  confirmSignUp(email: string, code: string): Promise<void>
  /** 인증 코드 재발송. */
  resendCode(email: string): Promise<void>
  signIn(email: string, password: string): Promise<Account>
  signOut(): Promise<void>
  saveSurvey(survey: Survey): Promise<Account>
  /**
   * 백엔드 호출에 붙일 ID 토큰. 없으면 null.
   *
   * 백엔드(`backend/api/app/auth/`)가 이 토큰을 검증한다. `local` 제공자는
   * 토큰을 만들 수 없으므로 null 을 돌려주고, 보호된 백엔드에서는 401 이 된다.
   */
  idToken(): Promise<string | null>
}

export const EMPTY_SURVEY: Survey = {
  purpose: '',
  areas: [],
  medication: '',
  allergy: '',
}

export const REQUIRED_AGREEMENTS = ['terms', 'privacy', 'health'] as const

export const AGREEMENT_DEFS = [
  { key: 'terms', tag: '필수', label: '이용약관 동의' },
  { key: 'privacy', tag: '필수', label: '개인정보 수집·이용 동의' },
  { key: 'health', tag: '필수', label: '민감정보(건강정보) 처리 동의' },
  { key: 'mkt', tag: '선택', label: '임상시험 모집 안내 수신' },
] as const satisfies readonly { key: keyof Agreements; tag: string; label: string }[]

export const CLINICAL_AREAS = [
  '당뇨 · 내분비',
  '심혈관',
  '소화기',
  '근골격 · 관절',
  '호흡기 · 알레르기',
  '피부',
  '신경',
  '정신건강',
  '암',
]

export const SURVEY_PURPOSES = [
  '처음 임상시험을 찾아봅니다',
  '특정 질환의 임상시험을 찾습니다',
  '참여 가능 여부만 확인하고 싶습니다',
  '이전에 임상시험에 참여한 적이 있습니다',
]
