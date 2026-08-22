import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// Frontend builds are fully decoupled from the backend: `vite build`
// outputs to frontend/dist, deployed independently (CDN / nginx). The
// _NoCacheStaticFiles pattern no longer applies — the backend API never
// serves the UI.
export default defineConfig({
  base: '/',
  plugins: [vue()],
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
