/** 시안 4d — 최종 보고서. */

import { useMemo } from 'react'
import { useNavigate } from 'react-router-dom'

import type { RecommendationCriterion } from '../api/types'
import AppHeader from '../components/AppHeader'
import { useAuth } from '../auth/AuthContext'
import { allTrials, useMatching, verdictOf } from '../state/MatchingContext'

export default function Report() {
  const { account } = useAuth()
  const navigate = useNavigate()
  const { recommendation } = useMatching()

  const rows = allTrials(recommendation)

  // 집계도 화면 배지와 같은 기준(verdictOf → screening_decision)을 쓴다.
  // overall_status 는 A2A 합의가 반영되어 배지와 갈릴 수 있다.
  const matched = rows.filter((row) => verdictOf(row) === '가능').length
  const needsInfo = rows.filter((row) => verdictOf(row) === '판정불가').length
  const review = rows.filter((row) => row.human_review_required).length

  /**
   * 확인필요 항목은 기준별로 한 번씩만 보여준다. 여러 공고가 같은 기준을 물을 수
   * 있고, 참여자 입장에서는 같은 질문이다.
   */
  const openCriteria = useMemo(() => {
    const seen = new Map<string, RecommendationCriterion>()
    for (const row of rows) {
      for (const criterion of row.criteria) {
        if (criterion.screening_status !== 'UNKNOWN') continue
        if (!seen.has(criterion.criterion_id)) seen.set(criterion.criterion_id, criterion)
      }
    }
    return [...seen.values()]
  }, [rows])

  return (
    <div className="page-wide">
      <div className="sheet sheet-wide" style={{ marginBottom: 'var(--space-4)' }}>
        <AppHeader />
      </div>

      <div className="sheet sheet-report">
        <div className="row-baseline" style={{ marginBottom: 'var(--space-6)' }}>
          <span className="kicker">매칭 보고서</span>
          <span className="muted" style={{ fontSize: 12 }}>
            {recommendation?.recommendation_id ?? '—'} · person{' '}
            {account?.personId ?? '—'}
          </span>
        </div>

        <h2 style={{ margin: '0 0 var(--space-2)', fontSize: 30, maxWidth: '26ch' }}>
          임상시험 매칭 결과
        </h2>
        <p
          style={{
            fontSize: 14.5,
            color: 'var(--color-neutral-800)',
            margin: '0 0 var(--space-8)',
            maxWidth: '56ch',
          }}
        >
          후보 {recommendation?.evaluated_trials ?? 0}건을 검토해{' '}
          {recommendation?.recommended_trials.length ?? 0}건을 추천했습니다.
          {recommendation
            ? ` 교차 검토는 공고당 최대 ${recommendation.limits.a2a_max_criteria_per_trial}개 기준, ${recommendation.limits.a2a_max_rounds}라운드로 제한됩니다.`
            : ''}
        </p>

        <h4 style={{ margin: '0 0 var(--space-3)' }}>요약</h4>
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(3, 1fr)',
            gap: 'var(--space-4)',
            marginBottom: 'var(--space-8)',
          }}
        >
          <div>
            <div style={{ fontSize: 28, fontWeight: 600 }}>{matched}</div>
            <div className="muted" style={{ fontSize: 13 }}>
              바로 참여 가능
            </div>
          </div>
          <div>
            <div style={{ fontSize: 28, fontWeight: 600 }}>{needsInfo}</div>
            <div className="muted" style={{ fontSize: 13 }}>
              정보 확인 후 판단
            </div>
          </div>
          <div>
            <div style={{ fontSize: 28, fontWeight: 600 }}>{review}</div>
            <div className="muted" style={{ fontSize: 13 }}>
              사람 검토 요청
            </div>
          </div>
        </div>

        <h4 style={{ margin: '0 0 var(--space-3)' }}>임상시험별 판정</h4>
        <div style={{ overflowX: 'auto', marginBottom: 'var(--space-8)' }}>
          <table className="table" style={{ width: '100%', fontSize: 13.5 }}>
            <thead>
              <tr>
                <th>임상시험</th>
                <th>판정</th>
                <th>확인된 기준</th>
                <th>확인되지 않은 기준</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const verdict = verdictOf(row)
                return (
                  <tr
                    key={row.trial_id}
                    style={verdict === '불가능' ? { opacity: 0.6 } : undefined}
                  >
                    <td>{row.title}</td>
                    <td style={{ fontWeight: verdict === '불가능' ? 400 : 600 }}>
                      {verdict}
                    </td>
                    <td>
                      {row.criteria_met} / {row.criteria_total}
                    </td>
                    <td>{row.unresolved_criteria.length}</td>
                  </tr>
                )
              })}
              {!rows.length ? (
                <tr>
                  <td colSpan={4} className="muted">
                    아직 실행된 판정이 없습니다.
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </div>

        <h4 style={{ margin: '0 0 var(--space-3)' }}>남은 확인필요 항목</h4>
        <div className="stack-loose" style={{ marginBottom: 'var(--space-8)' }}>
          {openCriteria.length ? (
            openCriteria.map((criterion) => (
              <div key={criterion.criterion_id}>
                <div style={{ fontSize: 14.5, fontWeight: 600 }}>
                  {criterion.label} ({criterion.criterion_id})
                </div>
                {criterion.next_question ? (
                  <div
                    style={{
                      fontSize: 13,
                      color: 'var(--color-neutral-800)',
                      marginTop: 3,
                    }}
                  >
                    다음 질문 — {criterion.next_question}
                  </div>
                ) : null}
                <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
                  {criterion.reason}
                </div>
              </div>
            ))
          ) : (
            <p className="muted" style={{ margin: 0 }}>
              남은 확인 항목이 없습니다.
            </p>
          )}
        </div>

        <p
          className="muted"
          style={{
            fontSize: 12.5,
            lineHeight: 1.8,
            margin: '0 0 var(--space-6)',
            maxWidth: '60ch',
          }}
        >
          판정은 가능 · 불가능 · 판정불가 세 가지이며, 규칙 계층이 확정합니다. 모델은
          제안만 하고 상태를 올리지 못합니다. 이 보고서는 참여 가능성을 사전 검토한
          자료이며 최종 선정은 실시기관의 사전 문진과 검사로 결정됩니다. 보고서에는
          이름·연락처가 포함되지 않습니다.
        </p>

        <div style={{ display: 'flex', gap: 'var(--space-2)' }}>
          <button
            className="btn btn-primary"
            type="button"
            style={{ minHeight: 42, marginTop: 0 }}
            onClick={() => window.print()}
          >
            PDF로 저장
          </button>
          <button
            className="btn btn-secondary"
            type="button"
            style={{ minHeight: 42, marginTop: 0 }}
            disabled={!openCriteria.length}
            onClick={() => navigate('/intake')}
          >
            남은 질문 답하기
          </button>
        </div>
      </div>
    </div>
  )
}
