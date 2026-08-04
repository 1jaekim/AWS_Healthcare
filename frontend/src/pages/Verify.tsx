/**
 * 이메일 인증 코드 확인 — 시안에 없는 화면.
 *
 * Cognito 는 가입 직후 계정이 `UNCONFIRMED` 상태이고, 이메일로 보낸 코드를
 * 확인해야 로그인할 수 있다. 시안은 2b(가입) → 2c(설문) 로 바로 넘어가지만
 * 그 사이에 이 단계가 필요하다.
 *
 * `VITE_AUTH_PROVIDER=local` 일 때는 가입이 곧바로 CONFIRMED 로 끝나므로
 * 이 화면을 거치지 않는다.
 */

import { useState, type FormEvent } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'

import { useAuth } from '../auth/AuthContext'
import { ErrorNote, Field, Steps, TextInput } from '../components/ui'

interface VerifyState {
  email?: string
  /** 확인 직후 자동 로그인하려고 가입 화면이 넘겨준다. */
  password?: string
}

export default function Verify() {
  const { confirmSignUp, resendCode, signIn } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const state = (location.state as VerifyState | null) ?? {}

  const [email, setEmail] = useState(state.email ?? '')
  const [code, setCode] = useState('')
  const [error, setError] = useState<unknown>(null)
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)

  async function handleSubmit(event: FormEvent) {
    event.preventDefault()
    setError(null)
    setNotice('')
    setBusy(true)
    try {
      await confirmSignUp(email, code)
      if (state.password) {
        // 가입 화면에서 넘어온 경우에만 자동 로그인한다. 링크를 직접 열어
        // 들어온 사용자는 비밀번호가 없으므로 로그인 화면으로 보낸다.
        await signIn(email, state.password)
        navigate('/signup/survey', { replace: true })
      } else {
        navigate('/login', { replace: true })
      }
    } catch (cause) {
      setError(cause)
    } finally {
      setBusy(false)
    }
  }

  async function handleResend() {
    setError(null)
    setNotice('')
    setBusy(true)
    try {
      await resendCode(email)
      setNotice('인증 코드를 다시 보냈습니다. 메일함을 확인해 주세요.')
    } catch (cause) {
      setError(cause)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="page-center">
      <form className="sheet sheet-narrow" onSubmit={handleSubmit}>
        <div className="row-baseline" style={{ marginBottom: 'var(--space-4)' }}>
          <h3 style={{ margin: 0 }}>이메일 인증</h3>
          <span className="kicker">Step 1 / 3</span>
        </div>
        <div style={{ marginBottom: 'var(--space-6)' }}>
          <Steps total={3} current={0} wide />
        </div>

        <p
          style={{
            fontSize: 14.5,
            lineHeight: 1.7,
            color: 'var(--color-neutral-800)',
            margin: '0 0 var(--space-6)',
            maxWidth: '34ch',
          }}
        >
          {email ? <strong>{email}</strong> : '가입하신 이메일'} 로 인증 코드를
          보냈습니다. 코드를 입력하면 가입이 완료됩니다.
        </p>

        <div className="stack">
          {state.email ? null : (
            <Field label="이메일" id="vf-email">
              <TextInput
                id="vf-email"
                value={email}
                onChange={setEmail}
                placeholder="name@hospital.kr"
                autoComplete="username"
              />
            </Field>
          )}
          <Field label="인증 코드" id="vf-code">
            <TextInput
              id="vf-code"
              value={code}
              onChange={setCode}
              placeholder="메일로 받은 6자리 숫자"
              autoComplete="one-time-code"
            />
          </Field>
        </div>

        <button
          className="btn btn-primary btn-block"
          type="submit"
          disabled={busy || !email || !code.trim()}
          style={{ minHeight: 46, fontSize: 15, marginTop: 'var(--space-6)' }}
        >
          {busy ? '확인 중' : '인증하고 계속하기'}
        </button>
        <button
          className="btn btn-ghost btn-block"
          type="button"
          disabled={busy || !email}
          onClick={() => void handleResend()}
          style={{ minHeight: 44 }}
        >
          코드 다시 받기
        </button>

        {notice ? (
          <div className="banner" style={{ marginTop: 'var(--space-3)' }} role="status">
            {notice}
          </div>
        ) : null}
        <ErrorNote error={error} />

        <p className="muted" style={{ fontSize: 12, margin: 'var(--space-6) 0 0' }}>
          메일이 오지 않으면 스팸함을 확인해 주세요.
        </p>
      </form>
    </div>
  )
}
