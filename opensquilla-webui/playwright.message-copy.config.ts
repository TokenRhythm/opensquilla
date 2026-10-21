import { defineConfig } from '@playwright/test'
import baseConfig from './playwright.config'

// Native copy and paste share the OS clipboard across browser contexts.
export default defineConfig({
  ...baseConfig,
  testMatch: /user-message-copy\.spec\.ts/,
  fullyParallel: false,
  workers: 1,
  projects: [{
    ...baseConfig.projects![0],
    name: 'chromium',
    testIgnore: [],
    use: {
      ...baseConfig.projects![0].use,
      permissions: ['clipboard-read', 'clipboard-write'],
    },
  }],
})
