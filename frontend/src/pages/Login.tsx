/** 시안 2a — 로그인. */

import { useState, type FormEvent } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'

import { useAuth } from '../auth/AuthContext'
import { Check, ErrorNote, Field, TextInput } from '../components/ui'

export default function Login() {
  const { signIn } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const from = (location.state as { from?: string } | null)?.from ?? '/home'

  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [remember, setRemember] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState(false)

  async function handleSubmit(event: FormEvent) {
    event.preventDefault()
    setError(null)
    setBusy(true)
    try {
      await signIn(email, password)
      navigate(from, { replace: true })
    } catch (cause) {
      setError(cause)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="page-center">
      <form className="sheet sheet-narrow" onSubmit={handleSubmit}>
        <div className="brand" style={{ marginBottom: 'var(--space-8)' }}>
          <span className="brand-name">병원명 의료원</span>
          <span className="kicker">Trial Matching</span>
        </div>

        <h2 style={{ fontSize: 32, margin: '0 0 var(--space-2)' }}>로그인</h2>
        <p
          style={{
            fontSize: 15,
            color: 'var(--color-neutral-800)',
            margin: '0 0 var(--space-6)',
            maxWidth: '30ch',
          }}
        >
          참여할 수 있는 임상시험을 찾아 가능 여부를 알려드립니다.
        </p>

        <div className="stack">
          <Field label="아이디 (이메일)" id="lg-id">
            <TextInput
              id="lg-id"
              value={email}
              onChange={setEmail}
              placeholder="name@hospital.kr"
              autoComplete="username"
            />
          </Field>
          <Field label="비밀번호" id="lg-pw">
            <TextInput
              id="lg-pw"
              type="password"
              value={password}
              onChange={setPassword}
              placeholder="••••••••"
              autoComplete="current-password"
            />
          </Field>
        </div>

        <div
          className="row-between"
          style={{ margin: 'var(--space-3) 0 var(--space-6)' }}
        >
          <Check checked={remember} onChange={setRemember}>
            로그인 상태 유지
          </Check>
          <Link to="/login" style={{ fontSize: 13, color: 'var(--color-accent-700)' }}>
            비밀번호 찾기
          </Link>
        </div>

        <button
          className="btn btn-primary btn-block"
          type="submit"
          disabled={busy || !email || !password}
          style={{ minHeight: 46, fontSize: 15, marginTop: 0 }}
        >
          {busy ? '확인 중' : '로그인'}
        </button>
        <button
          className="btn btn-secondary btn-block"
          type="button"
          onClick={() => navigate('/signup')}
          style={{ minHeight: 46, fontSize: 15, marginTop: 'var(--space-2)' }}
        >
          회원가입
        </button>

        <ErrorNote error={error} />

        <p
          className="muted"
          style={{ fontSize: 12, margin: 'var(--space-6) 0 0' }}
        >
          임상시험 참여 상담은 대표번호 02-000-0000 으로 문의하세요.
        </p>
      </form>
    </div>
  )
}
