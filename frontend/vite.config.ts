import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 개발 서버 포트를 3000 으로 고정한다. 백엔드(`backend/api/app/main.py`)의
// CORS 기본 허용 목록이 localhost:3000 이라, 여기를 바꾸면 CORS_ALLOW_ORIGINS
// 도 같이 바꿔야 한다.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    strictPort: true,
  },
  build: {
    // Amplify / S3 정적 호스팅 산출물. 해시 파일명이라 장기 캐시가 안전하다.
    outDir: 'dist',
    sourcemap: false,
  },
})
