import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'

import App from './App'
import { setTokenSource } from './api/client'
import { authProvider } from './auth/provider'
import './styles/ds.css'
import './styles/app.css'

// API 클라이언트가 인증 계층을 직접 import 하면 순환이 생긴다. 여기서 이어준다.
setTokenSource(() => authProvider.idToken())

const container = document.getElementById('root')
if (!container) throw new Error('#root 를 찾지 못했습니다.')

createRoot(container).render(
  <StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </StrictMode>,
)
