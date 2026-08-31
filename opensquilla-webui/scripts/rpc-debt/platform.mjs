export const lane = 'platform'

export const debt = {
  'src/components/settings/SettingsMemoryPanel.vue': {
    call: 9,
    markMethodUnavailable: 4,
    supportsMethod: 6,
    waitForConnection: 1,
  },
  'src/composables/channels/useChannelEditor.ts': { call: 1, waitForConnection: 1 },
  'src/composables/channels/useChannelMembers.ts': { call: 5, waitForConnection: 1 },
  'src/composables/chat/useSandboxSetupRecovery.ts': {
    call: 1,
    callReference: 1,
    waitForConnectionReference: 1,
  },
  'src/composables/settings/useSandboxSettings.ts': { call: 10, waitForConnection: 6 },
  'src/composables/setup/channelRpc.ts': { call: 2 },
  'src/composables/setup/useMemoryLearningSettings.ts': { call: 1 },
  'src/stores/sandboxSetup.ts': { call: 2, waitForConnection: 1 },
  'src/views/ChannelsView.vue': { call: 7, on: 1, waitForConnection: 1 },
}
