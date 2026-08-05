/**
 * Cognito 인증 제공자.
 *
 * 아키텍처 v2 의 `Amazon Cognito` 자리다. User Pool 은
 * `backend/infra/auth_stack.py` 가 만든다.
 *
 * 프로필과 설문을 User Pool 커스텀 속성에 둔다. 아키텍처의 목표 저장소는
 * DynamoDB `UserProfileTable` 이며, 그 테이블과 API 가 생기면 이 파일에서
 * 속성 읽기·쓰기만 HTTP 호출로 바꾼다. 화면 코드는 손대지 않는다.
 *
 * 커스텀 속성의 제약:
 *   - 값은 문자열뿐이다. 배열(관심 분야)은 쉼표로 잇고, 불리언은 'true'/'false'.
 *   - User Pool 을 만들 때 정의한 것만 쓸 수 있고 나중에 삭제할 수 없다.
 *   - 길이 상한이 있다 (auth_stack.py 에 명시).
 */

import { Amplify } from 'aws-amplify'
import {
  confirmSignUp as amplifyConfirmSignUp,
  fetchAuthSession,
  fetchUserAttributes,
  getCurrentUser,
  resendSignUpCode,
  signIn as amplifySignIn,
  signOut as amplifySignOut,
  signUp as amplifySignUp,
  updateUserAttributes,
  type FetchUserAttributesOutput,
} from 'aws-amplify/auth'

import { config } from '../api/client'
import type {
  Account,
  AuthProvider,
  Survey,
  SignupInput,
  SignupOutcome,
} from './types'
import { EMPTY_SURVEY } from './types'

export interface CognitoConfig {
  userPoolId: string
  userPoolClientId: string
}

export function configureCognito(cognito: CognitoConfig): void {
  Amplify.configure({
    Auth: {
      Cognito: {
        userPoolId: cognito.userPoolId,
        userPoolClientId: cognito.userPoolClientId,
        // SPA 는 클라이언트 시크릿을 갖지 않는다. 번들에 넣으면 비밀이 아니다.
        signUpVerificationMethod: 'code',
      },
    },
  })
}

/** Cognito 는 `1990-01-01` 형태를 기대한다. 시안 입력은 `1990.01.01` 이다. */
function toIsoDate(value: string): string {
  const digits = value.replace(/[^0-9]/g, '')
  if (digits.length !== 8) return value.trim()
  return `${digits.slice(0, 4)}-${digits.slice(4, 6)}-${digits.slice(6, 8)}`
}

function fromIsoDate(value: string | undefined): string {
  if (!value) return ''
  return value.replace(/-/g, '.')
}

/** E.164 로 바꾼다. 한국 번호만 다룬다. 형식이 아니면 비워서 보내지 않는다. */
function toE164(tel: string): string | null {
  const digits = tel.replace(/[^0-9]/g, '')
  if (digits.startsWith('82')) return `+${digits}`
  if (digits.startsWith('0') && digits.length >= 10) return `+82${digits.slice(1)}`
  return null
}

function surveyFromAttributes(attributes: FetchUserAttributesOutput): Survey | null {
  const purpose = attributes['custom:survey_purpose'] ?? ''
  const areasRaw = attributes['custom:interest_areas'] ?? ''
  const medication = attributes['custom:survey_medication'] ?? ''
  const allergy = attributes['custom:survey_allergy'] ?? ''

  // 넷 다 비어 있으면 설문을 아직 안 낸 것이다. 빈 설문과 미제출을 구분한다.
  if (!purpose && !areasRaw && !medication && !allergy) return null

  return {
    ...EMPTY_SURVEY,
    purpose,
    areas: areasRaw ? areasRaw.split(',').filter(Boolean) : [],
    medication: (medication as Survey['medication']) || '',
    allergy,
  }
}

function accountFromAttributes(
  attributes: FetchUserAttributesOutput,
  isAdmin = false,
): Account {
  const personId = Number(attributes['custom:person_id'])
  return {
    email: attributes.email ?? '',
    name: attributes.name ?? '',
    birth: fromIsoDate(attributes.birthdate),
    sex: attributes.gender === 'M' || attributes.gender === 'F' ? attributes.gender : '',
    tel: attributes.phone_number ?? '',
    agreements: {
      // 필수 동의 없이는 가입 자체가 막히므로, 계정이 있다는 것은 세 건에
      // 동의했다는 뜻이다. 선택 동의만 따로 기록한다.
      terms: true,
      privacy: true,
      health: true,
      mkt: attributes['custom:marketing_opt_in'] === 'true',
    },
    survey: surveyFromAttributes(attributes),
    personId: Number.isFinite(personId) && personId > 0 ? personId : config.demoPersonId,
    createdAt: attributes['custom:agreed_at'] ?? '',
    isAdmin,
  }
}

export class CognitoAuthProvider implements AuthProvider {
  /** restore() 가 채운다. Cognito 조회는 비동기라 동기 getter 에는 캐시가 필요하다. */
  private cached: Account | null = null

  current(): Account | null {
    return this.cached
  }

  async restore(): Promise<Account | null> {
    try {
      await getCurrentUser()
      const attributes = await fetchUserAttributes()
      const session = await fetchAuthSession()
      const groups = session.tokens?.idToken?.payload['cognito:groups']
      const isAdmin = Array.isArray(groups) && groups.includes('admin')
      this.cached = accountFromAttributes(attributes, isAdmin)
      return this.cached
    } catch {
      // 로그인 상태가 아니면 예외가 난다. 오류가 아니라 정상 경로다.
      this.cached = null
      return null
    }
  }

  async signUp(input: SignupInput): Promise<SignupOutcome> {
    const email = input.email.trim().toLowerCase()
    const phone = toE164(input.tel)

    const { isSignUpComplete } = await amplifySignUp({
      username: email,
      password: input.password,
      options: {
        userAttributes: {
          email,
          name: input.name.trim(),
          ...(input.birth.trim() ? { birthdate: toIsoDate(input.birth) } : {}),
          ...(input.sex ? { gender: input.sex } : {}),
          // 형식이 맞지 않는 번호는 아예 보내지 않는다. Cognito 가 거부하면
          // 가입 전체가 실패한다.
          ...(phone ? { phone_number: phone } : {}),
          'custom:agreed_at': new Date().toISOString(),
          'custom:marketing_opt_in': String(input.agreements.mkt),
          // 환자 번호는 본인이 선택할 수 있는 값이 아니다. 가입 후 관리자 또는
          // 별도 서버 매핑 절차에서 검증된 번호만 넣는다.
        },
      },
    })

    if (isSignUpComplete) {
      const account = await this.signIn(email, input.password)
      return { status: 'CONFIRMED', account }
    }
    return { status: 'NEEDS_CONFIRMATION', email }
  }

  async confirmSignUp(email: string, code: string): Promise<void> {
    await amplifyConfirmSignUp({
      username: email.trim().toLowerCase(),
      confirmationCode: code.trim(),
    })
  }

  async resendCode(email: string): Promise<void> {
    await resendSignUpCode({ username: email.trim().toLowerCase() })
  }

  async signIn(email: string, password: string): Promise<Account> {
    const username = email.trim().toLowerCase()
    try {
      await amplifySignIn({ username, password })
    } catch (cause) {
      // 이미 로그인된 상태에서 다시 부르면 Amplify 가 던진다. 세션을 정리하고
      // 한 번만 재시도한다.
      if (
        cause instanceof Error &&
        cause.name === 'UserAlreadyAuthenticatedException'
      ) {
        await amplifySignOut()
        await amplifySignIn({ username, password })
      } else {
        throw cause
      }
    }
    const account = await this.restore()
    if (!account) throw new Error('로그인 후 사용자 정보를 불러오지 못했습니다.')
    return account
  }

  async signOut(): Promise<void> {
    await amplifySignOut()
    this.cached = null
  }

  async saveSurvey(survey: Survey): Promise<Account> {
    await updateUserAttributes({
      userAttributes: {
        'custom:survey_purpose': survey.purpose,
        // 쉼표로 잇는다. 분야 이름에 쉼표가 없다는 전제이며,
        // CLINICAL_AREAS 는 가운뎃점(·)만 쓴다.
        'custom:interest_areas': survey.areas.join(','),
        'custom:survey_medication': survey.medication,
        'custom:survey_allergy': survey.allergy,
      },
    })
    const account = await this.restore()
    if (!account) throw new Error('설문 저장 후 사용자 정보를 불러오지 못했습니다.')
    return account
  }

  async idToken(): Promise<string | null> {
    try {
      const session = await fetchAuthSession()
      return session.tokens?.idToken?.toString() ?? null
    } catch {
      return null
    }
  }
}
