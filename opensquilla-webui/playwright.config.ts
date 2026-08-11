import { defineConfig, devices } from '@playwright/test'
import { fileURLToPath } from 'node:url'

const chromiumExecutablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH || undefined
const baseURL = process.env.OPENSQUILLA_WEBUI_BASE_URL || 'http://127.0.0.1:18791'
const managedServer = process.env.OPENSQUILLA_PLAYWRIGHT_MANAGE_WEBUI || ''
const repoRoot = fileURLToPath(new URL('..', import.meta.url))
const parsedBaseURL = new URL(baseURL)
const gatewayPort = Number(
  parsedBaseURL.port || (parsedBaseURL.protocol === 'https:' ? 443 : 80),
)
if (!Number.isInteger(gatewayPort) || gatewayPort < 1 || gatewayPort > 65_535) {
  throw new Error(`Invalid OPENSQUILLA_WEBUI_BASE_URL port: ${baseURL}`)
}
const webServer = managedServer === 'gateway'
  ? {
      command: `uv run opensquilla gateway run --bind 127.0.0.1 --port ${gatewayPort}`,
      cwd: repoRoot,
      url: `${baseURL.replace(/\/$/, '')}/control/`,
      reuseExistingServer: false,
      timeout: 120_000,
    }
  : managedServer === '1'
    ? {
        command: `npm run dev -- --host 127.0.0.1 --port ${gatewayPort} --strictPort`,
        url: `${baseURL.replace(/\/$/, '')}/control/`,
        reuseExistingServer: false,
        timeout: 120_000,
      }
    : undefined

export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: 'list',
  use: {
    baseURL,
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
  },
  webServer,
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
