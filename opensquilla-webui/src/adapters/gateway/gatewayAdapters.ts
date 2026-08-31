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
import type { HttpRequestOptions } from './privateHttpTransport'
import type { GoalCenter } from '@/modules/goalCenter'
import { createV4GoalCenter } from './goalCenterV4'
import { createV4GoalContinuity } from './goalContinuityV4'
import type { GoalContinuity } from '@/modules/goalContinuity'
import type { MetaSkillCatalog } from '@/modules/metaSkillCatalog'
import { createV4MetaSkillCatalog } from './metaSkillCatalogV4'

type RpcStoreTransportSource = Parameters<typeof createPrivateGatewayTransports>[0]

export interface GatewayAdapters {
  readonly sessionDirectory: SessionDirectory
  readonly sessionDirectoryChanges: SessionDirectoryChanges
  readonly sessionLifecycle: SessionLifecycle
  readonly sessionRouting: SessionRouting
  readonly turnCommands: TurnCommands
  readonly pendingInputQueue: PendingInputQueuePort
  readonly approvalCenter: ApprovalCenter
  readonly goalCenter: GoalCenter
  readonly goalContinuity: GoalContinuity
  readonly metaSkillCatalog: MetaSkillCatalog
}

interface GatewayHttpSource {
  requestJson<T = unknown>(endpoint: string, options?: HttpRequestOptions): Promise<T>
}

export interface GatewayAdapterOptions {
  http?: GatewayHttpSource
}

/** Wire Gateway-backed domain Adapters without leaking generic transports. */
export function createGatewayAdapters(
  source: RpcStoreTransportSource,
  options: GatewayAdapterOptions = {},
): GatewayAdapters {
  const transports = createPrivateGatewayTransports(source)
  const http = options.http ?? {
    requestJson: async () => {
      throw new Error('Gateway HTTP transport is unavailable.')
    },
  }
  const sessionDirectory = createV4SessionDirectory(transports.rpc)
  const adapters: GatewayAdapters = {
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
    metaSkillCatalog: createV4MetaSkillCatalog(transports.rpc),
  }
  return adapters
}
