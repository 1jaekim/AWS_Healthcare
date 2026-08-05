/**
 * 시안 4c — 추천 결과 + 기준별 판정 상세.
 *
 * 행을 누르면 `recommendations/run` 이 준 기준별 판정을 펼친다. 그와 별개로
 * `GET /screening/{run_id}` 를 한 번 더 불러 기준별 판단 검증 7항목
 * (`judgment.items[].verification.checks`)을 같이 보여준다. 규칙 계층이 모델
 * 제안을 어떤 근거로 받아들이거나 물렸는지가 여기 남는다.
 */

import { useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'

import { TRIAL_META } from '../api/mock'
import type {
  JudgmentItem,
  RecommendationCriterion,
  RecommendedTrial,
  ScreeningRun,
} from '../api/types'
import AppHeader from '../components/AppHeader'
import { ErrorNote, Loading } from '../components/ui'
import { useAuth } from '../auth/AuthContext'
import {
  allTrials,
  fitScore,
  STATUS_LABEL,
  useMatching,
} from '../state/MatchingContext'

const DECISION_LABEL = {
  OK: '충족',
  NOT_OK: '미충족',
  UNKNOWN: '확인필요',
} as const

/** 검증 항목 ID → 사람이 읽는 이름. 백엔드 README 의 표와 같다. */
const CHECK_LABEL: Record<string, string> = {
  'V-EVIDENCE': '근거 존재',
  'V-WINDOW': '시간 범위',
  'V-UNIT': '단위',
  'V-OPERATOR': '연산자',
  'V-TYPE': '기준 유형',
  'V-PII': '개인정보',
  'V-CONFIDENCE': '신뢰도',
}

function CriterionRow({
  criterion,
  judgment,
}: {
  criterion: RecommendationCriterion
  judgment?: JudgmentItem
}) {
  const label = DECISION_LABEL[criterion.screening_status]
  const failed =
    judgment?.verification?.checks.filter((check) => !check.passed) ?? []
  const patientReported = criterion.evidence_ids.some((id) => id.startsWith('APP-'))

  return (
    <div className="verdict-row">
      <div
        style={{ fontSize: 14, fontWeight: 600 }}
        className={criterion.screening_status === 'UNKNOWN' ? 'dotted' : undefined}
      >
        {label}
      </div>
      <div>
        <div style={{ fontSize: 14.5 }}>{criterion.label}</div>
        {criterion.reason ? (
          <div
            style={{ fontSize: 13, color: 'var(--color-neutral-800)', marginTop: 3 }}
          >
            {criterion.reason}
          </div>
        ) : null}

        <div className="muted" style={{ fontSize: 12, marginTop: 5 }}>
          {patientReported
            ? '지원자 답변 근거 · 의료 기록 확인 필요'
            : criterion.evidence_ids.length
              ? '의료 기록 근거 확인됨'
              : '추가 확인 필요'}
          {criterion.a2a_applied ? ' · 교차 검토 반영' : ''}
        </div>

        {/* 모델 제안과 최종 상태가 갈린 경우만 드러낸다. 같으면 잡음이다. */}
        {judgment?.proposed && judgment.proposed !== criterion.screening_status ? (
          <div className="muted" style={{ fontSize: 12, marginTop: 3 }}>
            모델 제안 {judgment.proposed} → 규칙 확정 {criterion.screening_status}
          </div>
        ) : null}

        {failed.length ? (
          <div className="quote" style={{ marginTop: 6 }}>
            검증 실패 —{' '}
            {failed
              .map((check) => CHECK_LABEL[check.id] ?? check.id)
              .join(', ')}
          </div>
        ) : null}

        {criterion.next_question ? (
          <div style={{ fontSize: 13, marginTop: 6 }}>
            다음 질문 — {criterion.next_question}
          </div>
        ) : null}
      </div>
    </div>
  )
}

function ResultCard({
  trial,
  detail,
  open,
  onToggle,
}: {
  trial: RecommendedTrial
  detail: ScreeningRun | undefined
  open: boolean
  onToggle: () => void
}) {
  const navigate = useNavigate()
  const excluded = trial.overall_status === 'EXCLUDED'
  const unresolved = trial.unresolved_criteria.length
  const unmet = trial.criteria.filter((c) => c.screening_status === 'NOT_OK').length

  const judgmentItems: JudgmentItem[] =
    detail && 'items' in detail.judgment
      ? ((detail.judgment.items as JudgmentItem[] | undefined) ?? [])
      : []
  const judgmentOf = (criterionId: string) =>
    judgmentItems.find((item) => item.criterion_id === criterionId)

  return (
    <div>
      <button type="button" className="list-row list-row-result" onClick={onToggle}>
        <div>
          <div className="row-title" style={excluded ? { opacity: 0.6 } : undefined}>
            {trial.title}
          </div>
          <div className="row-meta">
            {TRIAL_META[trial.trial_id] ?? trial.trial_id} · {trial.selection_reason}
          </div>
        </div>
        <div style={{ fontSize: 13 }}>
          <span style={{ fontWeight: 600 }}>충족 {trial.criteria_met}</span>
          <span style={{ opacity: 0.55 }}> · </span>
          <span
            className={unresolved ? 'dotted' : undefined}
            style={{ fontWeight: unresolved ? 600 : 400, opacity: unresolved ? 1 : 0.55 }}
          >
            확인필요 {unresolved}
          </span>
          <span style={{ opacity: 0.55 }}> · </span>
          <span style={{ fontWeight: unmet ? 600 : 400, opacity: unmet ? 1 : 0.55 }}>
            미충족 {unmet}
          </span>
        </div>
        <div
          style={{
            textAlign: 'right',
            fontSize: 13,
            fontWeight: 600,
            opacity: excluded ? 0.6 : 1,
          }}
        >
          {excluded ? '제외' : `적합도 ${fitScore(trial)}`}
        </div>
      </button>

      {open ? (
        <div style={{ padding: 'var(--space-3) var(--space-2) var(--space-4)' }}>
          <div className="kicker" style={{ marginBottom: 'var(--space-3)' }}>
            기준별 판정 · 종합 {STATUS_LABEL[trial.overall_status]}
          </div>

          {trial.human_review_required ? (
            <div className="banner banner-warn" style={{ marginBottom: 'var(--space-4)' }}>
              확인되지 않은 기준이 있어 사람 검토가 필요합니다.
            </div>
          ) : null}

          <div className="stack-loose">
            {trial.criteria.map((criterion) => (
              <CriterionRow
                key={criterion.criterion_id}
                criterion={criterion}
                judgment={judgmentOf(criterion.criterion_id)}
              />
            ))}

            {trial.criteria_total > trial.criteria.length ? (
              <div className="verdict-row">
                <div style={{ fontSize: 14, opacity: 0.55 }}>
                  나머지 {trial.criteria_total - trial.criteria.length}건
                </div>
                <div className="muted" style={{ fontSize: 13 }}>
                  상세를 접어두었습니다.
                </div>
              </div>
            ) : null}
          </div>

          {/* 추천 판정이 원래 판정과 다르면 A2A 가 개입한 것이다. 구분해 보여준다. */}
          {trial.recommendation_decision !== trial.screening_decision ? (
            <p className="muted" style={{ fontSize: 12.5, margin: 'var(--space-4) 0 0' }}>
              스크리닝 판정 {trial.screening_decision} · 추천 판정{' '}
              {trial.recommendation_decision} — A2A 합의가 추천 정렬에만 반영되었고
              원래 판정은 보존되었습니다.
            </p>
          ) : null}

          {detail?.supplements?.source_application_id ? (
            <p className="muted" style={{ fontSize: 12.5, margin: 'var(--space-2) 0 0' }}>
              지원서 {detail.supplements.source_application_id} 의 값이 보충 관찰값으로
              반영되었습니다
              {detail.supplements.retrieval_mode
                ? ` · 검색 ${detail.supplements.retrieval_mode}`
                : ''}
              .
            </p>
          ) : null}

          <div style={{ display: 'flex', gap: 'var(--space-2)', marginTop: 'var(--space-6)' }}>
            <button
              className="btn btn-primary"
              type="button"
              style={{ minHeight: 42, marginTop: 0 }}
              disabled={excluded}
              onClick={() => navigate(`/intake/${encodeURIComponent(trial.trial_id)}`)}
            >
              {unresolved ? '지금 답변하기' : '참여 가능 확인'}
            </button>
            <button
              className="btn btn-secondary"
              type="button"
              style={{ minHeight: 42, marginTop: 0 }}
              onClick={() => navigate('/report')}
            >
              보고서로 저장
            </button>
          </div>
        </div>
      ) : null}
    </div>
  )
}

export default function Results() {
  const { trialId } = useParams<{ trialId?: string }>()
  const { account } = useAuth()
  const navigate = useNavigate()
  const { recommendation, runs, busy, error, run, loadRunDetail } = useMatching()
  const [openId, setOpenId] = useState<string | null>(trialId ?? null)
  const personId = account?.personId ?? 1

  useEffect(() => {
    if (recommendation) return
    // 전체 후보를 보려는 화면이므로 top_k 를 넉넉히 잡는다.
    void run(personId, 20)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [personId])

  const entries = allTrials(recommendation)

  // 펼친 행의 판단 검증 상세만 따로 가져온다.
  useEffect(() => {
    if (!openId) return
    const target = entries.find((trial) => trial.trial_id === openId)
    if (target) void loadRunDetail(target.run_id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openId, recommendation])

  const recommended = recommendation?.recommended_trials ?? []

  return (
    <div className="page-wide">
      <div className="sheet sheet-wide">
        <AppHeader />
        <div style={{ padding: 'var(--space-6) var(--space-8) var(--space-8)' }}>
          <div className="row-baseline" style={{ marginBottom: 'var(--space-2)' }}>
            <h2 style={{ margin: 0, fontSize: 30 }}>
              추천 임상시험 {recommended.length}건
            </h2>
          </div>

          <p
            style={{
              fontSize: 14,
              color: 'var(--color-neutral-800)',
              margin: '0 0 var(--space-6)',
              maxWidth: '60ch',
            }}
          >
            후보 {recommendation?.evaluated_trials ?? 0}건을 공고별 선정·제외 기준과
            대조한 결과입니다. 확인필요 항목이 남아 있으면 신청 전 연구간호사가 다시
            확인합니다.
          </p>

          {busy ? <Loading label="판정 중입니다" /> : null}
          <ErrorNote error={error} />

          <div className="stack-loose">
            {entries.map((trial) => (
              <ResultCard
                key={trial.trial_id}
                trial={trial}
                detail={runs[trial.run_id]}
                open={openId === trial.trial_id}
                onToggle={() =>
                  setOpenId((prev) => (prev === trial.trial_id ? null : trial.trial_id))
                }
              />
            ))}
          </div>

          {!busy && !entries.length ? (
            <p className="muted">아직 판정된 공고가 없습니다.</p>
          ) : null}

          <div style={{ display: 'flex', gap: 'var(--space-2)', marginTop: 'var(--space-8)' }}>
            <button
              className="btn btn-primary"
              type="button"
              style={{ minHeight: 42, marginTop: 0 }}
              onClick={() => navigate('/report')}
            >
              보고서 보기
            </button>
            <button
              className="btn btn-secondary"
              type="button"
              style={{ minHeight: 42, marginTop: 0 }}
              onClick={() => navigate('/home')}
            >
              홈으로
            </button>
          </div>

          <p
            className="muted"
            style={{ fontSize: 12.5, margin: 'var(--space-6) 0 0', maxWidth: '64ch' }}
          >
            모든 판정에는 근거 출처 ID가 함께 기록됩니다. 상태 확정은 규칙 계층이
            하며, 모델은 제안만 합니다. 같은 정보와 같은 기준 버전이면 결과는 동일하게
            재현됩니다.
          </p>
        </div>
      </div>
    </div>
  )
}
