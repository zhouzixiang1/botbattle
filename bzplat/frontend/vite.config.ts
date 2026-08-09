import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'node:path'

// 后端 API/avatars 目标地址：安全默认指向 worktree 端口 50381；也可显式设置
//   BZ_API_TARGET=http://127.0.0.1:50381 npm run dev
// 把前端请求代理到 worktree 独立后端（严禁 proxy 到 50380 线上服务，会把测试写进线上 db）。
const apiTarget = process.env.BZ_API_TARGET || 'http://127.0.0.1:50381'
if (new URL(apiTarget).port === '50380') {
  throw new Error('Vite dev proxy must not target the 50380 main service; use an isolated worktree backend')
}

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(import.meta.dirname, './src'),
    },
  },
  server: {
    proxy: {
      // /api 同时承载 REST/SSE 与人类对战 WebSocket；显式开启 ws，
      // 否则开发服务器只代理 HTTP，请求 /play 时会在 Vite 端反复断线。
      '/api': { target: apiTarget, ws: true },
      '/avatars': apiTarget,
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
})
