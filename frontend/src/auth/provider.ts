/**
 * 인증 제공자 선택.
 *
 * `VITE_AUTH_PROVIDER` 로 고른다.
 *
 *   cognito — 아키텍처 v2 의 목표 구성. User Pool 설정이 필요하다.
 *   local   — localStorage 자리표시자. User Pool 없이 화면을 돌려볼 때.
 *
 * 기본값이 local 인 이유: Cognito 설정이 없는 환경에서 앱이 로그인 화면에서
 * 멈춰버리면 나머지 화면을 아예 확인할 수 없다. 설정을 넣은 사람만 Cognito 를
 * 쓰게 한다.
 *
 * cognito 를 골랐는데 User Pool 값이 비어 있으면 조용히 local 로 내려앉지 않고
 * 즉시 실패한다. 진짜 인증인 줄 알고 배포하는 상황을 만들지 않기 위해서다.
 */

import { CognitoAuthProvider, configureCognito } from './cognitoProvider'
import { LocalAuthProvider } from './localProvider'
import type { AuthProvider } from './types'

const mode = (import.meta.env.VITE_AUTH_PROVIDER ?? 'local').trim().toLowerCase()

function buildProvider(): AuthProvider {
  if (mode !== 'cognito') return new LocalAuthProvider()

  const userPoolId = import.meta.env.VITE_COGNITO_USER_POOL_ID ?? ''
  const userPoolClientId = import.meta.env.VITE_COGNITO_CLIENT_ID ?? ''

  if (!userPoolId || !userPoolClientId) {
    throw new Error(
      'VITE_AUTH_PROVIDER=cognito 인데 VITE_COGNITO_USER_POOL_ID 또는 ' +
        'VITE_COGNITO_CLIENT_ID 가 비어 있습니다. ' +
        'cdk deploy HealthcareAuthStack 의 출력값을 넣고 다시 빌드하세요.',
    )
  }

  configureCognito({ userPoolId, userPoolClientId })
  return new CognitoAuthProvider()
}

export const authProvider: AuthProvider = buildProvider()

/** 화면에서 안내 문구를 가르기 위해 노출한다. */
export const isCognito = mode === 'cognito'
