import { createApp, watch } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import { router } from './router'
import i18n from './i18n'
import { useAppStore } from './stores/app'
import { useRpcStore } from './stores/rpc'
import { createGatewayAdapters } from './adapters/gateway/gatewayAdapters'
import { createPrivateHttpTransport } from './adapters/gateway/privateHttpTransport'
import { SESSION_DIRECTORY_KEY } from './modules/sessionDirectory'
import { SESSION_DIRECTORY_CHANGES_KEY } from './modules/sessionDirectoryChanges'
import { SESSION_LIFECYCLE_KEY } from './modules/sessionLifecycle'
import { SESSION_ROUTING_KEY } from './modules/sessionRouting'
import { TURN_COMMANDS_KEY } from './modules/turnCommands'
import { PENDING_INPUT_QUEUE_KEY } from './modules/pendingInputQueue'
import { APPROVAL_CENTER_KEY } from './modules/approvalCenter'
import { GOAL_CENTER_KEY } from './modules/goalCenter'
import { GOAL_CONTINUITY_KEY } from './modules/goalContinuity'
import { PLAN_CENTER_KEY } from './modules/planCenter'
import { META_RUN_CENTER_KEY } from './modules/metaRunCenter'
import { APP_SETTINGS_KEY } from './modules/appSettings'
import { PROVIDER_CONFIGURATION_KEY } from './modules/providerConfiguration'
import { SETUP_WORKFLOW_KEY } from './modules/setupWorkflow'
import { MIGRATION_OPERATIONS_KEY } from './modules/migrationOperations'
import { WORKSPACE_CATALOG_KEY } from './modules/workspaceCatalog'
import { SANDBOX_RUNTIME_KEY } from './modules/sandboxRuntime'
import { USAGE_REPORTING_KEY } from './modules/usageReporting'
import { COMMAND_CATALOG_KEY } from './modules/commandCatalog'
import { ROUTE_FEEDBACK_KEY } from './modules/routeFeedback'
import { PROMPT_CACHE_LEASE_KEY } from './modules/promptCacheLease'
import { CLARIFICATION_SUBMISSION_KEY } from './modules/clarificationSubmission'
import { SESSION_MAINTENANCE_KEY } from './modules/sessionMaintenance'
import { OBSERVABILITY_KEY } from './modules/observability'
import { SKILL_CATALOG_KEY } from './modules/skillCatalog'
import { AGENT_CATALOG_KEY } from './modules/agentCatalog'
import { CRON_SCHEDULER_KEY } from './modules/cronScheduler'
import { ARTIFACT_WORKBENCH_KEY } from './modules/artifactWorkbench'
import { CHANNEL_ADMINISTRATION_KEY } from './modules/channelAdministration'
import { CHANNEL_SETUP_KEY } from './modules/channelSetup'
import { MEMORY_PROFILE_IMPORT_KEY } from './modules/memoryProfileImport'
import { AUDIO_TRANSCRIPTION_KEY } from './modules/audioTranscription'
import { GATEWAY_ACCESS_KEY } from './modules/gatewayAccess'
import { CONVERSATION_EVENTS_KEY } from './modules/conversationEvents'
import { SESSION_READ_LIFECYCLE_FACTORY_KEY } from './modules/sessionReadLifecycle'
import { SESSION_INSPECTION_KEY } from './modules/sessionInspection'
import 'katex/dist/katex.min.css'
import './assets/base.css'
import './themes/tokens' // eagerly bundles every value theme's token block
import './styles/control-visual-system.css'
import './styles/route-fx.css'
import './styles/chat-markdown.css'
import './styles/chat-shared.css'
import './styles/apple-modern.css'

const app = createApp(App)
app.use(createPinia())
app.use(router)
app.use(i18n)

const appStore = useAppStore()
appStore.initTheme()

const rpcStore = useRpcStore()
rpcStore.init()
const gatewayAdapters = createGatewayAdapters(rpcStore, {
  http: createPrivateHttpTransport(),
})
appStore.bindAppSettings(gatewayAdapters.appSettings)
watch(() => rpcStore.state, (state) => {
  if (state === 'connected' && appStore.pendingChannelNoticeLocale) {
    void appStore.syncLocaleToGateway()
  }
})
app.provide(
  SESSION_DIRECTORY_KEY,
  gatewayAdapters.sessionDirectory,
)
app.provide(GATEWAY_ACCESS_KEY, gatewayAdapters.gatewayAccess)
app.provide(CONVERSATION_EVENTS_KEY, gatewayAdapters.conversationEvents)
app.provide(
  SESSION_READ_LIFECYCLE_FACTORY_KEY,
  gatewayAdapters.sessionReadLifecycleFactory,
)
app.provide(SESSION_INSPECTION_KEY, gatewayAdapters.sessionInspection)
app.provide(
  SESSION_DIRECTORY_CHANGES_KEY,
  gatewayAdapters.sessionDirectoryChanges,
)
app.provide(
  SESSION_LIFECYCLE_KEY,
  gatewayAdapters.sessionLifecycle,
)
app.provide(SESSION_ROUTING_KEY, gatewayAdapters.sessionRouting)
app.provide(TURN_COMMANDS_KEY, gatewayAdapters.turnCommands)
app.provide(PENDING_INPUT_QUEUE_KEY, gatewayAdapters.pendingInputQueue)
app.provide(APPROVAL_CENTER_KEY, gatewayAdapters.approvalCenter)
app.provide(GOAL_CENTER_KEY, gatewayAdapters.goalCenter)
app.provide(GOAL_CONTINUITY_KEY, gatewayAdapters.goalContinuity)
app.provide(PLAN_CENTER_KEY, gatewayAdapters.planCenter)
app.provide(META_RUN_CENTER_KEY, gatewayAdapters.metaRunCenter)
app.provide(APP_SETTINGS_KEY, gatewayAdapters.appSettings)
app.provide(PROVIDER_CONFIGURATION_KEY, gatewayAdapters.providerConfiguration)
app.provide(SETUP_WORKFLOW_KEY, gatewayAdapters.setupWorkflow)
app.provide(MIGRATION_OPERATIONS_KEY, gatewayAdapters.migrationOperations)
app.provide(WORKSPACE_CATALOG_KEY, gatewayAdapters.workspaceCatalog)
app.provide(SANDBOX_RUNTIME_KEY, gatewayAdapters.sandboxRuntime)
app.provide(USAGE_REPORTING_KEY, gatewayAdapters.usageReporting)
app.provide(COMMAND_CATALOG_KEY, gatewayAdapters.commandCatalog)
app.provide(ROUTE_FEEDBACK_KEY, gatewayAdapters.routeFeedback)
app.provide(PROMPT_CACHE_LEASE_KEY, gatewayAdapters.promptCacheLease)
app.provide(CLARIFICATION_SUBMISSION_KEY, gatewayAdapters.clarificationSubmission)
app.provide(SESSION_MAINTENANCE_KEY, gatewayAdapters.sessionMaintenance)
app.provide(OBSERVABILITY_KEY, gatewayAdapters.observability)
app.provide(SKILL_CATALOG_KEY, gatewayAdapters.skillCatalog)
app.provide(AGENT_CATALOG_KEY, gatewayAdapters.agentCatalog)
app.provide(CRON_SCHEDULER_KEY, gatewayAdapters.cronScheduler)
app.provide(CHANNEL_ADMINISTRATION_KEY, gatewayAdapters.channelAdministration)
app.provide(CHANNEL_SETUP_KEY, gatewayAdapters.channelSetup)
app.provide(ARTIFACT_WORKBENCH_KEY, gatewayAdapters.artifactWorkbench)
app.provide(MEMORY_PROFILE_IMPORT_KEY, gatewayAdapters.memoryProfileImport)
app.provide(AUDIO_TRANSCRIPTION_KEY, gatewayAdapters.audioTranscription)
router.afterEach(() => {
  rpcStore.applyLinkTokenFromUrl()
})

// Resolve + load the active locale before mounting so the first paint is
// already in the right language (no English flash). initLocale never rejects
// (it falls back to en internally); finally() guarantees the app still mounts.
appStore.initLocale().finally(() => {
  app.mount('#app')
})
