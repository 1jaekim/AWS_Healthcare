/**
 * 시안 6a → 6b — 공고 하나에 대한 자연어 지원서.
 *
 * 홈에서 추천 공고를 클릭하면 이 화면으로 들어온다. 공고의 선정·제외 기준에서
 * 파생된 항목을 묻고, 서술에서 빠진 항목만 이어서 확인한다.
 *
 *   공고 클릭
 *   → POST /trials/{trial_id}/application-schema  (기준 → 지원서 스키마)
 *   → POST /applications                          (첫 서술 제출)
 *   → POST /applications/{id}/responses           (누락 항목 재질문, 최대 5회)
 *   → COMPLETE
 *   → POST /applications/{id}/screening           (환자 기록 연결 + 판정 실행)
 *
 * 이 단계는 값을 모을 뿐 적격 여부를 판단하지 않는다. 판정은 마지막 screening
 * 호출부터 시작한다.
 *
 * 빈 배열(`알레르기 없음`)과 `false`(`과거 참여 없음`)는 유효한 답변이며 누락으로
 * 처리되지 않는다. 그래서 화면도 "없음" 답변을 미응답처럼 보여주지 않는다.
 */

import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'

import { api } from '../api/endpoints'
import { TRIAL_META } from '../api/mock'
import type { ApplicationIntake, ApplicationSchema } from '../api/types'
import AppHeader from '../components/AppHeader'
import { ErrorNote, Loading, TextInput } from '../components/ui'
import { useAuth } from '../auth/AuthContext'
import { useMatching } from '../state/MatchingContext'

const MAX_LENGTH = 2000

const TIPS = [
  '진단 시기 — "2019년에 진단받았습니다"',
  '검사 수치와 시점 — "지난달 당화혈색소 7.8"',
  '복용 약 — "메트포르민 하루 두 번"',
  '최근 변화 — "두 달 전부터 발이 저립니다"',
]

const PLACEHOLDER =
  '예) 1990년생 여자입니다. 2019년에 2형 당뇨 진단받고 메트포르민 복용 중입니다. ' +
  '지난달 검사에서 당화혈색소가 7.8이었고, 최근에는 발이 저린 느낌이 있습니다. ' +
  '알레르기는 없고 임상시험에 참여한 적은 없습니다.'

interface ChatTurn {
  who: 'system' | 'me'
  text: string
  note?: string
}

/** 지원서 스키마의 프로퍼티 한 건. 백엔드가 붙이는 x- 확장을 함께 읽는다. */
interface SchemaProperty {
  title?: string
  description?: string
  type?: string
  'x-source'?: string
  'x-unit'?: string
  'x-criterion-field'?: string
}

/** 지원서에 채워진 값을 사람이 읽는 한 줄로. */
function renderValue(value: unknown): string {
  if (Array.isArray(value)) return value.length ? value.join(', ') : '없음'
  if (typeof value === 'boolean') return value ? '있음' : '없음'
  if (value === null || value === undefined) return '—'
  return String(value)
}

export default function Intake() {
  const { account } = useAuth()
  const navigate = useNavigate()
  const params = useParams<{ trialId: string }>()
  const { trials, recommendation, loadTrials } = useMatching()
  const personId = account?.personId ?? 1

  const [stage, setStage] = useState<'write' | 'review'>('write')
  const [text, setText] = useState('')
  const [schema, setSchema] = useState<ApplicationSchema | null>(null)
  const [preparing, setPreparing] = useState(false)
  const [intake, setIntake] = useState<ApplicationIntake | null>(null)
  const [turns, setTurns] = useState<ChatTurn[]>([])
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const logRef = useRef<HTMLDivElement>(null)

  // 공고 없이 /intake 로 들어오면 추천 1순위를 쓴다. 링크를 잘못 눌러 빈 화면을
  // 보는 것보다 낫다.
  const trialId =
    params.trialId ??
    recommendation?.recommended_trials[0]?.trial_id ??
    trials[0]?.trial_id

  const trialName =
    trials.find((trial) => trial.trial_id === trialId)?.trial_name ??
    recommendation?.recommended_trials.find((trial) => trial.trial_id === trialId)
      ?.title ??
    trialId ??
    ''

  useEffect(() => {
    if (!trials.length) void loadTrials()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  /**
   * 공고 기준으로 지원서 스키마를 준비한다. 같은 공고면 같은 schema_id 가 오므로
   * 다시 들어와도 스키마가 늘어나지 않는다.
   */
  useEffect(() => {
    if (!trialId || schema?.trial_id === trialId) return
    let cancelled = false
    setPreparing(true)
    setError(null)
    void api
      .prepareTrialApplicationSchema(trialId)
      .then((next) => {
        if (cancelled) return
        setSchema(next)
        setIntake(null)
        setTurns([])
        setStage('write')
      })
      .catch((cause) => {
        if (!cancelled) setError(cause)
      })
      .finally(() => {
        if (!cancelled) setPreparing(false)
      })
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [trialId])

  // 새 말풍선이 붙으면 아래로 따라간다.
  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight, behavior: 'smooth' })
  }, [turns])

  const properties = useMemo<Record<string, SchemaProperty>>(() => {
    const shape = schema?.json_schema as
      | { properties?: Record<string, SchemaProperty> }
      | undefined
    return shape?.properties ?? {}
  }, [schema])

  /** 이 공고의 기준에서 파생된 항목. 기본 6항목과 구분해서 보여준다. */
  const criteriaFields = useMemo(
    () =>
      Object.entries(properties).filter(
        ([, spec]) => spec['x-source'] === 'trial_notice',
      ),
    [properties],
  )

  const titleOf = (name: string) => properties[name]?.title ?? name

  function speak(next: ApplicationIntake, prefix?: string) {
    const lines: ChatTurn[] = []
    if (prefix) lines.push({ who: 'system', text: prefix })

    if (next.status === 'NEEDS_MORE_INFO' && next.follow_up_prompt) {
      lines.push({
        who: 'system',
        text: next.follow_up_prompt,
        note: `재질문 ${next.follow_up_count} / ${next.max_follow_ups} · 남은 항목 ${next.missing_fields.length}개`,
      })
    } else if (next.status === 'COMPLETE') {
      lines.push({
        who: 'system',
        text: '이 공고가 요구하는 항목이 모두 채워졌습니다. 이제 기록과 대조해 참여 가능 여부를 확인하겠습니다.',
      })
    } else {
      lines.push({
        who: 'system',
        text: `재질문 ${next.max_follow_ups}회를 모두 사용했습니다. 남은 항목은 연구간호사가 직접 확인합니다.`,
        note: `미확인 ${next.missing_fields.map((field) => field.title).join(', ')}`,
      })
    }
    setTurns((prev) => [...prev, ...lines])
  }

  async function handleStart() {
    if (!schema) return
    setBusy(true)
    setError(null)
    try {
      const next = await api.startApplication(schema.schema_id, text)
      setIntake(next)
      setStage('review')
      setTurns([])
      speak(
        next,
        `적어주신 내용에서 ${Object.keys(next.data).length}가지 항목을 확인했습니다. 왼쪽 목록에서 확인하실 수 있습니다.`,
      )
    } catch (cause) {
      setError(cause)
    } finally {
      setBusy(false)
    }
  }

  async function answer(value: string) {
    const trimmed = value.trim()
    if (!intake || !trimmed || intake.status !== 'NEEDS_MORE_INFO') return

    setTurns((prev) => [...prev, { who: 'me', text: trimmed }])
    setDraft('')
    setBusy(true)
    setError(null)
    try {
      const next = await api.answerApplication(intake.application_id, trimmed)
      setIntake(next)
      speak(next)
    } catch (cause) {
      setError(cause)
    } finally {
      setBusy(false)
    }
  }

  async function handleScreen() {
    if (!intake || intake.status !== 'COMPLETE') return
    setBusy(true)
    setError(null)
    try {
      await api.screenApplication(intake.application_id, personId)
      // 판정은 매칭 화면에서 실행하고, 끝나면 이 공고의 결과로 바로 보낸다.
      navigate(`/matching?trial=${encodeURIComponent(intake.trial_id)}`)
    } catch (cause) {
      setError(cause)
    } finally {
      setBusy(false)
    }
  }

  // -- 공고를 못 찾은 경우 ----------------------------------------------------

  if (!trialId) {
    return (
      <div className="page-center">
        <div className="sheet" style={{ width: 460, padding: 'var(--space-8)' }}>
          <h3 style={{ margin: '0 0 var(--space-2)' }}>공고를 먼저 선택해 주세요</h3>
          <p style={{ fontSize: 14, color: 'var(--color-neutral-800)' }}>
            지원서 항목은 공고의 선정·제외 기준에서 만들어집니다. 홈에서 공고를
            고르면 그 공고에 맞는 질문으로 시작합니다.
          </p>
          <ErrorNote error={error} />
          <button
            className="btn btn-primary btn-block"
            type="button"
            style={{ minHeight: 44 }}
            onClick={() => navigate('/home')}
          >
            공고 목록으로
          </button>
        </div>
      </div>
    )
  }

  // -- 1단계: 서술형 한 문항 -------------------------------------------------

  if (stage === 'write') {
    return (
      <div className="page-wide">
        <div
          className="sheet"
          style={{ maxWidth: 720, margin: '0 auto', padding: 'var(--space-8)' }}
        >
          <div className="row-baseline" style={{ marginBottom: 'var(--space-4)' }}>
            <span className="kicker">1단계 · 서술</span>
            <span className="muted" style={{ fontSize: 12.5 }}>
              2단계 · 챗으로 이어서 확인
            </span>
          </div>

          <div className="kicker" style={{ color: 'var(--color-accent-700)' }}>
            지원 공고
          </div>
          <h3 style={{ margin: '0 0 var(--space-1)', fontSize: 19 }}>{trialName}</h3>
          <div className="muted" style={{ fontSize: 12.5, marginBottom: 'var(--space-4)' }}>
            {TRIAL_META[trialId] ?? trialId}
          </div>

          <h2 style={{ margin: '0 0 var(--space-3)', fontSize: 28, maxWidth: '24ch' }}>
            현재 건강 상태를 편하게 적어주세요
          </h2>
          <p
            style={{
              fontSize: 14.5,
              lineHeight: 1.7,
              color: 'var(--color-neutral-800)',
              margin: '0 0 var(--space-4)',
              maxWidth: '52ch',
            }}
          >
            진단받은 질환, 복용 중인 약, 최근 받은 검사와 수치, 불편한 증상을 아는
            만큼만 적으시면 됩니다. 형식은 신경 쓰지 않으셔도 됩니다. 적어주신
            내용에서 항목을 뽑아낸 뒤, 빠진 정보만 이어서 여쭤봅니다.
          </p>

          {preparing ? <Loading label="공고 기준을 불러오는 중입니다" /> : null}

          {criteriaFields.length ? (
            <div
              className="stack-tight"
              style={{
                border: '1px solid var(--color-neutral-300)',
                padding: 'var(--space-3)',
                marginBottom: 'var(--space-4)',
              }}
            >
              <div className="kicker">이 공고가 추가로 확인하는 항목</div>
              <div className="inline-wrap">
                {criteriaFields.map(([name, spec]) => (
                  <span key={name} className="tag">
                    {spec.title ?? name}
                    {spec['x-unit'] ? ` (${spec['x-unit']})` : ''}
                  </span>
                ))}
              </div>
              <div className="muted" style={{ fontSize: 12 }}>
                공고의 선정·제외 기준에서 만들어진 항목입니다. 모르시면 비워두셔도
                되고, 이어지는 질문에서 다시 확인합니다.
              </div>
            </div>
          ) : null}

          <textarea
            className="input"
            rows={9}
            value={text}
            maxLength={MAX_LENGTH}
            onChange={(event) => setText(event.target.value)}
            placeholder={PLACEHOLDER}
            style={{
              minHeight: 200,
              lineHeight: 1.7,
              fontSize: 14.5,
              padding: 'var(--space-3)',
            }}
          />

          <div className="row-between" style={{ marginTop: 'var(--space-2)' }}>
            <span className="muted" style={{ fontSize: 12 }}>
              {text.length} / {MAX_LENGTH}자 · 이름이나 연락처는 적지 않으셔도 됩니다
            </span>
          </div>

          <div style={{ height: 'var(--space-6)' }} />

          <div className="kicker" style={{ marginBottom: 'var(--space-2)' }}>
            이렇게 적으면 좋습니다
          </div>
          <div
            className="stack-tight"
            style={{ fontSize: 13.5, color: 'var(--color-neutral-800)' }}
          >
            {TIPS.map((tip) => (
              <div key={tip}>{tip}</div>
            ))}
          </div>

          {schema?.notice_text ? (
            <details style={{ marginTop: 'var(--space-6)' }}>
              <summary style={{ fontSize: 13, cursor: 'pointer' }}>
                공고 요약과 기준 보기
              </summary>
              <pre
                className="muted"
                style={{
                  fontSize: 12.5,
                  lineHeight: 1.7,
                  whiteSpace: 'pre-wrap',
                  margin: 'var(--space-2) 0 0',
                  fontFamily: 'inherit',
                }}
              >
                {schema.notice_text}
              </pre>
            </details>
          ) : null}

          <button
            className="btn btn-primary btn-block"
            type="button"
            disabled={busy || preparing || !schema || !text.trim()}
            onClick={() => void handleStart()}
            style={{ minHeight: 48, fontSize: 15, marginTop: 'var(--space-8)' }}
          >
            {busy ? '항목을 뽑는 중입니다' : '적은 내용으로 시작하기'}
          </button>

          <ErrorNote error={error} />

          <button
            type="button"
            className="btn btn-ghost"
            style={{
              fontSize: 12.5,
              color: 'var(--color-neutral-700)',
              padding: 0,
              marginTop: 'var(--space-4)',
            }}
            onClick={() => navigate('/home')}
          >
            다른 공고 고르기
          </button>

          <p className="muted" style={{ fontSize: 12, margin: 'var(--space-4) 0 0' }}>
            이름과 연락처 같은 직접 식별정보는 지원 자격 정보에 포함되지 않습니다.
          </p>
        </div>
      </div>
    )
  }

  // -- 2단계: 뽑아낸 항목 확인 + 누락분 재질문 --------------------------------

  const filled = intake ? Object.entries(intake.data) : []
  const asking = intake?.status === 'NEEDS_MORE_INFO'
  const complete = intake?.status === 'COMPLETE'
  const exhausted = intake?.status === 'MAX_FOLLOW_UPS_REACHED'

  return (
    <div className="page-wide">
      <div className="sheet sheet-wide">
        <AppHeader />
        <div className="chat-shell">
          <aside className="chat-side">
            <div className="kicker" style={{ color: 'var(--color-accent-700)' }}>
              지원 공고
            </div>
            <h4 style={{ margin: '0 0 var(--space-4)', fontSize: 15 }}>{trialName}</h4>

            <div className="kicker" style={{ marginBottom: 'var(--space-2)' }}>
              서술에서 뽑아낸 항목
            </div>
            <h4 style={{ margin: '0 0 var(--space-4)' }}>
              {filled.length}개 항목
              {intake?.missing_fields.length
                ? ` · 확인필요 ${intake.missing_fields.length}개`
                : ''}
            </h4>

            <div className="stack-loose" style={{ fontSize: 13.5 }}>
              {filled.map(([name, value]) => (
                <div key={name}>
                  <div style={{ fontWeight: 600 }}>
                    {renderValue(value)}
                    {properties[name]?.['x-unit'] &&
                    typeof value === 'number' &&
                    properties[name]?.['x-unit'] !== 'category'
                      ? ` ${properties[name]?.['x-unit']}`
                      : ''}
                  </div>
                  <div className="muted" style={{ fontSize: 12, marginTop: 2 }}>
                    {titleOf(name)}
                    {properties[name]?.['x-source'] === 'trial_notice'
                      ? ' · 공고 기준'
                      : ''}
                  </div>
                </div>
              ))}

              {intake?.missing_fields.map((field) => (
                <div key={field.name}>
                  <div className="dotted" style={{ fontWeight: 600 }}>
                    {field.title}
                  </div>
                  <div className="muted" style={{ fontSize: 12, marginTop: 2 }}>
                    {exhausted ? '재질문 상한 초과 · 사람 확인' : '서술에 없음 · 질문 예정'}
                  </div>
                </div>
              ))}
            </div>

            <div style={{ height: 'var(--space-6)' }} />
            <button
              type="button"
              className="btn btn-ghost"
              style={{ fontSize: 12.5, color: 'var(--color-accent-700)', padding: 0 }}
              onClick={() => {
                setStage('write')
                setIntake(null)
                setTurns([])
              }}
            >
              서술 내용 다시 적기
            </button>
          </aside>

          <div className="chat-main">
            <div
              className="row-baseline"
              style={{ padding: 'var(--space-4) var(--space-6) var(--space-3)' }}
            >
              <h4 style={{ margin: 0 }}>빠진 정보만 이어서 확인</h4>
              <span className="muted" style={{ fontSize: 12.5 }}>
                재질문 {intake?.follow_up_count ?? 0} / {intake?.max_follow_ups ?? 5}
              </span>
            </div>

            <div className="steps" style={{ padding: '0 var(--space-6) var(--space-4)' }}>
              {Array.from({ length: intake?.max_follow_ups ?? 5 }, (_, index) => {
                const used = intake?.follow_up_count ?? 0
                return (
                  <div
                    key={index}
                    className={
                      index < used ? 'step done' : index === used ? 'step current' : 'step'
                    }
                  />
                )
              })}
            </div>

            <div className="chat-log" ref={logRef}>
              {turns.map((turn, index) => (
                <div key={index} className={turn.who === 'me' ? 'bubble mine' : 'bubble'}>
                  <div className="bubble-body">{turn.text}</div>
                  {turn.note ? <div className="bubble-note">{turn.note}</div> : null}
                </div>
              ))}

              {busy ? <Loading label="확인하는 중입니다" /> : null}
              <ErrorNote error={error} />

              {complete ? (
                <button
                  className="btn btn-primary"
                  type="button"
                  disabled={busy}
                  style={{ minHeight: 42, marginTop: 0, alignSelf: 'flex-start' }}
                  onClick={() => void handleScreen()}
                >
                  이 내용으로 참여 가능 확인하기
                </button>
              ) : null}

              {exhausted ? (
                <button
                  className="btn btn-secondary"
                  type="button"
                  style={{ minHeight: 42, marginTop: 0, alignSelf: 'flex-start' }}
                  onClick={() => navigate('/home')}
                >
                  홈으로 돌아가기
                </button>
              ) : null}
            </div>

            <div className="chat-compose">
              <TextInput
                value={draft}
                onChange={setDraft}
                placeholder={
                  asking ? '답변을 적어주세요' : '더 이상 답변할 항목이 없습니다'
                }
                minHeight={44}
              />
              <button
                className="btn btn-primary"
                type="button"
                disabled={busy || !asking || !draft.trim()}
                style={{ minHeight: 44, marginTop: 0 }}
                onClick={() => void answer(draft)}
              >
                보내기
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
