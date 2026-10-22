// LOCAL QA ONLY — not deployed. Set D3M0N_PREVIEW_TARGET explicitly for a
// reviewed test backend; the safe default is the local authenticated server.
import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import tailwindcss from '@tailwindcss/vite'

const TARGET = process.env.D3M0N_PREVIEW_TARGET || 'http://127.0.0.1:5055'

export default defineConfig({
  plugins: [vue(), tailwindcss()],
  resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
  server: {
    port: 6190,
    strictPort: true,
    host: '127.0.0.1',
    proxy: {
      '/api': {
        target: TARGET,
        changeOrigin: true,
        secure: true,
        ws: true,
        configure: (proxy) => {
          // Present the same origin the production page uses.
          proxy.on('proxyReq', (req) => { if (req.getHeader('origin')) req.setHeader('origin', TARGET) })
          proxy.on('proxyReqWs', (req) => { req.setHeader('origin', TARGET) })
          proxy.on('proxyRes', (res) => {
            if (res.headers['content-type']?.includes('text/event-stream')) {
              res.headers['cache-control'] = 'no-cache'
              res.headers['x-accel-buffering'] = 'no'
            }
          })
        },
      },
      '/mascot': { target: TARGET, changeOrigin: true, secure: true },
    },
  },
})
