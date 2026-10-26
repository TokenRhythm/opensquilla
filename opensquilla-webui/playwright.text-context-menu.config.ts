import { defineConfig } from '@playwright/test'
import baseConfig from './playwright.config'

// A real Electron window renders the Vue app; do not launch a second browser
// or run concurrent tests against the shared OS clipboard.
export default defineConfig({
  ...baseConfig,
  testMatch: /text-context-menu\.desktop\.spec\.ts/,
  fullyParallel: false,
  workers: 1,
  retries: 0,
  projects: [{ name: 'electron', testIgnore: [] }],
})
