import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import { Navigate, useLocation } from 'react-router-dom'

import { authProvider } from './provider'
import type { Account, SignupInput, SignupOutcome, Survey } from './types'

interface AuthValue {
  account: Account | null
  /** 세션 복원이 끝났는지. Cognito 는 비동기라 첫 렌더에서 아직 모른다. */
  ready: boolean
  signUp: (input: SignupInput) => Promise<SignupOutcome>
  confirmSignUp: (email: string, code: string) => Promise<void>
  resendCode: (email: string) => Promise<void>
  signIn: (email: string, password: string) => Promise<Account>
  signOut: () => Promise<void>
  saveSurvey: (survey: Survey) => Promise<Account>
}

const AuthContext = createContext<AuthValue | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [account, setAccount] = useState<Account | null>(() => authProvider.current())
  const [ready, setReady] = useState(false)

  // 새로고침 직후 저장된 세션을 되살린다. 이게 끝나기 전에 라우팅을 결정하면
  // 로그인한 사용자가 로그인 화면으로 튕긴다.
  useEffect(() => {
    let cancelled = false
    void authProvider
      .restore()
      .then((restored) => {
        if (!cancelled) setAccount(restored)
      })
      .finally(() => {
        if (!cancelled) setReady(true)
      })
    return () => {
      cancelled = true
    }
  }, [])

  const signUp = useCallback(async (input: SignupInput) => {
    const outcome = await authProvider.signUp(input)
    if (outcome.status === 'CONFIRMED') setAccount(outcome.account)
    return outcome
  }, [])

  const confirmSignUp = useCallback(async (email: string, code: string) => {
    await authProvider.confirmSignUp(email, code)
  }, [])

  const resendCode = useCallback(async (email: string) => {
    await authProvider.resendCode(email)
  }, [])

  const signIn = useCallback(async (email: string, password: string) => {
    const next = await authProvider.signIn(email, password)
    setAccount(next)
    return next
  }, [])

  const signOut = useCallback(async () => {
    await authProvider.signOut()
    setAccount(null)
  }, [])

  const saveSurvey = useCallback(async (survey: Survey) => {
    const next = await authProvider.saveSurvey(survey)
    setAccount(next)
    return next
  }, [])

  const value = useMemo<AuthValue>(
    () => ({
      account,
      ready,
      signUp,
      confirmSignUp,
      resendCode,
      signIn,
      signOut,
      saveSurvey,
    }),
    [account, ready, signUp, confirmSignUp, resendCode, signIn, signOut, saveSurvey],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthValue {
  const value = useContext(AuthContext)
  if (!value) throw new Error('useAuth 는 AuthProvider 안에서만 쓸 수 있습니다.')
  return value
}

/** 로그인한 사용자만 통과시킨다. 돌아올 위치를 state 에 남긴다. */
export function RequireAuth({ children }: { children: ReactNode }) {
  const { account, ready } = useAuth()
  const location = useLocation()

  // 세션 복원 중에는 판단하지 않는다.
  if (!ready) return null

  if (!account) {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />
  }
  return <>{children}</>
}
