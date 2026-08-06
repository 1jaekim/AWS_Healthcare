/**
 * 판정 결과 보관소.
 *
 * `POST /api/v1/recommendations/run` 한 번이 후보 공고 전체를 판정해서 돌려준다.
 * 공고마다 `screening/run` 을 돌릴 필요가 없다.
 *
 * 화면에 노출하는 결과는 **가능 · 불가능 · 판정불가 세 값뿐**이다. 백엔드가 함께
 * 주는 추천 분석값(`rank_score` 점수, `rank` 순위, `recommendation_decision`,
 * `selection_reason`)은 쓰지 않는다.
 *
 * 점수를 걷어낸 이유는 그것이 참여 가능성의 크기를 뜻하지 않기 때문이다.
 * `rank_score` 는 기준별 고정 상수(확정 1.0, 미해소 0.4, A2A 합의 0.8)의 평균이라
 * "적합도 73" 처럼 보여주면 없는 정밀도를 만들어낸다. 기준 하나가 미해소면 결론은
 * "아직 판정할 수 없음" 이고, 그건 73%가 아니다.
 *
 * `recommendation_decision` 을 쓰지 않는 이유도 같다. 그 값에는 A2A 교차 검토
 * 합의가 반영되는데, 규칙 계층이 확정한 판정은 `screening_decision` 이다. 사용자에게
 * 보여줄 것은 규칙이 확정한 쪽이다.
 *
 * 기준별 근거는 남긴다. "판정불가" 가 왜 판정불가인지 답할 수단이 없으면 세 값만
 * 보여주는 것이 오히려 불친절하다. 실행 상세(판단 검증 7항목)는 행을 펼칠 때만
 * `GET /screening/{run_id}` 로 따로 가져온다.
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
  Decision,
  DeliberationSummary,
  RecommendationCriterion,
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
  /**
   * trial_id → 지원서 제출로 직접 받은 판정.
   *
   * 결과 화면은 이 값이 있으면 추천을 실행하지 않는다. 방금 받은 판정을 버리고
   * 전체를 다시 돌리는 것이 대기시간의 주 원인이었다.
   */
  screened: Record<string, RecommendedTrial>
  busy: boolean
  error: unknown
  ranAt: Date | null
  loadTrials: () => Promise<TrialSummary[]>
  run: (personId: number | null, topK?: number, applicationId?: string) => Promise<RecommendationRun | null>
  loadRunDetail: (runId: string) => Promise<ScreeningRun | null>
  /** 지원서 제출로 받은 판정을 보관한다. 상세도 같이 채워 재조회를 없앤다. */
  recordScreening: (screening: ScreeningRun, title: string) => RecommendedTrial
  /** 공고 승인 등 후보 집합이 바뀌었을 때 이전 추천 스냅샷을 폐기한다. */
  invalidate: () => void
}

const MatchingContext = createContext<MatchingState | null>(null)

/** 추천·제외를 합친 전체 판정 목록. */
export function allTrials(recommendation: RecommendationRun | null): RecommendedTrial[] {
  if (!recommendation) return []
  return [...recommendation.recommended_trials, ...recommendation.excluded_trials]
}

/**
 * 시안의 "적합도" 자리. 백엔드 점수를 0~100 으로만 바꾼다.
 *
 * 결과 화면은 세 값(가능·불가능·판정불가)만 노출하므로 지금 이 함수를 부르는
 * 화면은 없다. 백엔드가 `rank_score` 로 추천을 정렬하는 동안은 계약이 살아 있어야
 * 하므로 남겨둔다.
 */
export function fitScore(trial: RecommendedTrial | undefined): string {
  if (!trial) return '—'
  if (trial.overall_status === 'EXCLUDED') return '제외'
  if (trial.criteria_total < MIN_SCORABLE_CRITERIA) return '자료 부족'
  return String(Math.round(trial.rank_score * 100))
}

/**
 * 적합도를 계산할 수 있는 최소 기준 수. 백엔드 `_MIN_SCORABLE_CRITERIA` 와 같다.
 *
 * 승인된 기준이 이보다 적으면 판정을 신뢰할 근거가 부족하다. 세 값 체계에서는
 * 판정불가의 한 종류로 다룬다.
 */
export const MIN_SCORABLE_CRITERIA = 3

/** 사용자에게 노출하는 판정. 이 셋 말고는 없다. */
export type Verdict = '가능' | '불가능' | '판정불가'

export const VERDICT_NOTE: Record<Verdict, string> = {
  가능: '기록으로 모든 기준이 확인되었습니다.',
  불가능: '충족하지 못한 기준이 있습니다.',
  판정불가: '기록만으로 확인되지 않은 기준이 있습니다.',
}

/**
 * 스크리닝 판정을 세 값으로 환원한다.
 *
 * 근거는 `screening_decision` 이다. 규칙 계층이 확정한 값이며 A2A 교차 검토가
 * 덮어쓰지 않는다. 추천 정렬용 `recommendation_decision` 은 쓰지 않는다.
 */
export function verdictOf(trial: RecommendedTrial | undefined): Verdict | null {
  if (!trial) return null
  if (trial.screening_decision === 'OK') return '가능'
  if (trial.screening_decision === 'NOT_OK') return '불가능'
  return '판정불가'
}

/** 판정별 표시 클래스. 색으로도 구분되지만 글자만으로 읽을 수 있어야 한다. */
export function verdictClass(verdict: Verdict | null): string {
  if (verdict === '가능') return 'tag tag-accent'
  if (verdict === '불가능') return 'tag'
  return 'tag'
}

/** 기준 상태 → 3값. 백엔드 `TrialRanker._public_status` 와 같은 규칙이다. */
function decisionOf(status: string): Decision {
  if (status === 'EVIDENCE_FOUND') return 'OK'
  if (status === 'CONTRADICTED') return 'NOT_OK'
  return 'UNKNOWN'
}

/** 판정불가의 종류. 사용자가 다음에 할 수 있는 일이 다르다. */
export type UnresolvedKind = 'ASKABLE' | 'DISPUTED' | 'UNGROUNDED' | 'A2A_RESOLVED'

export const UNRESOLVED_NOTE: Record<UnresolvedKind, string> = {
  ASKABLE: '기록에서 근거를 찾지 못했습니다. 지원서에서 답하면 확인됩니다.',
  DISPUTED: '두 검토자의 판단이 갈렸습니다. 연구간호사가 직접 확인합니다.',
  UNGROUNDED: '인용된 근거를 확인할 수 없었습니다. 연구간호사가 직접 확인합니다.',
  A2A_RESOLVED: '교차 검토에서는 합의되었으나 기록 확인이 필요합니다.',
}

/**
 * 공고 자체의 기준이 부족해 판정을 신뢰할 수 없는 경우.
 *
 * 기준별 문제가 아니라 공고 단위의 문제다. 크롤링·파싱에서 기준을 충분히 뽑지
 * 못했거나 승인된 기준이 적을 때 생긴다. 참여자가 답해서 풀 수 있는 것이 아니므로
 * 기준별 판정불가와 구분해 보여준다.
 */
export function insufficientCriteria(trial: RecommendedTrial | undefined): boolean {
  if (!trial) return false
  return trial.criteria_total < MIN_SCORABLE_CRITERIA
}

/**
 * 미해소 기준이 왜 미해소인지 구분한다.
 *
 * `판정불가` 만 보여주면 사용자가 할 수 있는 일이 없다. A2A 의 `agreement` 와
 * `grounded` 가 그 빈칸을 채운다 — 질문으로 풀리는 것과 사람이 봐야 하는 것은
 * 다른 상황이다. A2A 를 살려두는 이유가 이것이다.
 *
 * 교차 검토 대상이 아니었으면(A2A 가 안 돌았거나 상한에 걸렸으면) 근거 유무로만
 * 판단해 `ASKABLE` 로 둔다. 모르는 것을 사람 확인으로 부풀리지 않는다.
 */
export function unresolvedKindOf(
  criterion: RecommendationCriterion,
): UnresolvedKind | null {
  if (criterion.screening_status !== 'UNKNOWN') return null
  const a2a = criterion.a2a
  if (!a2a) return 'ASKABLE'
  if (!a2a.agreement) return 'DISPUTED'
  if (!a2a.grounded) return 'UNGROUNDED'
  if (a2a.recommendation === 'OK' || a2a.recommendation === 'NOT_OK') {
    return 'A2A_RESOLVED'
  }
  return 'ASKABLE'
}

/**
 * 스크리닝 실행 하나를 결과 화면이 쓰는 모양으로 바꾼다.
 *
 * 지원서를 낸 뒤에는 그 공고의 판정만 보면 된다. 예전에는 여기서
 * `recommendations/run` 을 다시 불러 승인 공고 전체를 재판정했다. 화면이 필요한
 * 것은 방금 받은 실행 하나인데 Bedrock 왕복이 공고 수만큼 더 늘어났다.
 *
 * 추천 분석값은 채우지 않는다. 화면이 쓰지 않으므로 없는 값을 만들 이유가 없다.
 * `rank`·`rank_score` 는 0/null 로 두고 `screening_decision` 만 진짜 값을 담는다.
 */
export function trialFromRun(run: ScreeningRun, title: string): RecommendedTrial {
  const questionOf = new Map(
    (run.requests ?? []).map((item) => [item.criterion_id, item.question]),
  )
  // A2A 결과를 기준별로 붙인다. 판정을 바꾸지는 않고 판정불가의 종류를 가른다.
  const deliberation = run.deliberation as DeliberationSummary | undefined
  const a2aOf = new Map(
    (deliberation?.items ?? []).map((item) => [item.criterion_id, item]),
  )

  const criteria: RecommendationCriterion[] = (run.packet?.items ?? []).map((item) => {
    const decision = decisionOf(String(item.status))
    const a2a = a2aOf.get(item.criterion_id) ?? null
    return {
      criterion_id: item.criterion_id,
      criterion_type: item.criterion_type,
      label: item.label,
      screening_status: decision,
      recommendation_status: decision,
      reason: item.explanation,
      evidence_ids: item.source_ids ?? [],
      a2a_applied: Boolean(a2a?.agreement && a2a?.grounded),
      next_question: questionOf.get(item.criterion_id) ?? null,
      a2a,
    }
  })

  return {
    rank: null,
    run_id: run.run_id,
    trial_id: run.trial_id,
    title,
    description: '',
    rank_score: 0,
    overall_status:
      run.screening_decision === 'OK'
        ? 'MATCHED'
        : run.screening_decision === 'NOT_OK'
          ? 'EXCLUDED'
          : 'NEEDS_MORE_INFO',
    screening_decision: run.screening_decision,
    recommendation_decision: run.screening_decision,
    criteria_met: run.criteria_met,
    criteria_total: run.criteria_total,
    unresolved_criteria: [...(run.open_criteria ?? []), ...(run.review_criteria ?? [])],
    human_review_required: (run.human_review_criteria ?? []).length > 0,
    human_review_criteria: run.human_review_criteria ?? [],
    selection_reason: '',
    a2a: {},
    criteria,
  }
}

export function MatchingProvider({ children }: { children: ReactNode }) {
  const [trials, setTrials] = useState<TrialSummary[]>([])
  const [recommendation, setRecommendation] = useState<RecommendationRun | null>(null)
  const [runs, setRuns] = useState<Record<string, ScreeningRun>>({})
  const [screened, setScreened] = useState<Record<string, RecommendedTrial>>({})
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

  const run = useCallback(async (personId: number | null, topK = 3, applicationId?: string) => {
    setBusy(true)
    setError(null)
    try {
      const result = await api.runRecommendations(personId, topK, undefined, applicationId)
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
        // 추천 응답 자체에 기준별 결과가 있다. Lambda 실행 환경이 바뀌어 선택적
        // 검증 상세가 사라져도 전체 결과 화면을 오류로 덮지 않는다.
        return null
      }
    },
    [runs],
  )

  const recordScreening = useCallback((screening: ScreeningRun, title: string) => {
    const entry = trialFromRun(screening, title)
    setScreened((prev) => ({ ...prev, [entry.trial_id]: entry }))
    // 실행 상세도 같이 채운다. 결과 화면이 근거를 펼칠 때 GET /screening/{id} 를
    // 다시 부르지 않아도 된다.
    setRuns((prev) => ({ ...prev, [screening.run_id]: screening }))
    return entry
  }, [])

  const invalidate = useCallback(() => {
    setRecommendation(null)
    setRuns({})
    setScreened({})
    setRanAt(null)
  }, [])

  const value = useMemo<MatchingState>(
    () => ({
      trials,
      recommendation,
      runs,
      screened,
      busy,
      error,
      ranAt,
      loadTrials,
      run,
      loadRunDetail,
      recordScreening,
      invalidate,
    }),
    [
      trials,
      recommendation,
      runs,
      screened,
      busy,
      error,
      ranAt,
      loadTrials,
      run,
      loadRunDetail,
      recordScreening,
      invalidate,
    ],
  )

  return <MatchingContext.Provider value={value}>{children}</MatchingContext.Provider>
}

export function useMatching(): MatchingState {
  const value = useContext(MatchingContext)
  if (!value) throw new Error('useMatching 은 MatchingProvider 안에서만 쓸 수 있습니다.')
  return value
}
