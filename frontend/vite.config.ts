import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'node:path'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { '@': path.resolve(__dirname, './src') },
  },
  server: {
    port: 5173,
    proxy: {
      // 前端统一请求 /api/*，dev 阶段由 Vite 转发到本地 FastAPI
      '/api': {
        target: process.env.VITE_API_URL || 'http://localhost:8000',
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api/, ''),
      },
      // 侧栏「API 文档」指向 /api/docs。FastAPI 那张 Swagger 页里写死的是
      // 绝对路径 `url: '/openapi.json'`（不带 /api 前缀），落在前端源上会被
      // SPA 兜底成 index.html —— 200 + text/html，Swagger 拿到 HTML 当 JSON 解，
      // 报 "Failed to load API definition"。所以规格文件本身也要转发。
      // 生产侧的同一处修复在 docker/templates/default.conf.template。
      '/openapi.json': {
        target: process.env.VITE_API_URL || 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
  },
})
