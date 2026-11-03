// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useConfirm } from '@/composables/useConfirm'
import { SetupWorkflowError, type ActivateProfile, type SetupWorkflow, type UpsertAndActivateProfile } from '@/modules/setupWorkflow'
import {
  activateAccountPrimary,
  AccountPrimaryActivationError,
  primaryProviderIsConfigured,
  representativeAccountProviderModel,
} from './accountPrimaryActivation'

const conflict = () => new SetupWorkflowError('conflict', 'Routes need a choice', 'router-provider-conflict', undefined, {
  reason: 'router_provider_conflict', providerId: 'tokenrhythm', conflictProviders: ['openrouter'],
  allowedRouterActions: ['use_recommended', 'disable'],
})

function fixture(currentProvider = 'openai', hasStoredProfile = false) {
  const calls: string[] = []
  const activateProfile = vi.fn(async (_command: ActivateProfile) => { calls.push('activate'); return {} })
  const upsertAndActivateProfile = vi.fn(async (_command: UpsertAndActivateProfile) => { calls.push('upsert'); return {} })
  const bind = vi.fn(async () => { calls.push('bind') })
  const refresh = vi.fn(async () => { calls.push('refresh') })
  const setupWorkflow = { profile: { activateProfile, upsertAndActivateProfile } } as unknown as Pick<SetupWorkflow, 'profile'>
  return {
    calls, activateProfile, upsertAndActivateProfile, bind, refresh,
    options: { setupWorkflow, currentProvider, hasStoredProfile, model: 'default-model', bind, refresh },
  }
}

afterEach(() => useConfirm().resolveConfirm(false))

describe('shared account primary activation', () => {
  it.each([false, true])('commits the provider before binding and refreshing (stored: %s)', async stored => {
    const f = fixture('openai', stored)
    await expect(activateAccountPrimary(f.options)).resolves.toMatchObject({ status: 'success', providerCommitted: true, bindingCommitted: true })
    expect(f.calls).toEqual([stored ? 'activate' : 'upsert', 'bind', 'refresh'])
    expect((stored ? f.activateProfile : f.upsertAndActivateProfile).mock.calls[0]?.[0]).toMatchObject({
      providerId: 'tokenrhythm', credentialSource: 'account', imageGenerationIntent: 'preserve',
      ...(stored ? {} : { model: 'default-model' }),
    })
  })

  it('changes only the binding when TokenRhythm is already primary', async () => {
    const f = fixture('tokenrhythm')
    await expect(activateAccountPrimary(f.options)).resolves.toMatchObject({ status: 'success', providerCommitted: false, bindingCommitted: true })
    expect(f.calls).toEqual(['bind', 'refresh'])
  })

  it('skips unavailable credentials without consuming a provider mutation', async () => {
    const f = fixture()
    await expect(activateAccountPrimary({ ...f.options, ready: false })).resolves.toMatchObject({ status: 'skipped' })
    expect(f.calls).toEqual([])
  })

  it('treats the existing router confirmation cancellation as a terminal outcome', async () => {
    const f = fixture()
    f.upsertAndActivateProfile.mockRejectedValueOnce(conflict())
    const pending = activateAccountPrimary(f.options)
    await vi.waitFor(() => expect(useConfirm().confirmState.value).not.toBeNull())
    useConfirm().resolveConfirm(false)
    await expect(pending).resolves.toMatchObject({ status: 'cancelled', providerCommitted: false, bindingCommitted: false })
    expect(f.upsertAndActivateProfile).toHaveBeenCalledTimes(1)
    expect(f.bind).not.toHaveBeenCalled()
    expect(f.refresh).not.toHaveBeenCalled()
  })

  it('does not retry or bind a superseded login after its router confirmation', async () => {
    const f = fixture()
    const stale = new Error('Login was superseded')
    let current = true
    const assertCurrent = () => { if (!current) throw stale }
    f.upsertAndActivateProfile.mockRejectedValueOnce(conflict())
    const pending = activateAccountPrimary({ ...f.options, assertCurrent })
    await vi.waitFor(() => expect(useConfirm().confirmState.value).not.toBeNull())
    current = false
    useConfirm().resolveConfirmChoice('primary')
    await expect(pending).rejects.toMatchObject({ cause: stale, providerCommitted: false, bindingCommitted: false })
    expect(f.upsertAndActivateProfile).toHaveBeenCalledTimes(1)
    expect(f.bind).not.toHaveBeenCalled()
  })

  it('retains acknowledgement when the login changes during the provider write', async () => {
    const f = fixture()
    let current = true
    f.upsertAndActivateProfile.mockImplementationOnce(async () => { current = false; return {} })
    const assertCurrent = () => { if (!current) throw new Error('Login changed') }
    await expect(activateAccountPrimary({ ...f.options, assertCurrent })).rejects.toMatchObject({
      phase: 'provider', providerCommitted: true, bindingCommitted: false,
    })
    expect(f.bind).not.toHaveBeenCalled()
  })

  it.each(['provider', 'binding', 'refresh'] as const)('preserves the committed phase when %s fails without replaying writes', async phase => {
    const f = fixture()
    const failure = new SetupWorkflowError('unavailable', 'Synthetic transport failure')
    if (phase === 'provider') f.upsertAndActivateProfile.mockRejectedValueOnce(failure)
    if (phase === 'binding') f.bind.mockRejectedValueOnce(failure)
    if (phase === 'refresh') f.refresh.mockRejectedValueOnce(failure)
    await expect(activateAccountPrimary(f.options)).rejects.toMatchObject({
      name: 'AccountPrimaryActivationError', cause: failure, phase,
      providerCommitted: phase !== 'provider', bindingCommitted: phase === 'refresh',
    } satisfies Partial<AccountPrimaryActivationError>)
    expect(f.upsertAndActivateProfile).toHaveBeenCalledTimes(1)
    expect(f.bind).toHaveBeenCalledTimes(phase === 'provider' ? 0 : 1)
    expect(f.refresh).toHaveBeenCalledTimes(phase === 'refresh' ? 1 : 0)
  })
})

describe('account primary snapshot semantics', () => {
  it('shares provenance-aware defaults without promoting an unresolved provider', () => {
    const effective = { fields: { 'llm.provider': { source: 'default' } } }
    expect(primaryProviderIsConfigured({ provider: 'tokenrhythm' }, { hasConfig: true, llmConfigured: false }, effective)).toBe(false)
    expect(primaryProviderIsConfigured({ provider: 'tokenrhythm' }, { llmConfigured: true }, effective)).toBe(true)
    expect(primaryProviderIsConfigured({ provider: 'tokenrhythm' }, { llmConfigured: true, sectionDetails: { llm: { providerResolution: { status: 'conflict' } } } }, effective)).toBe(false)
  })

  it('uses the saved model, then the catalog direct default before historical routes', () => {
    const catalog = { providers: [{ providerId: 'tokenrhythm', runtimeSupported: true, defaultDirectModel: 'direct', defaultModel: 'legacy' }] }
    const routes = { squilla_router: { tiers: { c0: { provider: 'tokenrhythm', model: 'old-tier' } } } }
    expect(representativeAccountProviderModel({ ...routes, llm_profiles: { tokenrhythm: { model: 'saved' } } }, catalog)).toBe('saved')
    expect(representativeAccountProviderModel(routes, catalog)).toBe('direct')
    expect(representativeAccountProviderModel(routes, { providers: [] })).toBe('old-tier')
  })
})
