import type { Platform } from '@/platform/types'
import { sessionApplicationLink, sessionGatewayUrl } from '@/types/references'

/** One browser-openable session link. Preserve the WebUI's public origin and base;
 * Desktop's private renderer origin instead needs its owned Gateway's Control UI. */
export async function currentSessionLink(
  sessionKey: string,
  platform: Pick<Platform, 'capabilities' | 'gateway'>,
): Promise<string> {
  if (!platform.capabilities.isDesktop) return sessionApplicationLink(sessionKey)
  const connection = await platform.gateway.getConnection?.()
  const endpoint = connection?.httpUrl || connection?.wsUrl
  if (connection?.status !== 'ready' || !endpoint) {
    throw new Error('Gateway is not ready')
  }
  const url = new URL(endpoint)
  if (!['http:', 'https:', 'ws:', 'wss:'].includes(url.protocol)) {
    throw new Error('Gateway URL is unavailable')
  }
  // Desktop configures its Gateway Control UI at /control. Its local
  // opensquilla-app://desktop renderer instead uses / as the router base.
  return sessionGatewayUrl(sessionKey, endpoint, '/control')
}
