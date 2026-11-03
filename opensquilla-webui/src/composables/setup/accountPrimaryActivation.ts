import type { RouterResolutionAction, SetupWorkflow } from '@/modules/setupWorkflow'
import { submitPrimaryProviderTransition } from './primaryProviderTransition'

export interface AccountPrimaryConfig {
  llm_profiles?: Record<string, { model?: unknown }>
  squilla_router?: { tiers?: Record<string, { provider?: unknown; model?: unknown }> }
  llm_ensemble?: { candidates?: readonly { provider?: unknown; model?: unknown }[] }
}

interface PrimaryStatus {
  hasConfig?: boolean
  llmConfigured?: boolean
  llmSource?: string
  sectionDetails?: { llm?: { providerResolution?: { status?: unknown } } }
}

/** Runtime defaults are not a saved primary, including after unrelated preferences were saved. */
export function primaryProviderIsConfigured(
  llm: { provider?: unknown } | undefined,
  status: PrimaryStatus,
  effective: { fields?: Record<string, { source?: unknown }> },
): boolean {
  if (!String(llm?.provider || '').trim()) return false
  if (String(status.sectionDetails?.llm?.providerResolution?.status || '').trim().toLowerCase() === 'conflict') return false
  const source = String(effective.fields?.['llm.provider']?.source || '')
  if (source) return source !== 'default' || status.llmConfigured === true
  if (status.hasConfig !== false) return true
  if (status.llmConfigured === true) return true
  if (status.llmConfigured === false) return false
  return ['explicit', 'env', 'not_required'].includes(String(status.llmSource || ''))
}

/** Use the same model selection in the settings button and the login coordinator. */
export function representativeAccountProviderModel(
  config: AccountPrimaryConfig,
  catalog: { providers?: readonly { providerId?: unknown; runtimeSupported?: unknown; defaultDirectModel?: unknown; defaultModel?: unknown }[] },
): string {
  const provider = 'tokenrhythm'
  const profile = Object.entries(config.llm_profiles || {}).find(([id]) => id.trim().toLowerCase() === provider)?.[1]
  const saved = String(profile?.model || '').trim()
  if (saved) return saved
  const spec = catalog.providers?.find(item => item.runtimeSupported && String(item.providerId || '').trim().toLowerCase() === provider)
  const direct = String(spec?.defaultDirectModel || '').trim()
  if (direct) return direct
  for (const entry of [
    ...Object.values(config.squilla_router?.tiers || {}),
    ...(config.llm_ensemble?.candidates || []),
  ]) {
    if (String(entry.provider || '').trim().toLowerCase() !== provider) continue
    const model = String(entry.model || '').trim()
    if (model) return model
  }
  return String(spec?.defaultModel || '').trim()
}

export interface AccountPrimaryActivationProgress {
  providerCommitted: boolean
  bindingCommitted: boolean
}

export class AccountPrimaryActivationError extends Error {
  readonly name = 'AccountPrimaryActivationError'
  constructor(
    readonly cause: unknown,
    readonly phase: 'provider' | 'binding' | 'refresh',
    readonly providerCommitted: boolean,
    readonly bindingCommitted: boolean,
  ) {
    super(cause instanceof Error ? cause.message : 'Account primary activation failed')
  }
}

export type AccountPrimaryActivationResult = AccountPrimaryActivationProgress & {
  status: 'skipped' | 'cancelled' | 'success'
  routerAction?: RouterResolutionAction
}

export interface AccountPrimaryActivationOptions {
  setupWorkflow: Pick<SetupWorkflow, 'profile'>
  currentProvider: string
  hasStoredProfile: boolean
  model: string
  ready?: boolean
  providerLabel?: (id: string) => string
  assertCurrent?: () => void
  bind: () => Promise<unknown>
  refresh: () => Promise<unknown>
  onRouterAction?: (action: RouterResolutionAction | undefined) => void
}

/** Persist the provider before binding credentials; uncertain writes are never replayed. */
export async function activateAccountPrimary(options: AccountPrimaryActivationOptions): Promise<AccountPrimaryActivationResult> {
  let providerCommitted = false
  let bindingCommitted = false
  let phase: AccountPrimaryActivationError['phase'] = 'provider'
  let routerAction: RouterResolutionAction | undefined
  if (options.ready === false) return { status: 'skipped', providerCommitted, bindingCommitted }
  const assertCurrent = options.assertCurrent ?? (() => {})
  try {
    assertCurrent()
    if (options.currentProvider.trim().toLowerCase() !== 'tokenrhythm') {
      const command = { providerId: 'tokenrhythm', credentialSource: 'account' as const, imageGenerationIntent: 'preserve' as const }
      const transition = await submitPrimaryProviderTransition(
        options.hasStoredProfile ? command : { ...command, model: options.model },
        async snapshot => {
          const result = options.hasStoredProfile
            ? await options.setupWorkflow.profile.activateProfile(snapshot)
            : await options.setupWorkflow.profile.upsertAndActivateProfile(snapshot)
          // Record acknowledgement before the transition's post-submit guard.
          providerCommitted = true
          return result
        },
        options.providerLabel,
        assertCurrent,
      )
      if (!transition) return { status: 'cancelled', providerCommitted, bindingCommitted }
      routerAction = transition.routerAction
      options.onRouterAction?.(routerAction)
    }
    phase = 'binding'
    assertCurrent()
    await options.bind()
    bindingCommitted = true
    assertCurrent()
    phase = 'refresh'
    await options.refresh()
    assertCurrent()
    return { status: 'success', providerCommitted, bindingCommitted, ...(routerAction ? { routerAction } : {}) }
  } catch (cause) {
    throw new AccountPrimaryActivationError(cause, phase, providerCommitted, bindingCommitted)
  }
}
