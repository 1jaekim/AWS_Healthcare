/** 시안 2b — 회원가입 · 정보 입력 + 개인정보 활용 동의 (Step 1/3). */

import { useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'

import { useAuth } from '../auth/AuthContext'
import {
  AGREEMENT_DEFS,
  REQUIRED_AGREEMENTS,
  type Agreements,
} from '../auth/types'
import { Check, ErrorNote, Field, Segmented, Steps, TextInput } from '../components/ui'

const SEX_OPTIONS = ['남', '여'] as const

export default function Signup() {
  const { signUp } = useAuth()
  const navigate = useNavigate()

  const [name, setName] = useState('')
  const [birth, setBirth] = useState('')
  const [sex, setSex] = useState<(typeof SEX_OPTIONS)[number] | ''>('')
  const [tel, setTel] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [passwordConfirm, setPasswordConfirm] = useState('')
  const [agreements, setAgreements] = useState<Agreements>({
    terms: false,
    privacy: false,
    health: false,
    mkt: false,
  })
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState(false)

  const allChecked = AGREEMENT_DEFS.every((def) => agreements[def.key])
  const requiredOk = REQUIRED_AGREEMENTS.every((key) => agreements[key])
  const passwordOk = password.length >= 8 && password === passwordConfirm
  const canSubmit = Boolean(name && email && passwordOk && requiredOk) && !busy

  function toggleAll() {
    const next = !allChecked
    setAgreements({ terms: next, privacy: next, health: next, mkt: next })
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault()
    setError(null)
    setBusy(true)
    try {
      const outcome = await signUp({
        email,
        password,
        name,
        birth,
        sex: sex === '남' ? 'M' : sex === '여' ? 'F' : '',
        tel,
        agreements,
      })
      if (outcome.status === 'NEEDS_CONFIRMATION') {
        // Cognito 는 이메일 코드 확인을 거쳐야 계정이 살아난다.
        // 확인 직후 자동 로그인하려고 비밀번호를 같이 넘긴다 (메모리 안, URL 아님).
        navigate('/signup/verify', {
          state: { email: outcome.email, password },
          replace: true,
        })
        return
      }
      navigate('/signup/survey')
    } catch (cause) {
      setError(cause)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="page-center">
      <form className="sheet sheet-form" onSubmit={handleSubmit}>
        <div className="row-baseline" style={{ marginBottom: 'var(--space-4)' }}>
          <h3 style={{ margin: 0 }}>회원가입</h3>
          <span className="kicker">Step 1 / 3</span>
        </div>
        <div style={{ marginBottom: 'var(--space-6)' }}>
          <Steps total={3} current={0} wide />
        </div>

        <div className="stack">
          <Field label="이름" id="su-name">
            <TextInput id="su-name" value={name} onChange={setName} placeholder="홍길동" />
          </Field>

          <div
            style={{
              display: 'grid',
              gridTemplateColumns: '1.3fr 1fr',
              gap: 'var(--space-3)',
              alignItems: 'end',
            }}
          >
            <Field label="생년월일" id="su-birth">
              <TextInput
                id="su-birth"
                value={birth}
                onChange={setBirth}
                placeholder="1990.01.01"
              />
            </Field>
            <div className="field">
              <label>성별</label>
              <Segmented
                name="sex"
                options={SEX_OPTIONS}
                value={sex}
                onChange={setSex}
                minHeight={42}
                grow
              />
            </div>
          </div>

          <div className="field">
            <label htmlFor="su-tel">휴대전화</label>
            <div style={{ display: 'flex', gap: 'var(--space-2)' }}>
              <TextInput
                id="su-tel"
                value={tel}
                onChange={setTel}
                placeholder="010-0000-0000"
              />
              <button
                className="btn btn-secondary"
                type="button"
                style={{ minHeight: 42, whiteSpace: 'nowrap' }}
              >
                인증 요청
              </button>
            </div>
          </div>

          <Field label="이메일 (아이디)" id="su-email">
            <TextInput
              id="su-email"
              value={email}
              onChange={setEmail}
              placeholder="name@hospital.kr"
              autoComplete="username"
            />
          </Field>
          <Field label="비밀번호" id="su-pw">
            <TextInput
              id="su-pw"
              type="password"
              value={password}
              onChange={setPassword}
              placeholder="8자 이상, 영문·숫자 조합"
              autoComplete="new-password"
            />
          </Field>
          <Field label="비밀번호 확인" id="su-pw2">
            <TextInput
              id="su-pw2"
              type="password"
              value={passwordConfirm}
              onChange={setPasswordConfirm}
              placeholder="비밀번호를 다시 입력"
              autoComplete="new-password"
            />
          </Field>
          {passwordConfirm && password !== passwordConfirm ? (
            <p className="muted" style={{ fontSize: 12.5, margin: 0 }}>
              비밀번호가 서로 다릅니다.
            </p>
          ) : null}
        </div>

        <div style={{ marginTop: 'var(--space-8)' }}>
          <h5 style={{ margin: '0 0 var(--space-3)' }}>약관 및 개인정보 활용 동의</h5>
          <div style={{ marginBottom: 'var(--space-3)' }}>
            <Check checked={allChecked} onChange={toggleAll} fontSize={15}>
              전체 동의
            </Check>
          </div>
          <div className="stack">
            {AGREEMENT_DEFS.map((def) => (
              <div
                key={def.key}
                style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)' }}
              >
                <div style={{ flex: 1 }}>
                  <Check
                    checked={agreements[def.key]}
                    onChange={(checked) =>
                      setAgreements((prev) => ({ ...prev, [def.key]: checked }))
                    }
                  >
                    <span
                      className={
                        def.tag === '필수' ? 'tag tag-accent' : 'tag tag-neutral'
                      }
                      style={{ marginRight: 'var(--space-2)' }}
                    >
                      {def.tag}
                    </span>
                    {def.label}
                  </Check>
                </div>
                <button
                  type="button"
                  className="btn btn-ghost"
                  style={{ fontSize: 12, color: 'var(--color-neutral-700)' }}
                >
                  보기
                </button>
              </div>
            ))}
          </div>

          <p
            style={{
              fontSize: 13,
              lineHeight: 1.7,
              color: 'var(--color-neutral-800)',
              margin: 'var(--space-4) 0 0',
              maxWidth: '52ch',
            }}
          >
            진료·검사 기록 등 건강정보(민감정보)는 임상시험 참여 가능 여부 판단에 한해
            처리되며, 보관 기간이 지나면 지체 없이 파기됩니다. 필수 항목에 동의하지
            않으면 회원가입이 제한됩니다.
          </p>
        </div>

        <button
          className="btn btn-primary btn-block"
          type="submit"
          disabled={!canSubmit}
          style={{ minHeight: 46, fontSize: 15, marginTop: 'var(--space-6)' }}
        >
          다음 · 간단 설문
        </button>

        <ErrorNote error={error} />
      </form>
    </div>
  )
}
