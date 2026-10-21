import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath } from 'node:url'

// Isolated source fixture: no application bootstrap, shared dependency patch,
// installed Gateway or user profile. Only the isolated test RPC wrapper is proxied.
export default defineConfig({
  plugins: [vue()],
  cacheDir: fileURLToPath(new URL('../.tmp/usage-vite-cache', import.meta.url)),
  define: { __VUE_I18N_FULL_INSTALL__: 'true', __VUE_I18N_LEGACY_API__: 'false', __INTLIFY_PROD_DEVTOOLS__: 'false' },
  resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
  optimizeDeps: { noDiscovery: true, include: ['vue', 'vue-router', 'vue-i18n'] },
  server: { proxy: { '/__usage_gateway': {
    target: process.env.OPENSQUILLA_USAGE_TEST_BACKEND_URL || 'http://127.0.0.1:19087',
    rewrite: path => path.replace(/^\/__usage_gateway/, ''),
  } } },
})
