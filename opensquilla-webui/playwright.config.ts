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
  : managedServer === 'preview'
    ? {
        // Release-gate path: serve the already-built artifact rather than
        // compiling modules on demand through the Vite development server.
        command: `npm run preview -- --host 127.0.0.1 --port ${gatewayPort} --strictPort --base /control/`,
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
      // The append-only live-turn proof runs in the dedicated project below.
      testIgnore: /fold-live-turn\.spec\.ts/,
    },
    {
      // Drive the real live-stream path through the sole turn projection.
      name: 'chromium-live-turn',
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
