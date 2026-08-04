/** 시안 2d — 가입 완료 (Step 3/3). */

import { useNavigate } from 'react-router-dom'

import { useAuth } from '../auth/AuthContext'
import { AGREEMENT_DEFS, REQUIRED_AGREEMENTS } from '../auth/types'

function SummaryRow({ label, value }: { label: string; value: string }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 14 }}>
      <span className="muted">{label}</span>
      <span>{value}</span>
    </div>
  )
}

export default function SignupDone() {
  const { account } = useAuth()
  const navigate = useNavigate()

  const requiredCount = REQUIRED_AGREEMENTS.filter(
    (key) => account?.agreements[key],
  ).length
  const optionalCount = AGREEMENT_DEFS.filter(
    (def) => def.tag === '선택' && account?.agreements[def.key],
  ).length
  const answered = account?.survey
    ? [
        account.survey.purpose,
        account.survey.areas.length ? 'areas' : '',
        account.survey.medication,
        account.survey.allergy,
      ].filter(Boolean).length
    : 0

  return (
    <div className="page-center">
      <div className="sheet sheet-narrow" style={{ padding: 'var(--space-8)' }}>
        <div
          style={{
            width: 12,
            height: 12,
            background: 'var(--color-accent)',
            marginBottom: 'var(--space-6)',
          }}
        />
        <h3 style={{ margin: '0 0 var(--space-2)' }}>가입이 완료되었습니다</h3>
        <p
          style={{
            fontSize: 15,
            color: 'var(--color-neutral-800)',
            margin: '0 0 var(--space-8)',
            maxWidth: '32ch',
          }}
        >
          {account?.name ?? '회원'}님, 이제 참여 가능한 임상시험을 추천받을 수 있습니다.
        </p>

        <div className="stack" style={{ marginBottom: 'var(--space-8)' }}>
          <SummaryRow label="아이디" value={account?.email ?? '-'} />
          <SummaryRow label="설문" value={`${answered}문항 완료`} />
          <SummaryRow
            label="동의"
            value={`필수 ${requiredCount}건 · 선택 ${optionalCount}건`}
          />
        </div>

        <button
          className="btn btn-primary btn-block"
          type="button"
          onClick={() => navigate('/intake')}
          style={{ minHeight: 46, fontSize: 15, marginTop: 0 }}
        >
          임상시험 매칭 시작
        </button>
        <button
          className="btn btn-ghost btn-block"
          type="button"
          onClick={() => navigate('/home')}
          style={{ minHeight: 44 }}
        >
          홈으로
        </button>
      </div>
    </div>
  )
}
