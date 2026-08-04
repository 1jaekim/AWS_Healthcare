/**
 * 추천 결과 보관소.
 *
 * `POST /api/v1/recommendations/run` 한 번이 후보 공고 전체를 판정하고, A2A 교차
 * 검토를 반영해 정렬까지 해서 돌려준다. 예전처럼 공고마다 `screening/run` 을
 * 돌릴 필요가 없다.
 *
 * 점수(`rank_score`)도 백엔드가 준다. 확정 `OK=1.0`, 미해소 `UNKNOWN=0.4`,
 * 그라운딩된 A2A 합의 `OK=0.8` 처럼 코드에 고정된 보수적 값이며 모델 생성값이
 * 아니다. 화면은 이 값을 그대로 표시하고 직접 계산하지 않는다.
 *
 * 실행 상세(기준별 판단 검증 7항목)는 행을 펼칠 때만 `GET /screening/{run_id}`
 * 로 따로 가져온다. 목록을 그리는 데는 필요 없기 때문이다.
 */

import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from 'react'

import { api } from '../api/endpoints'
import type {
  RecommendationRun,
  RecommendedTrial,
  ScreeningRun,
  TrialSummary,
} from '../api/types'

export interface MatchingState {
  trials: TrialSummary[]
  recommendation: RecommendationRun | null
  /** run_id → 실행 상세. 펼친 행만 채워진다. */
  runs: Record<string, ScreeningRun>
  busy: boolean
  error: unknown
  ranAt: Date | null
  loadTrials: () => Promise<TrialSummary[]>
  run: (personId: number, topK?: number) => Promise<RecommendationRun | null>
  loadRunDetail: (runId: string) => Promise<ScreeningRun | null>
}

const MatchingContext = createContext<MatchingState | null>(null)

/** 추천·제외를 합친 전체 판정 목록. */
export function allTrials(recommendation: RecommendationRun | null): RecommendedTrial[] {
  if (!recommendation) return []
  return [...recommendation.recommended_trials, ...recommendation.excluded_trials]
}

/** 시안의 "적합도" 자리. 백엔드 점수를 0~100 으로만 바꾼다. */
export function fitScore(trial: RecommendedTrial | undefined): string {
  if (!trial) return '—'
  if (trial.overall_status === 'EXCLUDED') return '제외'
  return String(Math.round(trial.rank_score * 100))
}

export function verdictSummary(trial: RecommendedTrial | undefined): string {
  if (!trial) return '판정 전'
  const unresolved = trial.unresolved_criteria.length
  const unmet = trial.criteria.filter(
    (criterion) => criterion.screening_status === 'NOT_OK',
  ).length
  return `충족 ${trial.criteria_met} · 확인필요 ${unresolved} · 미충족 ${unmet}`
}

export const STATUS_LABEL: Record<RecommendedTrial['overall_status'], string> = {
  MATCHED: '추천',
  NEEDS_MORE_INFO: '정보부족',
  EXCLUDED: '제외',
}

export function MatchingProvider({ children }: { children: ReactNode }) {
  const [trials, setTrials] = useState<TrialSummary[]>([])
  const [recommendation, setRecommendation] = useState<RecommendationRun | null>(null)
  const [runs, setRuns] = useState<Record<string, ScreeningRun>>({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const [ranAt, setRanAt] = useState<Date | null>(null)

  const loadTrials = useCallback(async () => {
    setError(null)
    try {
      const list = await api.listTrials()
      setTrials(list)
      return list
    } catch (cause) {
      setError(cause)
      return []
    }
  }, [])

  const run = useCallback(async (personId: number, topK = 3) => {
    setBusy(true)
    setError(null)
    try {
      const result = await api.runRecommendations(personId, topK)
      setRecommendation(result)
      setRanAt(new Date())
      return result
    } catch (cause) {
      setError(cause)
      return null
    } finally {
      setBusy(false)
    }
  }, [])

  const loadRunDetail = useCallback(
    async (runId: string) => {
      if (runs[runId]) return runs[runId]
      try {
        const detail = await api.getScreening(runId)
        setRuns((prev) => ({ ...prev, [runId]: detail }))
        return detail
      } catch (cause) {
        // 상세를 못 가져와도 목록은 그대로 보여준다. 치명적이지 않다.
        setError(cause)
        return null
      }
    },
    [runs],
  )

  const value = useMemo<MatchingState>(
    () => ({
      trials,
      recommendation,
      runs,
      busy,
      error,
      ranAt,
      loadTrials,
      run,
      loadRunDetail,
    }),
    [trials, recommendation, runs, busy, error, ranAt, loadTrials, run, loadRunDetail],
  )

  return <MatchingContext.Provider value={value}>{children}</MatchingContext.Provider>
}

export function useMatching(): MatchingState {
  const value = useContext(MatchingContext)
  if (!value) throw new Error('useMatching 은 MatchingProvider 안에서만 쓸 수 있습니다.')
  return value
}
