export const lane = 'independent'

export const debt = {
  'src/components/SupportDiagnosticsMenu.vue': { call: 1, waitForConnection: 1 },
  'src/components/workbench/AppWorkbench.vue': { on: 2 },
  'src/composables/chat/useSessionArtifacts.ts': {
    call: 1,
    markMethodUnavailable: 1,
    supportsMethod: 1,
  },
  'src/composables/cron/useCronForm.ts': { call: 1 },
  'src/composables/cron/useCronJobs.ts': { call: 3, on: 1, waitForConnection: 1 },
  'src/composables/cron/useCronRuns.ts': { call: 1 },
  'src/composables/skills/useSkillDetailController.ts': { call: 1 },
  'src/composables/skills/useSkillProposals.ts': { call: 10 },
  'src/composables/skills/useSkillRegistry.ts': { call: 5, supportsMethod: 1 },
  'src/composables/skills/useSkillsCatalog.ts': { call: 1, waitForConnection: 1 },
  'src/composables/usage/useUsageQuery.ts': {
    call: 3,
    markMethodUnavailable: 2,
    supportsMethod: 1,
    waitForConnection: 1,
  },
  'src/composables/useAgentOptions.ts': { call: 2 },
  'src/views/AgentsView.vue': { call: 3 },
  'src/views/LogsView.vue': { call: 2, waitForConnection: 1 },
  'src/views/OverviewView.vue': { call: 3, waitForConnection: 2 },
  'src/views/SkillsView.vue': { call: 1, waitForConnection: 1 },
  'src/workbench/artifactDocumentProvider.ts': {
    call: 1,
    markMethodUnavailable: 1,
    supportsMethod: 1,
  },
  'src/workbench/artifactPromptAnnotationProvider.ts': {
    call: 1,
    markMethodUnavailable: 1,
    supportsMethod: 1,
  },
  'src/workbench/workbenchResourceProvider.ts': {
    call: 1,
    markMethodUnavailable: 1,
    supportsMethod: 1,
  },
}
