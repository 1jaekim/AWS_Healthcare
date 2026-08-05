/**
 * 시안 4b — 매칭 실행 중 · 에이전트 단계.
 *
 * `POST /api/v1/recommendations/run` 은 후보 공고 전체를 판정하고 A2A 교차 검토까지
 * 마친 뒤 한 번에 응답한다. 진행 상황을 스트리밍하지 않으므로, 여기 단계 표시는
 * 백엔드가 실제로 어디까지 갔는지가 아니라 "무슨 일이 일어나는지" 를 알리는
 * 설명이다. 진짜 단계를 보여주려면 SSE 나 상태 조회 엔드포인트가 필요하다.
 */

import { useEffect, useRef, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'

import { ErrorNote } from '../components/ui'
import { useAuth } from '../auth/AuthContext'
import { useMatching } from '../state/MatchingContext'

const STAGES = [
  { label: '최신 모집공고 확인', detail: '수집된 공고 목록을 불러옵니다' },
  { label: '선정·제외 기준 불러오기', detail: '공고별 기준을 읽어옵니다' },
  { label: '근거 검색', detail: 'GraphRAG 로 소견서·검사 기록을 연결합니다' },
  { label: '기준별 판단과 검증', detail: '모델 제안을 7개 항목으로 검증합니다' },
  { label: '교차 검토', detail: '보류된 기준을 최대 2라운드로 다시 봅니다' },
  { label: '추천 정렬', detail: '확정 제외를 빼고 남은 후보를 정렬합니다' },
]

/** 실제 진행률을 알 수 없으므로 예상 소요시간에 맞춰 단계를 넘긴다. */
const STAGE_INTERVAL_MS = 1400

export default function Matching() {
  const { account } = useAuth()
  const navigate = useNavigate()
  const { run, busy, error } = useMatching()
  const [searchParams] = useSearchParams()
  const started = useRef(false)
  const [stage, setStage] = useState(0)
  const personId = account?.personId ?? null

  // 지원서를 낸 공고가 있으면 그 공고 결과로 바로 보낸다. 없으면 전체 목록.
  const focusTrialId = searchParams.get('trial')
  const applicationId = searchParams.get('application') ?? undefined

  useEffect(() => {
    if (started.current) return
    started.current = true
    if (!applicationId && personId === null) return
    void run(personId, 3, applicationId).then((result) => {
      if (!result) return
      navigate(
        focusTrialId
          ? `/results/${encodeURIComponent(focusTrialId)}`
          : '/results',
        { replace: true },
      )
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [personId])

  useEffect(() => {
    if (!busy) return
    // 마지막 단계에서 멈춰 기다린다. 끝난 척하지 않는다.
    const timer = setInterval(
      () => setStage((prev) => Math.min(prev + 1, STAGES.length - 1)),
      STAGE_INTERVAL_MS,
    )
    return () => clearInterval(timer)
  }, [busy])

  return (
    <div className="page-center">
      <div className="sheet" style={{ width: 520, maxWidth: '100%', padding: 'var(--space-8)' }}>
        <div className="kicker" style={{ marginBottom: 'var(--space-2)' }}>
          {applicationId ? '지원서 프로필' : `person ${personId ?? '미연결'}`}
        </div>
        <h3 style={{ margin: '0 0 var(--space-2)' }}>적합한 임상시험을 찾고 있습니다</h3>
        <p
          style={{
            fontSize: 14,
            color: 'var(--color-neutral-800)',
            margin: '0 0 var(--space-6)',
          }}
        >
          후보 공고를 기준과 대조합니다. 창을 닫아도 결과는 홈에서 다시 실행할 수
          있습니다.
        </p>

        <div className="stack-loose">
          {STAGES.map((item, index) => {
            const state =
              index < stage ? '완료' : index === stage ? '진행 중' : '대기'
            return (
              <div
                key={item.label}
                style={{
                  display: 'grid',
                  gridTemplateColumns: '1fr auto',
                  gap: 'var(--space-2)',
                  alignItems: 'baseline',
                }}
              >
                <div>
                  <div
                    style={{
                      fontSize: 14.5,
                      fontWeight: 600,
                      opacity: state === '대기' ? 0.5 : 1,
                    }}
                  >
                    {item.label}
                  </div>
                  <div className="muted" style={{ fontSize: 12.5 }}>
                    {item.detail}
                  </div>
                </div>
                <span
                  style={{
                    fontSize: 12.5,
                    opacity: state === '진행 중' ? 1 : 0.55,
                    fontWeight: state === '진행 중' ? 600 : 400,
                  }}
                >
                  {state}
                </span>
              </div>
            )
          })}
        </div>

        <ErrorNote error={error} />

        <button
          className="btn btn-secondary btn-block"
          type="button"
          onClick={() => navigate('/home')}
          style={{ minHeight: 42, marginTop: 'var(--space-8)' }}
        >
          {busy ? '중단하고 나중에 받기' : '홈으로'}
        </button>
      </div>
    </div>
  )
}
