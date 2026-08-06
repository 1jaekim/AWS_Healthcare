/** 시안 5a — 로그인 후 홈. 위쪽 추천 공고, 아래로 스크롤하면 전체 모집공고. */

import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'

import { TRIAL_META } from '../api/mock'
import type { RecommendedTrial, TrialSummary } from '../api/types'
import AppHeader from '../components/AppHeader'
import { Chips, ErrorNote, Loading } from '../components/ui'
import { useAuth } from '../auth/AuthContext'
import {
  allTrials,
  useMatching,
  verdictClass,
  verdictOf,
} from '../state/MatchingContext'

const ALL = '전체'

export default function Home() {
  const { account } = useAuth()
  const navigate = useNavigate()
  const { trials, recommendation, busy, error, ranAt, loadTrials, run } = useMatching()
  const [filter, setFilter] = useState(ALL)
  const [showAllFields, setShowAllFields] = useState(false)

  const personId = account?.personId ?? null

  useEffect(() => {
    if (recommendation) return
    let cancelled = false
    void (async () => {
      await loadTrials()
      if (cancelled) return
      if (personId !== null) await run(personId, 3)
    })()
    return () => {
      cancelled = true
    }
    // 최초 진입에서 한 번만. 재실행은 아래 버튼으로 명시적으로 한다.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [personId])

  /** 공고 분야. `/trials` 의 purpose 를 trial_id 로 붙인다. */
  const purposeOf = useMemo(() => {
    const map = new Map(trials.map((trial) => [trial.trial_id, trial.purpose]))
    return (trialId: string) => map.get(trialId) ?? '기타'
  }, [trials])

  const everything = allTrials(recommendation)
  const verdictByTrial = useMemo(
    () => new Map(everything.map((trial) => [trial.trial_id, trial])),
    [everything],
  )
  const fields = useMemo(
    () => [ALL, ...Array.from(new Set(trials.map((t) => purposeOf(t.trial_id))))],
    [trials, purposeOf],
  )
  const visibleFields = useMemo(() => {
    if (showAllFields || fields.length <= 6) return fields
    const compact = fields.slice(0, 6)
    if (!compact.includes(filter)) compact.push(filter)
    return compact
  }, [fields, filter, showAllFields])

  const top = recommendation?.recommended_trials ?? []
  // 헤드라인은 "가능" 으로 확정된 건수만 센다. 판정불가를 섞으면 가능한 것처럼 읽힌다.
  const possibleCount = everything.filter((trial) => verdictOf(trial) === '가능').length
  // "전체 모집공고"는 추천 실행 결과가 아니라 승인 공고 원장을 기준으로 한다.
  // 승인 직후 아직 판정되지 않은 공고도 여기에는 즉시 보여야 한다.
  const shown = trials.filter(
    (trial) => filter === ALL || purposeOf(trial.trial_id) === filter,
  )
  const openCount = everything.reduce(
    (sum, trial) => sum + trial.unresolved_criteria.length,
    0,
  )
  const areas = account?.survey?.areas ?? []

  /** 기준별 판정 상세. */
  function goDetail(trial: RecommendedTrial) {
    navigate(`/results/${encodeURIComponent(trial.trial_id)}`)
  }

  /**
   * 공고를 클릭하면 그 공고의 지원서 챗으로 들어간다. 공고의 선정·제외 기준에서
   * 파생된 항목을 묻고, 빠진 항목만 이어서 확인한다.
   *
   * 불가능으로 확정된 공고는 지원서를 받을 이유가 없으므로 판정 근거로 보낸다.
   *
   * 분기 기준은 배지와 같은 `verdictOf()` 다. 예전에는 `overall_status` 를 봤는데,
   * 그 값에는 A2A 합의가 반영되어 배지(screening_decision 기반)와 갈릴 수 있었다.
   * 배지는 "판정불가" 인데 클릭하면 제외된 공고처럼 막히는 상태가 가능했다.
   * 화면의 판정 기준은 하나여야 한다.
   */
  function openTrial(trial: RecommendedTrial) {
    if (verdictOf(trial) === '불가능') {
      goDetail(trial)
      return
    }
    navigate(`/intake/${encodeURIComponent(trial.trial_id)}`)
  }

  function openListedTrial(trial: TrialSummary) {
    const verdict = verdictByTrial.get(trial.trial_id)
    if (verdict) openTrial(verdict)
    else navigate(`/intake/${encodeURIComponent(trial.trial_id)}`)
  }

  return (
    <div className="page-wide">
      <div className="sheet sheet-wide">
        <AppHeader />

        <div style={{ padding: 'var(--space-6) var(--space-8) var(--space-8)' }}>
          <h2 style={{ margin: '0 0 var(--space-2)', fontSize: 30, maxWidth: '26ch' }}>
            {busy ? '참여 가능 여부를 확인하고 있습니다' : `참여 가능한 공고 ${possibleCount}건`}
          </h2>

          <div
            className="inline-wrap"
            style={{ alignItems: 'center', marginBottom: 'var(--space-2)' }}
          >
            <span className="muted" style={{ fontSize: 13 }}>
              관심 분야
            </span>
            {areas.length ? (
              areas.map((area) => (
                <span key={area} className="tag tag-accent">
                  {area}
                </span>
              ))
            ) : (
              <span className="muted" style={{ fontSize: 13 }}>
                선택하지 않았습니다
              </span>
            )}
            <Link
              to="/signup/survey"
              style={{ fontSize: 12, color: 'var(--color-neutral-700)' }}
            >
              수정
            </Link>
          </div>

          <p
            style={{
              fontSize: 13.5,
              color: 'var(--color-neutral-800)',
              margin: '0 0 var(--space-6)',
            }}
          >
            {ranAt && recommendation
              ? `${ranAt.toLocaleString('ko-KR')} 실행 · 공고 ${recommendation.evaluated_trials}건을 기준과 대조했습니다.`
              : '실행 준비 중입니다.'}{' '}
            {openCount > 0 && top[0] ? (
              <Link
                to={`/intake/${encodeURIComponent(top[0].trial_id)}`}
                style={{ color: 'var(--color-accent-700)' }}
              >
                판정불가 항목 답하기
              </Link>
            ) : null}
          </p>

          {busy ? <Loading label="공고를 대조하는 중입니다" /> : null}
          <ErrorNote error={error} />

          <div className="stack-loose">
            {top.map((trial) => (
              <button
                key={trial.trial_id}
                type="button"
                className="list-row list-row-top"
                onClick={() => openTrial(trial)}
              >
                <div>
                  <div className="row-title">{trial.title}</div>
                  <div className="row-meta">
                    {TRIAL_META[trial.trial_id] ?? trial.trial_id}
                  </div>
                </div>
                <div />
                <div style={{ textAlign: 'right' }}>
                  <span className={verdictClass(verdictOf(trial))}>
                    {verdictOf(trial)}
                  </span>
                </div>
              </button>
            ))}
          </div>

          <div style={{ display: 'flex', gap: 'var(--space-2)', marginTop: 'var(--space-4)' }}>
            <button
              className="btn btn-primary"
              type="button"
              disabled={!top.length}
              style={{ minHeight: 42, marginTop: 0 }}
              onClick={() => top[0] && openTrial(top[0])}
            >
              지원서 작성
            </button>
            <button
              className="btn btn-secondary"
              type="button"
              style={{ minHeight: 42, marginTop: 0 }}
              onClick={() => navigate('/results')}
            >
              판정 근거 보기
            </button>
            <button
              className="btn btn-ghost"
              type="button"
              disabled={busy || personId === null}
              style={{ minHeight: 42, marginTop: 0 }}
              onClick={() => void run(personId, 3)}
            >
              다시 실행
            </button>
          </div>

          {recommendation && recommendation.remaining_candidate_count > 0 ? (
            <p className="muted" style={{ fontSize: 12.5, margin: 'var(--space-4) 0 0' }}>
              위에는 {recommendation.limits.top_k}건만 보여집니다. 아래로 스크롤하면
              나머지 {recommendation.remaining_candidate_count}건까지 볼 수 있습니다.
            </p>
          ) : null}

          <div style={{ height: 'var(--space-8)' }} />

          <div className="row-baseline" style={{ marginBottom: 'var(--space-4)' }}>
            <h3 style={{ margin: 0 }}>전체 모집공고</h3>
            <span className="muted" style={{ fontSize: 13 }}>
              {shown.length}건 표시 · 매일 자동 수집
            </span>
          </div>

          <div style={{ marginBottom: 'var(--space-6)' }}>
            <div className="inline-wrap" style={{ alignItems: 'center' }}>
              <Chips options={visibleFields} selected={[filter]} onToggle={setFilter} />
              {fields.length > 6 ? (
                <button
                  type="button"
                  className="btn btn-ghost"
                  style={{ minHeight: 34, margin: 0, fontSize: 12.5 }}
                  onClick={() => setShowAllFields((current) => !current)}
                >
                  {showAllFields ? '접기' : `분야 더보기 +${fields.length - 6}`}
                </button>
              ) : null}
            </div>
          </div>

          <div className="stack-tight" style={{ gap: 'var(--space-1)' }}>
            {shown.map((trial) => {
              const verdict = verdictByTrial.get(trial.trial_id)
              return (
              <button
                key={trial.trial_id}
                type="button"
                className={
                  verdictOf(verdict) === '불가능'
                    ? 'list-row list-row-all row-dim'
                    : 'list-row list-row-all'
                }
                onClick={() => openListedTrial(trial)}
              >
                <span className="kicker" style={{ color: 'var(--color-accent-700)' }}>
                  {purposeOf(trial.trial_id)}
                </span>
                <div>
                  <div className="row-title-sm">{trial.trial_name}</div>
                  <div className="row-meta">
                    {TRIAL_META[trial.trial_id] ?? trial.trial_id}
                  </div>
                </div>
                <div />
                <div style={{ textAlign: 'right' }}>
                  {verdict ? (
                    <span className={verdictClass(verdictOf(verdict))}>
                      {verdictOf(verdict)}
                    </span>
                  ) : (
                    <span className="muted" style={{ fontSize: 12.5 }}>
                      판정 전
                    </span>
                  )}
                </div>
              </button>
              )
            })}
          </div>

          {!busy && !shown.length ? (
            <p className="muted">아직 판정된 공고가 없습니다.</p>
          ) : null}

          <p
            className="muted"
            style={{ fontSize: 12.5, margin: 'var(--space-6) 0 0', maxWidth: '64ch' }}
          >
            불가능은 충족하지 못한 기준이 확인된 경우입니다. 판정불가는 기록만으로
            확인되지 않은 기준이 남은 경우이며, 지원서에서 그 항목에 답하면 다시
            판정합니다.
          </p>
        </div>
      </div>
    </div>
  )
}
