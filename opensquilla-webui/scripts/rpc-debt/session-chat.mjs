export const lane = 'session-chat'

export const debt = {
  'src/composables/chat/sessionBootstrapAdmission.ts': { waitForConnection: 2 },
  'src/composables/chat/useChatApprovals.ts': {},
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
  // Config and routing access now flows through AppSettings, ProviderConfiguration,
  // and the SessionConversation event seam.
  'src/composables/chat/useChatHistory.ts': {},
  'src/composables/chat/useChatRouteFeedback.ts': {},
  'src/composables/chat/useChatRunModePreference.ts': { call: 4 },
  // TurnCommands now owns send/cancel/steer method selection.  Remaining calls
  // belong to the still-unmigrated chat bootstrap and feature surfaces.
  'src/composables/chat/useChatSend.ts': { call: 1 },
  'src/composables/chat/useChatSessionSubscription.ts': {},
  'src/composables/chat/useChatSlashCommands.ts': {},
  'src/composables/chat/useChatUsageWidget.ts': {},
  'src/composables/chat/useVoiceInput.ts': {
    httpRequest: 1,
    httpApiEndpoint: 1,
    httpAuthToken: 1,
    httpAuthorizationHeader: 1,
  },
  'src/composables/sessions/useSessionInspect.ts': {},
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
    // Workbench/document capability probes and attachment HTTP remain owned by
    // the ArtifactWorkbench slice; all session transport calls are migrated.
    supportsMethod: 3,
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
