import { useCallback, useEffect, useState } from 'react'

import { api } from '../api/endpoints'
import type { TrialReviewItem, TrialReviewStatus } from '../api/types'
import AppHeader from '../components/AppHeader'
import { useMatching } from '../state/MatchingContext'

const LABEL: Record<TrialReviewStatus, string> = {
  pending_review: '승인 대기',
  approved: '승인됨',
  rejected: '반려됨',
  NEEDS_FIX: '품질 수정 필요',
}

function dateTime(epoch: number | null): string {
  return epoch ? new Date(epoch * 1000).toLocaleString('ko-KR') : '—'
}

export default function AdminTrials() {
  const { invalidate } = useMatching()
  const [status, setStatus] = useState<TrialReviewStatus>('pending_review')
  const [items, setItems] = useState<TrialReviewItem[]>([])
  const [notes, setNotes] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState('')

  const load = useCallback(async () => {
    setError('')
    try {
      setItems(await api.listTrialReviews(status))
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '공고 목록을 불러오지 못했습니다.')
    }
  }, [status])

  useEffect(() => {
    void load()
  }, [load])

  async function decide(item: TrialReviewItem, decision: 'approved' | 'rejected') {
    setBusy(item.source_key)
    setError('')
    try {
      await api.decideTrialReview(
        item.trial_id,
        item.source_key,
        decision,
        notes[item.source_key] ?? '',
      )
      // 홈에 남아 있는 승인 전 추천 결과를 폐기한다. 다음 홈 진입에서 새 후보
      // 전체를 다시 읽고 판정한다.
      invalidate()
      await load()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '처리에 실패했습니다.')
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="page-wide">
      <div className="sheet sheet-wide" style={{ marginBottom: 'var(--space-4)' }}>
        <AppHeader />
      </div>
      <main className="sheet sheet-wide" style={{ padding: 'var(--space-8)' }}>
        <div className="row-between" style={{ alignItems: 'flex-end', marginBottom: 24 }}>
          <div>
            <span className="kicker">ADMIN · NOTICE GOVERNANCE</span>
            <h2 style={{ margin: '8px 0 4px' }}>수집 공고 승인</h2>
            <p className="muted" style={{ margin: 0 }}>
              승인된 버전만 전체 공고와 후보 산정에 반영됩니다.
            </p>
          </div>
          <select value={status} onChange={(event) => setStatus(event.target.value as TrialReviewStatus)}>
            {Object.entries(LABEL).map(([value, label]) => (
              <option key={value} value={value}>{label}</option>
            ))}
          </select>
        </div>

        {error ? <div className="callout callout-danger" style={{ marginBottom: 16 }}>{error}</div> : null}
        <div className="stack-loose">
          {items.map((item) => {
            const invalid = item.criteria_count < 3 || !item.trial_title || item.trial_title.toUpperCase() === 'UNKNOWN'
            return (
              <article key={item.source_key} style={{ border: '1px solid var(--color-neutral-200)', borderRadius: 12, padding: 20 }}>
                <div className="row-between" style={{ alignItems: 'flex-start' }}>
                  <div>
                    <span className="kicker">{LABEL[item.status]} · {item.trial_id}</span>
                    <h3 style={{ margin: '6px 0' }}>{item.trial_title || '제목 없음'}</h3>
                    <div className="muted" style={{ fontSize: 13 }}>
                      {[item.condition, item.phase, item.intervention].filter(Boolean).join(' · ') || '시험 메타데이터 없음'}
                    </div>
                  </div>
                  <strong>{item.criteria_count}개 기준</strong>
                </div>
                <details style={{ marginTop: 16 }}>
                  <summary>구조화 기준 및 원본 키 확인</summary>
                  <div className="muted" style={{ margin: '10px 0', fontSize: 12, wordBreak: 'break-all' }}>
                    S3: {item.source_key} · 수집 {dateTime(item.created_at)}
                  </div>
                  <div style={{ overflowX: 'auto' }}>
                    <table className="table" style={{ width: '100%', fontSize: 12 }}>
                      <thead><tr><th>구분</th><th>필드</th><th>연산자</th><th>값</th></tr></thead>
                      <tbody>
                        {item.criteria.map((criterion) => (
                          <tr key={criterion.criterion_id}>
                            <td>{criterion.criterion_type}</td><td>{criterion.field}</td>
                            <td>{criterion.operator}</td>
                            <td>{[criterion.value_low, criterion.value_high, criterion.unit].filter(Boolean).join(' ')}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </details>
                {item.status === 'NEEDS_FIX' ? (
                  <div className="callout callout-danger" style={{ marginTop: 16 }}>
                    RAG에 반영되지 않았습니다. 원인: {item.failure_reason || '품질 검사 실패'}
                  </div>
                ) : null}
                {item.status === 'pending_review' ? (
                  <div style={{ marginTop: 16 }}>
                    {invalid ? <p style={{ color: 'var(--color-danger)', fontSize: 13 }}>제목이 없거나 기준이 3개 미만이라 승인할 수 없습니다. 상세 기준을 다시 수집하세요.</p> : null}
                    <textarea
                      aria-label={`${item.trial_id} 검토 메모`}
                      placeholder="승인·반려 사유 (선택)"
                      value={notes[item.source_key] ?? ''}
                      onChange={(event) => setNotes((current) => ({ ...current, [item.source_key]: event.target.value }))}
                      style={{ width: '100%', minHeight: 72 }}
                    />
                    <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
                      <button className="btn btn-secondary" disabled={busy === item.source_key} onClick={() => void decide(item, 'rejected')}>반려</button>
                      <button className="btn btn-primary" disabled={invalid || busy === item.source_key} onClick={() => void decide(item, 'approved')}>승인 및 활성화</button>
                    </div>
                  </div>
                ) : (
                  <p className="muted" style={{ fontSize: 12, marginBottom: 0 }}>
                    {dateTime(item.reviewed_at)} · {item.reviewed_by} · {item.review_note || '메모 없음'}
                  </p>
                )}
              </article>
            )
          })}
          {!items.length ? <p className="muted">해당 상태의 공고가 없습니다.</p> : null}
        </div>
      </main>
    </div>
  )
}
