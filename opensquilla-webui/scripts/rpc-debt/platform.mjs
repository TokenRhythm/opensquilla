export const lane = 'platform'

export const debt = {
  'src/components/ProjectWorkspacePickerDialog.vue': { call: 3 },
  'src/components/settings/DataMigrationPanel.vue': {
    call: 2,
    supportsMethod: 2,
    waitForConnection: 1,
  },
  'src/components/settings/SettingsMemoryPanel.vue': {
    call: 9,
    markMethodUnavailable: 4,
    supportsMethod: 6,
    waitForConnection: 1,
  },
  'src/composables/channels/useChannelEditor.ts': { call: 2, waitForConnection: 2 },
  'src/composables/channels/useChannelMembers.ts': { call: 6, waitForConnection: 1 },
  'src/composables/chat/useSandboxSetupRecovery.ts': {
    call: 1,
    callReference: 1,
    waitForConnectionReference: 1,
  },
  'src/composables/settings/useSandboxSettings.ts': { call: 10, waitForConnection: 6 },
  'src/composables/setup/channelRpc.ts': { call: 2 },
  'src/composables/setup/useMemoryLearningSettings.ts': { call: 4, waitForConnection: 1 },
  'src/composables/setup/useSetupCatalog.ts': {
    call: 30,
    supportsMethod: 3,
    waitForConnection: 2,
  },
  'src/composables/setup/useSetupProviderForm.ts': { call: 2 },
  'src/composables/useProjectWorkspaces.ts': { call: 6, supportsMethod: 1 },
  'src/stores/app.ts': { call: 1, supportsMethod: 1 },
  'src/stores/sandboxSetup.ts': { call: 2, waitForConnection: 1 },
  'src/views/ChannelsView.vue': { call: 8, on: 1, waitForConnection: 1 },
}
