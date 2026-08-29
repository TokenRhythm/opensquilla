import type { SessionDirectory } from '@/modules/sessionDirectory'
import type { SessionDirectoryChanges } from '@/modules/sessionDirectoryChanges'
import type { SessionLifecycle } from '@/modules/sessionLifecycle'
import { createPrivateGatewayTransports } from './privateTransports'
import { createV4SessionDirectory } from './sessionDirectoryV4'
import { createV4SessionDirectoryChanges } from './sessionDirectoryChangesV4'
import { createV4SessionLifecycle } from './sessionLifecycleV4'

type RpcStoreTransportSource = Parameters<typeof createPrivateGatewayTransports>[0]

export interface GatewayAdapters {
  readonly sessionDirectory: SessionDirectory
  readonly sessionDirectoryChanges: SessionDirectoryChanges
  readonly sessionLifecycle: SessionLifecycle
}

/** Wire Gateway-backed domain Adapters without leaking generic transports. */
export function createGatewayAdapters(source: RpcStoreTransportSource): GatewayAdapters {
  const transports = createPrivateGatewayTransports(source)
  const sessionDirectory = createV4SessionDirectory(transports.rpc)
  return {
    sessionDirectory,
    sessionDirectoryChanges: createV4SessionDirectoryChanges(
      transports.rpc,
      transports.events,
    ),
    sessionLifecycle: createV4SessionLifecycle(transports.rpc),
  }
}
