import { Link, useNavigate } from 'react-router-dom'

import { useAuth } from '../auth/AuthContext'

/** 로그인 후 화면 상단. 시안 5a 의 sticky 헤더. */
export default function AppHeader() {
  const { account, signOut } = useAuth()
  const navigate = useNavigate()

  return (
    <div
      className="row-between"
      style={{
        padding: 'var(--space-3) var(--space-8)',
        position: 'sticky',
        top: 0,
        background: 'var(--color-bg)',
        zIndex: 2,
      }}
    >
      <Link to="/home" className="brand" style={{ textDecoration: 'none', color: 'inherit' }}>
        <span style={{ fontSize: 17, fontWeight: 600 }}>임상시험 매칭</span>
        <span className="kicker">Trial Matching</span>
      </Link>
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 'var(--space-4)',
          fontSize: 13,
        }}
      >
        <Link to="/report" style={{ color: 'var(--color-accent-700)' }}>
          보고서
        </Link>
        <Link to="/intake" style={{ color: 'var(--color-accent-700)' }}>
          내 정보 보완
        </Link>
        {account?.isAdmin ? (
          <Link to="/admin/trials" style={{ color: 'var(--color-accent-700)' }}>
            공고 승인
          </Link>
        ) : null}
        <span className="muted">{account?.name ?? '회원'}님</span>
        <button
          type="button"
          className="btn btn-ghost"
          style={{ fontSize: 13 }}
          onClick={() => {
            void signOut().then(() => navigate('/login'))
          }}
        >
          로그아웃
        </button>
      </div>
    </div>
  )
}
