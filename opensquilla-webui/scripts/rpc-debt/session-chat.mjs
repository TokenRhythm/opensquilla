export const lane = 'session-chat'

export const debt = {
  'src/components/chat/PromptCacheKeepaliveDialog.vue': { call: 2 },
  'src/composables/chat/sessionBootstrapAdmission.ts': { waitForConnection: 2 },
  'src/composables/chat/useChatApprovals.ts': {
    call: 1,
    on: 1,
  },
  'src/composables/chat/useChatAttachments.ts': {
    httpRequest: 1,
    httpApiEndpoint: 1,
    httpAuthToken: 1,
    httpAuthorizationHeader: 1,
  },
  'src/composables/chat/useArtifactPreview.ts': {
    httpRequest: 1,
    httpApiEndpoint: 1,
  },
  'src/composables/chat/useChatElevatedMode.ts': {
    httpRequest: 1,
    httpApiEndpoint: 1,
  },
  // Config and routing access now flows through the AppSettings and
  // ProviderConfiguration domain seams. Event subscription remains below.
  'src/composables/chat/useChatFeatureToggles.ts': { on: 1 },
  'src/composables/chat/useChatHistory.ts': { call: 1, waitForConnection: 1 },
  'src/composables/chat/useChatRouteFeedback.ts': { call: 1 },
  'src/composables/chat/useChatRunModePreference.ts': { call: 4 },
  // TurnCommands now owns send/cancel/steer method selection.  Remaining calls
  // belong to the still-unmigrated chat bootstrap and feature surfaces.
  'src/composables/chat/useChatSend.ts': { call: 1 },
  'src/composables/chat/useChatSessionSubscription.ts': { call: 7, waitForConnection: 3 },
  'src/composables/chat/useChatSlashCommands.ts': { call: 5 },
  'src/composables/chat/useChatUsageWidget.ts': { call: 2 },
  'src/composables/chat/useVoiceInput.ts': {
    httpRequest: 1,
    httpApiEndpoint: 1,
    httpAuthToken: 1,
    httpAuthorizationHeader: 1,
  },
  'src/composables/sessions/useSessionInspect.ts': { call: 3, waitForConnection: 1 },
  'src/utils/chat/artifactAccess.ts': {
    httpRequest: 2,
    httpApiEndpoint: 2,
    httpAuthToken: 2,
    httpAuthorizationHeader: 2,
    httpSessionKeyHeader: 2,
  },
  'src/utils/chat/attachmentAccess.ts': {
    httpRequest: 1,
    httpApiEndpoint: 1,
    httpAuthToken: 1,
    httpAuthorizationHeader: 1,
    httpSessionKeyHeader: 1,
  },
  'src/views/ChatView.vue': {
    call: 2,
    supportsEvent: 1,
    // Turn capability probes are owned by TurnCommands; Goal mode probes are
    // owned by GoalCenter; the remaining method probes migrate independently.
    supportsMethod: 5,
    waitForConnection: 1,
    httpRequest: 1,
    httpApiEndpoint: 1,
    httpAuthToken: 1,
    httpAuthorizationHeader: 1,
    httpSessionKeyHeader: 1,
  },
  'src/views/SessionsView.vue': {
    call: 1,
  },
}
