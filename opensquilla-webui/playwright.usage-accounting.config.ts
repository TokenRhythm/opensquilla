import { defineConfig, devices } from '@playwright/test'
import { existsSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const baseURL = process.env.OPENSQUILLA_USAGE_TEST_URL || 'http://127.0.0.1:19086'
const backendURL = process.env.OPENSQUILLA_USAGE_TEST_BACKEND_URL || 'http://127.0.0.1:19087'
const windowsPython = fileURLToPath(new URL('../.venv/Scripts/python.exe', import.meta.url))
const python = process.env.OPENSQUILLA_TEST_PYTHON || (existsSync(windowsPython)
  ? windowsPython : fileURLToPath(new URL('../.venv/bin/python', import.meta.url)))
export default defineConfig({
  testDir: './e2e', testMatch: /usage-accounting\.spec\.ts/,
  fullyParallel: false, workers: 1, timeout: 30_000, reporter: 'list',
  outputDir: '../.tmp/usage-browser/results',
  use: { ...devices['Desktop Chrome'], baseURL, screenshot: 'only-on-failure', trace: 'retain-on-failure',
    ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH ? { launchOptions: { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH } } : {}),
  },
  webServer: [
    { command: `"${python}" e2e/fixtures/usage-gateway.py --port ${new URL(backendURL).port}`,
      url: `${backendURL}/health`, reuseExistingServer: false, timeout: 90_000,
      gracefulShutdown: { signal: 'SIGTERM', timeout: 5_000 } },
    { command: `node node_modules/vite/bin/vite.js --config vite.usage-accounting.config.mjs --configLoader native --host 127.0.0.1 --port ${new URL(baseURL).port} --strictPort`,
      url: `${baseURL}/e2e/fixtures/usage-accounting.html`, reuseExistingServer: false, timeout: 30_000 },
  ],
})
