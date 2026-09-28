import { defineConfig, devices } from '@playwright/test'
import baseConfig from './playwright.config'

// The OS clipboard is shared across contexts: run these checks sequentially.
// Reuse the standard managed preview/dev server and its offline gateway routes.
export default defineConfig({
  ...baseConfig,
  testMatch: /image-copy\.spec\.ts/,
  fullyParallel: false,
  workers: 1,
  projects: [
    { ...baseConfig.projects![0], name: 'chromium', testIgnore: [],
      use: { ...baseConfig.projects![0].use, permissions: ['clipboard-read', 'clipboard-write'] } },
    { name: 'webkit', use: { ...devices['Desktop Safari'] } },
  ],
})
