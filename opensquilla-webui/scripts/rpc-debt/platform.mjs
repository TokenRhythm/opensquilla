export const lane = 'platform'

export const debt = {
  'src/components/settings/SettingsMemoryPanel.vue': {
    call: 9,
    markMethodUnavailable: 4,
    supportsMethod: 6,
    waitForConnection: 1,
  },
  'src/composables/settings/useSandboxSettings.ts': { call: 1, waitForConnection: 1 },
  'src/composables/setup/useMemoryLearningSettings.ts': { call: 1 },
  'src/stores/sandboxSetup.ts': { call: 2, waitForConnection: 1 },
}
