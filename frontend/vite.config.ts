import react from '@vitejs/plugin-react'
import { defineConfig, loadEnv } from 'vite'

/**
 * In development the portal is served from the same origin as the API, because Vite proxies
 * /api and /health to the backend. That is not a convenience: it means the browser never makes
 * a cross-origin request, so no CORS policy has to exist for the demo to work, and nobody has
 * to widen one under time pressure in front of a judge.
 *
 * A deployed build is a different question. Either serve these static files from the same
 * origin as the API -- still no CORS -- or set VITE_API_BASE_URL and add an explicit, narrow
 * CORS allowlist on the backend. `*` would be the wrong answer: /api is the authenticated
 * human's surface and carries the only endpoint that can write a price cap.
 */
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const target = env.VITE_API_PROXY_TARGET ?? 'http://127.0.0.1:8000'

  return {
    plugins: [react()],
    server: {
      proxy: {
        '/api': { target, changeOrigin: true },
        '/health': { target, changeOrigin: true },
      },
    },
  }
})
