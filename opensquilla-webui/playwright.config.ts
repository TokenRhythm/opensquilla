import { defineConfig, devices } from '@playwright/test'

const chromiumExecutablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH || undefined

export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: 'list',
  use: {
    baseURL: process.env.OPENSQUILLA_WEBUI_BASE_URL || 'http://127.0.0.1:18791',
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
  },
  projects: [
    {
      name: 'chromium',
      use: {
        ...devices['Desktop Chrome'],
        ...(chromiumExecutablePath
          ? { launchOptions: { executablePath: chromiumExecutablePath } }
          : {}),
      },
      // The fold-authoritative spec pins its flag explicitly and runs in the
      // dedicated project below. The ordinary project excludes that live-only
      // proof; production itself defaults to the fold unless explicitly set OFF.
      testIgnore: /fold-live-turn\.spec\.ts/,
    },
    {
      // Fold-authoritative proof: drive the live-stream paths with the fold authoritative
      // (opensquilla.chat.foldLiveTurn=1, set per-page in the spec). The spec
      // attaches the `[live-turn parity]` hard-fail, so this project is the
      // deterministic proof the ON path renders byte-faithfully to legacy.
      name: 'chromium-fold-on',
      use: {
        ...devices['Desktop Chrome'],
        ...(chromiumExecutablePath
          ? { launchOptions: { executablePath: chromiumExecutablePath } }
          : {}),
      },
      testMatch: /fold-live-turn\.spec\.ts/,
    },
  ],
})
