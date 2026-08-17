/// <reference types="vitest/config" />
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { resolve } from 'path'

// Where the dev server forwards backend traffic. The built bundle is served by
// the gateway itself (same origin), so this proxy only matters under `npm run
// dev`, where Vite serves the UI on its own port. Override the target to point
// at whichever local gateway you started.
const gatewayTarget = process.env.OPENSQUILLA_GATEWAY_URL || 'http://127.0.0.1:18790'

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [vue()],
  base: './',
  // vue-i18n build feature flags — silences the bundler warnings and drops the
  // legacy API + prod devtools from the bundle (Composition mode only).
  define: {
    __VUE_I18N_FULL_INSTALL__: 'true',
    __VUE_I18N_LEGACY_API__: 'false',
    __INTLIFY_PROD_DEVTOOLS__: 'false',
  },
  server: {
    proxy: {
      // REST endpoints (approvals, artifacts under /api/v1, file upload, audio, …).
      // Strip the browser's Origin header on the way through: the gateway's
      // same-origin guard on state-changing routes would otherwise reject
      // proxied POSTs (Origin says localhost:5173, Host says the gateway).
      // Requests through this proxy are developer-initiated, not drive-by.
      '/api': {
        target: gatewayTarget,
        changeOrigin: true,
        configure: (proxy) => {
          proxy.on('proxyReq', (proxyReq) => proxyReq.removeHeader('origin'))
        },
      },
      // RpcClient connects to ws://<host>/ws for the live chat/event stream.
      // Match the HTTP development proxy above: once Vite rewrites Host to the
      // gateway, forwarding the browser's Vite Origin would fail the gateway's
      // same-origin WebSocket guard. This proxy is only exposed by `npm run dev`.
      '/ws': {
        target: gatewayTarget,
        ws: true,
        changeOrigin: true,
        configure: (proxy) => {
          proxy.on('proxyReqWs', (proxyReq) => proxyReq.removeHeader('origin'))
        },
      },
      // Backend-owned static assets (brand mark, share-export images, …) that the
      // app loads from `${base}/static/*`. Scope to /control/static ONLY — the
      // bare /control prefix is the SPA router base and must stay with Vite, or
      // proxying it would forward the app's routes to the gateway and defeat HMR.
      '/control/static': { target: gatewayTarget, changeOrigin: true },
    },
  },
  build: {
    outDir: resolve(__dirname, '../src/opensquilla/gateway/static/dist'),
    emptyOutDir: true,
    sourcemap: true,
    rollupOptions: {
      output: {
        assetFileNames: (assetInfo) => {
          const info = assetInfo.name?.split('.') || []
          const ext = info[info.length - 1]
          if (/\.(png|jpe?g|gif|svg|webp|ico)$/i.test(assetInfo.name || '')) {
            return `assets/img/[name]-[hash][extname]`
          }
          if (/\.(woff2?|ttf|otf|eot)$/i.test(assetInfo.name || '')) {
            return `assets/fonts/[name]-[hash][extname]`
          }
          return `assets/[name]-[hash][extname]`
        },
        chunkFileNames: 'assets/[name]-[hash].js',
        entryFileNames: 'assets/[name]-[hash].js',
      },
    },
  },
  resolve: {
    alias: {
      '@': resolve(__dirname, 'src'),
    },
  },
  // Vitest: pure unit/property tests for the chat reducer and the merge/parity
  // helpers. Node environment (no DOM needed); the Playwright e2e tree under
  // e2e/ has its own runner and is excluded.
  test: {
    environment: 'node',
    include: ['src/**/*.{test,spec}.ts'],
    exclude: ['e2e/**', 'node_modules/**'],
  },
})
