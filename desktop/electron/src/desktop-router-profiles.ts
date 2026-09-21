import type { RouterTier } from './router-tier-normalization.js'
import { ROUTER_PROFILES } from './generated/desktop-router-catalog.js'

export { ROUTER_PROFILES }

export class DesktopRoutingConfigurationError extends Error {
  constructor() {
    super('Model routing settings conflict with the selected provider. '
      + 'Choose a supported routing mode, or use "Reset setup" to configure it again.')
    this.name = 'DesktopRoutingConfigurationError'
  }
}

export function supportsRouter(provider: string): boolean {
  return Object.hasOwn(ROUTER_PROFILES, provider)
}

export function defaultRouterTiers(provider: string, mode: string): Record<string, RouterTier> {
  if (mode === 'disabled') return {}
  if (!supportsRouter(provider)
    || (mode !== 'recommended' && !(mode === 'openrouter-mix' && provider === 'openrouter'))) {
    throw new DesktopRoutingConfigurationError()
  }
  // Callers edit their own ladder; the generated catalog remains authoritative.
  return structuredClone(ROUTER_PROFILES[provider])
}
