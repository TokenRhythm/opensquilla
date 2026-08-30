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
  'src/composables/chat/useChatFeatureToggles.ts': {
    call: 7,
    on: 1,
    supportsMethod: 1,
    waitForConnection: 2,
  },
  'src/composables/chat/useChatGoals.ts': { call: 5, on: 1 },
  'src/composables/chat/useChatHistory.ts': { call: 1, waitForConnection: 1 },
  'src/composables/chat/useChatMetaDraftRecovery.ts': {
    call: 1,
    markMethodUnavailable: 1,
    supportsMethod: 1,
    waitForConnection: 1,
  },
  'src/composables/chat/useChatPlans.ts': { call: 4, on: 6 },
  'src/composables/chat/useChatRouteFeedback.ts': { call: 1 },
  'src/composables/chat/useChatRunModePreference.ts': { call: 4 },
  // TurnCommands now owns send/cancel/steer method selection.  The one
  // remaining call is the unrelated meta-draft compatibility path, which is
  // intentionally deferred to the Meta domain slice.
  'src/composables/chat/useChatSend.ts': { call: 1 },
  'src/composables/chat/useChatSessionSubscription.ts': { call: 7, waitForConnection: 3 },
  'src/composables/chat/useChatSlashCommands.ts': { call: 6 },
  'src/composables/chat/useChatUsageWidget.ts': { call: 2 },
  'src/composables/chat/useVoiceInput.ts': {
    httpRequest: 1,
    httpApiEndpoint: 1,
    httpAuthToken: 1,
    httpAuthorizationHeader: 1,
  },
  'src/composables/chat/useMetaRuns.ts': { call: 5, on: 4 },
  'src/composables/chat/useMetaSkillSetup.ts': {
    call: 1,
    waitForConnection: 1,
    waitForConnectionReference: 1,
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
    call: 5,
    on: 1,
    supportsEvent: 1,
    // Turn capability probes are owned by TurnCommands; the remaining
    // method probes belong to non-Turn domains and migrate independently.
    supportsMethod: 9,
    waitForConnection: 2,
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
