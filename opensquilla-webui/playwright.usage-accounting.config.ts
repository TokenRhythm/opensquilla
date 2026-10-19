import { defineConfig, devices } from '@playwright/test'

const baseURL = process.env.OPENSQUILLA_USAGE_TEST_URL || 'http://127.0.0.1:19086'
export default defineConfig({
  testDir: './e2e', testMatch: /usage-accounting\.spec\.ts/,
  fullyParallel: false, workers: 1, timeout: 30_000, reporter: 'list',
  outputDir: '../.tmp/usage-browser/results',
  use: { ...devices['Desktop Chrome'], baseURL, screenshot: 'only-on-failure', trace: 'retain-on-failure',
    ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH ? { launchOptions: { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH } } : {}),
  },
  webServer: { command: `node node_modules/vite/bin/vite.js --config vite.usage-accounting.config.mjs --configLoader native --host 127.0.0.1 --port ${new URL(baseURL).port} --strictPort`,
    url: `${baseURL}/e2e/fixtures/usage-accounting.html`, reuseExistingServer: false, timeout: 30_000 },
})
