import type { SessionDirectory } from '@/modules/sessionDirectory'
import { createPrivateGatewayTransports } from './privateTransports'
import { createV4SessionDirectory } from './sessionDirectoryV4'

type RpcStoreTransportSource = Parameters<typeof createPrivateGatewayTransports>[0]

export interface GatewayAdapters {
  readonly sessionDirectory: SessionDirectory
}

/** Wire Gateway-backed domain Adapters without leaking generic transports. */
export function createGatewayAdapters(source: RpcStoreTransportSource): GatewayAdapters {
  const transports = createPrivateGatewayTransports(source)
  return {
    sessionDirectory: createV4SessionDirectory(transports.rpc),
  }
}
