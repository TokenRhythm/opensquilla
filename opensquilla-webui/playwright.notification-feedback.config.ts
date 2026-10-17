import { defineConfig, devices } from '@playwright/test'

// Source-level browser acceptance using the real shipped components. All
// domain adapters are in-memory fixtures; no Gateway or real profile is used.
export default defineConfig({
  testDir: './e2e',
  testMatch: /notification-feedback\.spec\.ts/,
  fullyParallel: false,
  workers: 1,
  timeout: 30_000,
  reporter: 'list',
  outputDir: './test-results/notification-feedback',
  use: {
    ...devices['Desktop Chrome'],
    baseURL: process.env.OPENSQUILLA_NOTIFICATION_TEST_URL || 'http://127.0.0.1:19085',
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
    ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH ? { launchOptions: { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH } } : {}),
  },
})
