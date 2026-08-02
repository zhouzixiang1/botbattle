import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'node:path'

// 后端 API/avatars 目标地址：默认主服务 50380；worktree 隔离开发时用
//   BZ_API_TARGET=http://127.0.0.1:50381 npm run dev
// 把前端请求代理到 worktree 独立后端（严禁 proxy 到 50380 线上服务，会把测试写进线上 db）。
const apiTarget = process.env.BZ_API_TARGET || 'http://127.0.0.1:50380'

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
      '/api': apiTarget,
      '/avatars': apiTarget,
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
})
