import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { version } from './package.json'

// Frontend builds are fully decoupled from the backend: `vite build`
// outputs to frontend/dist, deployed independently (CDN / nginx). The
// _NoCacheStaticFiles pattern no longer applies — the backend API never
// serves the UI.

// 版本与构建信息（P6 §4.5）：编译期常量，运行时零请求。
//   __APP_VERSION__ — package.json 的 version
//   __BUILD_ID__    — 构建时间戳（ISO 8601，UTC）：这份产物是什么时候打的
// 两者都是展示信息（登录页版本行 / 顶栏），不参与任何业务判定。
const BUILD_ID = new Date().toISOString()

export default defineConfig({
  base: '/',
  plugins: [vue()],
  define: {
    __APP_VERSION__: JSON.stringify(version),
    __BUILD_ID__: JSON.stringify(BUILD_ID),
  },
  server: {
    // Dev-only: proxy API + SSE to the local backend so `npm run dev` works
    // without an API base — open http://localhost:5173/ (HMR on).
    proxy: {
      '/v1': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    rollupOptions: {
      output: {
        manualChunks: {
          'vendor-element-plus': ['element-plus'],
          'vendor-echarts': ['echarts'],
        },
      },
    },
  },
})
