/** 시안 2c — 간단 설문 4문항 (Step 2/3). */

import { useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'

import { useAuth } from '../auth/AuthContext'
import {
  CLINICAL_AREAS,
  EMPTY_SURVEY,
  SURVEY_PURPOSES,
  type Survey as SurveyValue,
} from '../auth/types'
import {
  Chips,
  ErrorNote,
  Field,
  Radio,
  Segmented,
  Steps,
  TextInput,
} from '../components/ui'

const MEDICATION_OPTIONS = ['없음', '있음', '잘 모름'] as const

export default function Survey() {
  const { account, saveSurvey } = useAuth()
  const navigate = useNavigate()
  const [survey, setSurvey] = useState<SurveyValue>(account?.survey ?? EMPTY_SURVEY)
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState(false)

  async function persist(value: SurveyValue) {
    setError(null)
    setBusy(true)
    try {
      await saveSurvey(value)
      navigate('/signup/done')
    } catch (cause) {
      setError(cause)
    } finally {
      setBusy(false)
    }
  }

  function handleSubmit(event: FormEvent) {
    event.preventDefault()
    void persist(survey)
  }

  return (
    <div className="page-center">
      <form className="sheet sheet-form" onSubmit={handleSubmit}>
        <div className="row-baseline" style={{ marginBottom: 'var(--space-4)' }}>
          <h3 style={{ margin: 0 }}>간단 설문</h3>
          <span className="kicker">Step 2 / 3</span>
        </div>
        <div style={{ marginBottom: 'var(--space-3)' }}>
          <Steps total={3} current={1} wide />
        </div>
        <p
          style={{
            fontSize: 14,
            color: 'var(--color-neutral-800)',
            margin: '0 0 var(--space-6)',
            maxWidth: '46ch',
          }}
        >
          참여 가능한 임상시험을 찾기 위해 네 가지만 확인합니다. 마이페이지에서 언제든
          수정할 수 있습니다.
        </p>

        <div style={{ marginBottom: 'var(--space-6)' }}>
          <h5 style={{ margin: '0 0 var(--space-3)' }}>
            <span style={{ color: 'var(--color-accent-700)', marginRight: 'var(--space-2)' }}>
              Q1
            </span>
            어떤 이유로 임상시험을 찾고 있나요?
          </h5>
          <div className="stack-tight">
            {SURVEY_PURPOSES.map((purpose) => (
              <Radio
                key={purpose}
                name="purpose"
                checked={survey.purpose === purpose}
                onChange={() => setSurvey((prev) => ({ ...prev, purpose }))}
              >
                {purpose}
              </Radio>
            ))}
          </div>
        </div>

        <div style={{ marginBottom: 'var(--space-6)' }}>
          <h5 style={{ margin: '0 0 var(--space-3)' }}>
            <span style={{ color: 'var(--color-accent-700)', marginRight: 'var(--space-2)' }}>
              Q2
            </span>
            관심 있는 임상 분야를 모두 선택해 주세요
          </h5>
          <Chips
            options={CLINICAL_AREAS}
            selected={survey.areas}
            onToggle={(area) =>
              setSurvey((prev) => ({
                ...prev,
                areas: prev.areas.includes(area)
                  ? prev.areas.filter((item) => item !== area)
                  : [...prev.areas, area],
              }))
            }
          />
        </div>

        <div style={{ marginBottom: 'var(--space-6)' }}>
          <h5 style={{ margin: '0 0 var(--space-3)' }}>
            <span style={{ color: 'var(--color-accent-700)', marginRight: 'var(--space-2)' }}>
              Q3
            </span>
            현재 복용 중인 약이 있나요?
          </h5>
          <Segmented
            name="med"
            options={MEDICATION_OPTIONS}
            value={survey.medication}
            onChange={(medication) => setSurvey((prev) => ({ ...prev, medication }))}
          />
        </div>

        <Field label="" id="su-allergy">
          <h5 style={{ margin: '0 0 var(--space-3)' }}>
            <span style={{ color: 'var(--color-accent-700)', marginRight: 'var(--space-2)' }}>
              Q4
            </span>
            알레르기나 특이사항{' '}
            <span
              className="muted"
              style={{ fontWeight: 400, fontSize: 13 }}
            >
              선택
            </span>
          </h5>
          <TextInput
            id="su-allergy"
            value={survey.allergy}
            onChange={(allergy) => setSurvey((prev) => ({ ...prev, allergy }))}
            placeholder="예: 페니실린 알레르기"
          />
        </Field>

        <div style={{ display: 'flex', gap: 'var(--space-2)', marginTop: 'var(--space-8)' }}>
          <button
            className="btn btn-secondary"
            type="button"
            disabled={busy}
            onClick={() => void persist(EMPTY_SURVEY)}
            style={{ minHeight: 46, width: 120, marginTop: 0 }}
          >
            건너뛰기
          </button>
          <button
            className="btn btn-primary"
            type="submit"
            disabled={busy}
            style={{ minHeight: 46, flex: 1, fontSize: 15, marginTop: 0 }}
          >
            설문 제출
          </button>
        </div>

        <ErrorNote error={error} />
      </form>
    </div>
  )
}
