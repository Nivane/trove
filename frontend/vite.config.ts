import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// Build output lands in trove/api/static (the FastAPI static mount).
// The committed build keeps pip-install parity; _NoCacheStaticFiles on the
// server side already handles browser-side staleness during dev.
export default defineConfig({
  base: '/ui/',
  plugins: [vue()],
  server: {
    // Dev-only: proxy API + SSE to the local backend so `npm run dev` works
    // without an API base — open http://localhost:5173/ui/ (HMR on).
    proxy: {
      '/v1': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: '../trove/api/static',
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
