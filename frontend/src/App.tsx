import { Navigate, Route, Routes } from 'react-router-dom'

import { AuthProvider, RequireAuth, useAuth } from './auth/AuthContext'
import { MatchingProvider } from './state/MatchingContext'
import Home from './pages/Home'
import Intake from './pages/Intake'
import Login from './pages/Login'
import Matching from './pages/Matching'
import Report from './pages/Report'
import Results from './pages/Results'
import Signup from './pages/Signup'
import SignupDone from './pages/SignupDone'
import Survey from './pages/Survey'
import Verify from './pages/Verify'

/** 로그인했으면 홈, 아니면 로그인. 세션 복원이 끝날 때까지 기다린다. */
function Landing() {
  const { account, ready } = useAuth()
  if (!ready) return null
  return <Navigate to={account ? '/home' : '/login'} replace />
}

export default function App() {
  return (
    <AuthProvider>
      <MatchingProvider>
        <Routes>
          <Route path="/" element={<Landing />} />
          <Route path="/login" element={<Login />} />
          <Route path="/signup" element={<Signup />} />
          {/* 인증 코드 확인. 아직 로그인 전이므로 RequireAuth 를 걸지 않는다. */}
          <Route path="/signup/verify" element={<Verify />} />

          <Route
            path="/signup/survey"
            element={
              <RequireAuth>
                <Survey />
              </RequireAuth>
            }
          />
          <Route
            path="/signup/done"
            element={
              <RequireAuth>
                <SignupDone />
              </RequireAuth>
            }
          />
          <Route
            path="/home"
            element={
              <RequireAuth>
                <Home />
              </RequireAuth>
            }
          />
          {/* 공고를 클릭해 들어오는 지원서 챗. 공고 없이 들어오면 추천 1순위를 쓴다. */}
          <Route
            path="/intake"
            element={
              <RequireAuth>
                <Intake />
              </RequireAuth>
            }
          />
          <Route
            path="/intake/:trialId"
            element={
              <RequireAuth>
                <Intake />
              </RequireAuth>
            }
          />
          <Route
            path="/matching"
            element={
              <RequireAuth>
                <Matching />
              </RequireAuth>
            }
          />
          <Route
            path="/results"
            element={
              <RequireAuth>
                <Results />
              </RequireAuth>
            }
          />
          <Route
            path="/results/:trialId"
            element={
              <RequireAuth>
                <Results />
              </RequireAuth>
            }
          />
          <Route
            path="/report"
            element={
              <RequireAuth>
                <Report />
              </RequireAuth>
            }
          />

          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </MatchingProvider>
    </AuthProvider>
  )
}
