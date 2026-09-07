import type { SessionDirectory } from '@/modules/sessionDirectory'
import type { SessionDirectoryChanges } from '@/modules/sessionDirectoryChanges'
import type { SessionLifecycle } from '@/modules/sessionLifecycle'
import type { SessionRouting } from '@/modules/sessionRouting'
import type { TurnCommands } from '@/modules/turnCommands'
import type { PendingInputQueuePort } from '@/modules/pendingInputQueue'
import { createPrivateGatewayTransports } from './privateTransports'
import { createV4SessionDirectory } from './sessionDirectoryV4'
import { createV4SessionDirectoryChanges } from './sessionDirectoryChangesV4'
import { createV4SessionLifecycle } from './sessionLifecycleV4'
import { createV4SessionRouting } from './sessionRoutingV4'
import { createV4TurnCommands } from './turnCommandsV4'
import { createV4PendingInputQueue } from './pendingInputQueueV4'
import { createApprovalCenterV4 } from './approvalCenterV4'
import type { ApprovalCenter } from '@/modules/approvalCenter'
import type { HttpTransport } from './privateHttpTransport'
import type { GoalCenter } from '@/modules/goalCenter'
import { createV4GoalCenter } from './goalCenterV4'
import { createV4GoalContinuity } from './goalContinuityV4'
import type { GoalContinuity } from '@/modules/goalContinuity'
import type { PlanCenter } from '@/modules/planCenter'
import { createV4PlanCenter } from './planCenterV4'
import type { MetaRunCenter } from '@/modules/metaRunCenter'
import { createV4MetaRunCenter } from './metaRunCenterV4'
import type { AppSettings } from '@/modules/appSettings'
import { createV4AppSettings } from './appSettingsV4'
import type { ProviderConfiguration } from '@/modules/providerConfiguration'
import { createV4ProviderConfiguration } from './providerConfigurationV4'
import type { SetupWorkflow } from '@/modules/setupWorkflow'
import { createV4SetupWorkflow } from './setupWorkflowV4'
import type { MigrationOperations } from '@/modules/migrationOperations'
import { createV4MigrationOperations } from './migrationOperationsV4'
import type { WorkspaceCatalog } from '@/modules/workspaceCatalog'
import { createV4WorkspaceCatalog } from './workspaceCatalogV4'
import type { SandboxRuntime } from '@/modules/sandboxRuntime'
import { createV4SandboxRuntime } from './sandboxRuntimeV4'
import type { UsageReporting } from '@/modules/usageReporting'
import { createV4UsageReporting } from './usageReportingV4'
import type { CommandCatalog } from '@/modules/commandCatalog'
import { createV4CommandCatalog } from './commandCatalogV4'
import type { RouteFeedback } from '@/modules/routeFeedback'
import { createV4RouteFeedback } from './routeFeedbackV4'
import type { PromptCacheLease } from '@/modules/promptCacheLease'
import { createV4PromptCacheLease } from './promptCacheLeaseV4'
import type { ClarificationSubmission } from '@/modules/clarificationSubmission'
import { createV4ClarificationSubmission } from './clarificationSubmissionV4'
import type { SessionMaintenance } from '@/modules/sessionMaintenance'
import { createV4SessionMaintenance } from './sessionMaintenanceV4'
import type { Observability } from '@/modules/observability'
import { createV4Observability } from './observabilityV4'
import type { SkillCatalog } from '@/modules/skillCatalog'
import { createV4SkillCatalog } from './skillCatalogV4'
import type { AgentCatalog } from '@/modules/agentCatalog'
import { createV4AgentCatalog } from './agentCatalogV4'
import type { CronScheduler } from '@/modules/cronScheduler'
import { createV4CronScheduler } from './cronSchedulerV4'
import type { ChannelAdministration } from '@/modules/channelAdministration'
import { createV4ChannelAdministration } from './channelAdministrationV4'
import type { ChannelSetup } from '@/modules/channelSetup'
import { createV4ChannelSetup } from './channelSetupV4'
import type { ArtifactWorkbench } from '@/modules/artifactWorkbench'
import { createV4ArtifactWorkbench } from './artifactWorkbenchV4'
import type { MemoryProfileImport } from '@/modules/memoryProfileImport'
import { createV4MemoryProfileImport } from './memoryProfileImportV4'
import type { AudioTranscription } from '@/modules/audioTranscription'
import { createV4AudioTranscription } from './audioTranscriptionV4'
import type { GatewayAccess } from '@/modules/gatewayAccess'
import { createV4GatewayAccess } from './gatewayAccessV4'
import type { ConversationEvents } from '@/modules/conversationEvents'
import { createConversationEventTransport } from './conversationEventTransport'
import type { SessionReadLifecycleFactory } from '@/modules/sessionReadLifecycle'
import { createSessionReadLifecycleFactory } from '@/modules/sessionReadLifecycle'
import { createV4SessionReadPort } from './sessionReadPortV4'
import type { SessionInspection } from '@/modules/sessionInspection'
import { createV4SessionInspection } from './sessionInspectionV4'

type RpcStoreTransportSource = Parameters<typeof createPrivateGatewayTransports>[0]
  & Parameters<typeof createV4GatewayAccess>[0]

export interface GatewayAdapters {
  readonly gatewayAccess: GatewayAccess
  readonly conversationEvents: ConversationEvents
  readonly sessionReadLifecycleFactory: SessionReadLifecycleFactory
  readonly sessionInspection: SessionInspection
  readonly sessionDirectory: SessionDirectory
  readonly sessionDirectoryChanges: SessionDirectoryChanges
  readonly sessionLifecycle: SessionLifecycle
  readonly sessionRouting: SessionRouting
  readonly turnCommands: TurnCommands
  readonly pendingInputQueue: PendingInputQueuePort
  readonly approvalCenter: ApprovalCenter
  readonly goalCenter: GoalCenter
  readonly goalContinuity: GoalContinuity
  readonly planCenter: PlanCenter
  readonly metaRunCenter: MetaRunCenter
  readonly appSettings: AppSettings
  readonly providerConfiguration: ProviderConfiguration
  readonly setupWorkflow: SetupWorkflow
  readonly migrationOperations: MigrationOperations
  readonly workspaceCatalog: WorkspaceCatalog
  readonly sandboxRuntime: SandboxRuntime
  readonly usageReporting: UsageReporting
  readonly commandCatalog: CommandCatalog
  readonly routeFeedback: RouteFeedback
  readonly promptCacheLease: PromptCacheLease
  readonly clarificationSubmission: ClarificationSubmission
  readonly sessionMaintenance: SessionMaintenance
  readonly observability: Observability
  readonly skillCatalog: SkillCatalog
  readonly agentCatalog: AgentCatalog
  readonly cronScheduler: CronScheduler
  readonly channelAdministration: ChannelAdministration
  readonly channelSetup: ChannelSetup
  readonly artifactWorkbench: ArtifactWorkbench
  readonly memoryProfileImport: MemoryProfileImport
  readonly audioTranscription: AudioTranscription
}

export interface GatewayAdapterOptions {
  http?: HttpTransport
}

/** Wire Gateway-backed domain Adapters without leaking generic transports. */
export function createGatewayAdapters(
  source: RpcStoreTransportSource,
  options: GatewayAdapterOptions = {},
): GatewayAdapters {
  const transports = createPrivateGatewayTransports(source)
  const http: HttpTransport = options.http ?? {
    clearPreviewOrigin: async () => {
      throw new Error('Gateway HTTP transport is unavailable.')
    },
    fetchExternalArtifact: async () => {
      throw new Error('Gateway HTTP transport is unavailable.')
    },
    requestJson: async () => {
      throw new Error('Gateway HTTP transport is unavailable.')
    },
    requestBinary: async () => {
      throw new Error('Gateway HTTP transport is unavailable.')
    },
    requestBlob: async () => {
      throw new Error('Gateway HTTP transport is unavailable.')
    },
  }
  const sessionDirectory = createV4SessionDirectory(transports.rpc)
  const gatewayAccess = createV4GatewayAccess(source)
  const concurrentHistoryReads = () => gatewayAccess.concurrentHistoryReads
  const sessionReadLifecycleFactory = createSessionReadLifecycleFactory(
    createV4SessionReadPort(transports.rpc, { concurrentHistoryReads }),
  )
  const conversationEvents = createConversationEventTransport(transports.events)
  const adapters: GatewayAdapters = {
    gatewayAccess,
    conversationEvents,
    sessionReadLifecycleFactory,
    sessionInspection: createV4SessionInspection(
      transports.rpc,
      { concurrentHistoryReads },
    ),
    sessionDirectory,
    sessionDirectoryChanges: createV4SessionDirectoryChanges(
      transports.rpc,
      transports.events,
    ),
    sessionLifecycle: createV4SessionLifecycle(transports.rpc),
    sessionRouting: createV4SessionRouting(transports.rpc, transports.events),
    turnCommands: createV4TurnCommands(transports.rpc),
    pendingInputQueue: createV4PendingInputQueue(transports.rpc),
    approvalCenter: createApprovalCenterV4(transports.rpc, transports.events, { http }),
    goalCenter: createV4GoalCenter(transports.rpc),
    goalContinuity: createV4GoalContinuity(transports.rpc, transports.events),
    planCenter: createV4PlanCenter(transports.rpc, transports.events),
    metaRunCenter: createV4MetaRunCenter(transports.rpc, transports.events),
    appSettings: createV4AppSettings(transports.rpc),
    providerConfiguration: createV4ProviderConfiguration(transports.rpc, transports.events),
    setupWorkflow: createV4SetupWorkflow(transports.rpc),
    migrationOperations: createV4MigrationOperations(transports.rpc),
    workspaceCatalog: createV4WorkspaceCatalog(transports.rpc),
    sandboxRuntime: createV4SandboxRuntime(transports.rpc, transports.events),
    usageReporting: createV4UsageReporting(transports.rpc),
    commandCatalog: createV4CommandCatalog(transports.rpc),
    routeFeedback: createV4RouteFeedback(transports.rpc),
    promptCacheLease: createV4PromptCacheLease(transports.rpc),
    clarificationSubmission: createV4ClarificationSubmission(transports.rpc),
    sessionMaintenance: createV4SessionMaintenance(transports.rpc),
    observability: createV4Observability(transports.rpc, http),
    skillCatalog: createV4SkillCatalog(transports.rpc),
    agentCatalog: createV4AgentCatalog(transports.rpc),
    cronScheduler: createV4CronScheduler(transports.rpc, transports.events),
    channelAdministration: createV4ChannelAdministration(transports.rpc, transports.events),
    channelSetup: createV4ChannelSetup(transports.rpc),
    artifactWorkbench: createV4ArtifactWorkbench(transports.rpc, transports.events, http),
    memoryProfileImport: createV4MemoryProfileImport(transports.rpc),
    audioTranscription: createV4AudioTranscription(http),
  }
  return adapters
}
