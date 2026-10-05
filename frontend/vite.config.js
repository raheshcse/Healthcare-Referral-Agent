import process from 'node:process'

import react from '@vitejs/plugin-react'
import { defineConfig, loadEnv } from 'vite'

// The browser calls "/api/..." and Vite forwards it to FastAPI, so the
// frontend and API share one origin in development and preview.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const target = env.HEALTHCARE_REFERRAL_API_TARGET || 'http://127.0.0.1:8000'

  const proxy = {
    '/api': {
      target,
      changeOrigin: true,
      rewrite: (path) => path.replace(/^\/api/, ''),
    },
  }

  return {
    plugins: [react()],
    server: { proxy },
    preview: { proxy },
  }
})
