import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  build: {
    // Outputs to frontend/dist — Dockerfile copies this to app/static
    outDir: 'dist',
    emptyOutDir: true,
  },
  server: {
    proxy: {
      '/auth':            'http://localhost:8000',
      '/api':             'http://localhost:8000',
      '/billing':         'http://localhost:8000',
      '/webhook':         'http://localhost:8000',
      '/admin':           'http://localhost:8000',
      '/api-keys':        'http://localhost:8000',
      '/broker-accounts': 'http://localhost:8000',
      '/health':          'http://localhost:8000',
    },
  },
})
