// @vitest-environment happy-dom
import { effectScope, nextTick, reactive } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { AccountPrimaryActivation, TokenRhythmAccountState } from '../../../../contracts/desktop/tokenrhythm-account'
import type { AppSettings, SettingsObject } from '@/modules/appSettings'
import { SetupWorkflowError, type SetupWorkflow } from '@/modules/setupWorkflow'
import { invalidateReadiness, onReadinessInvalidated } from './readinessInvalidation'
import { useAccountPrimaryActivation } from './useAccountPrimaryActivation'

const { confirmChoice } = vi.hoisted(() => ({ confirmChoice: vi.fn(async () => 'cancel' as const) }))
vi.mock('@/composables/useConfirm', () => ({ useConfirm: () => ({ confirmChoice }) }))

const cleanups: Array<() => void> = []
afterEach(() => {
  for (const cleanup of cleanups.splice(0).reverse()) cleanup()
  vi.useRealTimers()
  vi.clearAllMocks()
})

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

function intent(id = 'login-a', accountId = 'a'): AccountPrimaryActivation {
  return { id, accountId, sessionId: `session-${accountId}` }
}

function fixture(options: { available?: boolean; ready?: boolean; pending?: boolean; config?: SettingsObject } = {}) {
  const account = reactive({
    supported: true,
    ready: options.ready ?? true,
    state: {
      revision: 1, status: 'signed_in', keyStatus: 'ready',
      profile: { accountId: 'a', sessionId: 'session-a', displayName: 'A', maskedPhone: '' },
      source: 'manual', selectedAccountId: null,
    } as TokenRhythmAccountState | null,
    primaryActivation: options.pending === false ? null : intent(),
    primaryMutationBusy: false,
    init: vi.fn(async () => {}),
    selectSource: vi.fn(async (_source: 'account' | 'manual', expected?: AccountPrimaryActivation) => {
      if (account.primaryActivation?.id !== expected?.id) throw new Error('ACCOUNT_PRIMARY_ACTIVATION_STALE')
      account.state!.source = 'account'
      account.state!.selectedAccountId = expected!.accountId
      account.primaryActivation = null
    }),
    cancelPrimaryActivation: vi.fn(async (id?: string) => {
      if (!id || account.primaryActivation?.id === id) account.primaryActivation = null
    }),
  })
  const gateway = reactive({
    isAvailable: options.available ?? true, isLocalOwner: true,
    deliveryIdentity: 'gateway-a', subscriptionEpoch: 1,
  })
  const config: SettingsObject = options.config ?? { llm: { provider: 'openrouter' }, llm_profiles: {} }
  const upsert = vi.fn(async () => ({}))
  const activate = vi.fn(async () => ({}))
  const catalog = vi.fn(async () => ({ providers: [{
    providerId: 'tokenrhythm', label: 'TokenRhythm', runtimeSupported: true,
    defaultDirectModel: 'account-default', defaultModel: 'fallback-model',
  }] }))
  const status = vi.fn(async () => ({ hasConfig: true, llmConfigured: true }))
  const readAll = vi.fn(async () => config)
  const readEffective = vi.fn(async () => ({ fields: { 'llm.provider': { value: 'openrouter', source: 'config' } } }))
  const setupWorkflow = { catalog, status, profile: { upsertAndActivateProfile: upsert, activateProfile: activate } } as unknown as SetupWorkflow
  const appSettings = { readAll, readEffective } as unknown as AppSettings
  const pushToast = vi.fn()
  const invalidated = vi.fn()
  cleanups.push(onReadinessInvalidated(invalidated))
  const scope = effectScope()
  function mount() {
    scope.run(() => useAccountPrimaryActivation(gateway, { account, setupWorkflow, appSettings, pushToast }))
    cleanups.push(() => scope.stop())
  }
  return { account, gateway, upsert, activate, catalog, status, readAll, readEffective, pushToast, invalidated, mount }
}

async function settle() {
  for (let index = 0; index < 20; index += 1) { await Promise.resolve(); await nextTick() }
}

const conflict = () => new SetupWorkflowError('conflict', 'router conflict', 'router-provider-conflict', undefined, {
  reason: 'router_provider_conflict', providerId: 'tokenrhythm', conflictProviders: ['openrouter'],
  allowedRouterActions: ['use_recommended', 'disable'],
})

describe('App account primary activation', () => {
  it('completes login without a Settings consumer and clears its intent once', async () => {
    const f = fixture()
    f.mount()
    await settle()
    expect(f.upsert).toHaveBeenCalledExactlyOnceWith({
      providerId: 'tokenrhythm', credentialSource: 'account', imageGenerationIntent: 'preserve', model: 'account-default',
    })
    expect(f.account.selectSource).toHaveBeenCalledExactlyOnceWith('account', intent())
    expect(f.account.state?.source).toBe('account')
    expect(f.account.primaryActivation).toBeNull()
    expect(f.account.primaryMutationBusy).toBe(false)
    expect(f.invalidated).toHaveBeenCalledOnce()
    expect(f.pushToast).toHaveBeenCalledOnce()
  })

  it('waits for a local Gateway, verified key, catalog and config before claiming', async () => {
    const f = fixture({ available: false, ready: false })
    const config = deferred<SettingsObject>()
    f.readAll.mockReturnValueOnce(config.promise)
    f.mount()
    await settle()
    expect(f.catalog).not.toHaveBeenCalled()
    f.gateway.isAvailable = true
    await settle()
    expect(f.catalog).not.toHaveBeenCalled()
    // retry-key can finish the same explicit login request.
    f.account.ready = true
    await settle()
    expect(f.account.primaryMutationBusy).toBe(false)
    expect(f.upsert).not.toHaveBeenCalled()
    config.resolve({ llm: { provider: 'openrouter' } })
    await settle()
    expect(f.upsert).toHaveBeenCalledOnce()
  })

  it('reconsiders an unready catalog on readiness and ignores duplicate state pushes', async () => {
    const f = fixture()
    f.catalog.mockResolvedValueOnce({ providers: [] })
    f.mount()
    await settle()
    expect(f.upsert).not.toHaveBeenCalled()
    expect(f.account.primaryMutationBusy).toBe(false)
    invalidateReadiness()
    await settle()
    expect(f.upsert).toHaveBeenCalledOnce()
    f.account.primaryActivation = intent()
    f.account.state!.revision += 1
    invalidateReadiness()
    await settle()
    expect(f.upsert).toHaveBeenCalledOnce()
  })

  it('retries a startup read failure without requiring another state notification', async () => {
    vi.useFakeTimers()
    const f = fixture()
    f.readAll.mockRejectedValueOnce(new SetupWorkflowError('unavailable', 'configuration starting'))
    f.mount()
    await settle()
    expect(f.account.primaryMutationBusy).toBe(false)
    expect(f.upsert).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(250)
    await settle()
    expect(f.readAll).toHaveBeenCalledTimes(3) // failed preparation, successful preparation, completion refresh
    expect(f.upsert).toHaveBeenCalledOnce()
    expect(f.account.selectSource).toHaveBeenCalledOnce()
    expect(f.pushToast).toHaveBeenCalledOnce()
  })

  it('terminates repeated preparation failures visibly without attempting a write', async () => {
    vi.useFakeTimers()
    const f = fixture()
    f.readAll.mockRejectedValue(new SetupWorkflowError('unavailable', 'configuration unavailable'))
    f.mount()
    await settle()
    await vi.advanceTimersByTimeAsync(1000)
    await settle()
    expect(f.readAll).toHaveBeenCalledTimes(3)
    expect(f.upsert).not.toHaveBeenCalled()
    expect(f.account.primaryMutationBusy).toBe(false)
    expect(f.account.primaryActivation).toBeNull()
    expect(f.pushToast).toHaveBeenCalledWith(expect.any(String), { tone: 'danger' })
    invalidateReadiness()
    await settle()
    expect(f.readAll).toHaveBeenCalledTimes(3)
  })

  it('waits for effective provenance before accepting a materialized TokenRhythm default', async () => {
    vi.useFakeTimers()
    const f = fixture({ config: { llm: { provider: 'tokenrhythm' }, theme: 'dark' } })
    f.status.mockResolvedValue({ hasConfig: true, llmConfigured: false })
    f.readEffective.mockRejectedValueOnce(new Error('effective config starting'))
      .mockResolvedValue({ fields: { 'llm.provider': { value: 'tokenrhythm', source: 'default' } } })
    f.mount()
    await settle()
    expect(f.account.primaryMutationBusy).toBe(false)
    expect(f.upsert).not.toHaveBeenCalled()
    expect(f.account.selectSource).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(250)
    await settle()
    expect(f.upsert).toHaveBeenCalledExactlyOnceWith({
      providerId: 'tokenrhythm', credentialSource: 'account', imageGenerationIntent: 'preserve', model: 'account-default',
    })
    expect(f.account.selectSource).toHaveBeenCalledOnce()
  })

  it('does not retry preparation after the login is cancelled during backoff', async () => {
    vi.useFakeTimers()
    const f = fixture()
    f.readAll.mockRejectedValueOnce(new Error('starting'))
    f.mount()
    await settle()
    await f.account.cancelPrimaryActivation()
    await vi.advanceTimersByTimeAsync(250)
    await settle()
    expect(f.readAll).toHaveBeenCalledOnce()
    expect(f.upsert).not.toHaveBeenCalled()
    expect(f.pushToast).not.toHaveBeenCalled()
  })

  it('does not activate on restored/refresh-only accounts without an explicit request', async () => {
    const f = fixture({ pending: false })
    f.account.state!.source = 'account'
    f.account.state!.selectedAccountId = 'a'
    f.mount()
    invalidateReadiness()
    await settle()
    expect(f.catalog).not.toHaveBeenCalled()
    expect(f.upsert).not.toHaveBeenCalled()
  })

  it('uses saved profiles and ignores an unconfigured materialized primary default', async () => {
    const f = fixture({ config: { llm: { provider: 'tokenrhythm' }, llm_profiles: { TokenRhythm: { model: 'saved-model' } } } })
    f.status.mockResolvedValue({ hasConfig: true, llmConfigured: false })
    f.readEffective.mockResolvedValue({ fields: { 'llm.provider': { value: 'tokenrhythm', source: 'default' } } })
    f.mount()
    await settle()
    expect(f.activate).toHaveBeenCalledExactlyOnceWith({ providerId: 'tokenrhythm', credentialSource: 'account', imageGenerationIntent: 'preserve' })
    expect(f.upsert).not.toHaveBeenCalled()
  })

  it('drops cancelled in-flight provider results without binding or retry', async () => {
    const f = fixture()
    const write = deferred<Record<string, unknown>>()
    f.upsert.mockReturnValueOnce(write.promise)
    f.mount()
    await settle()
    await f.account.cancelPrimaryActivation()
    write.resolve({})
    await settle()
    invalidateReadiness()
    await settle()
    expect(f.upsert).toHaveBeenCalledOnce()
    expect(f.account.selectSource).not.toHaveBeenCalled()
    expect(f.pushToast).not.toHaveBeenCalled()
  })

  it('never binds A after logout/login B and does not clear B when A settles', async () => {
    const f = fixture()
    const write = deferred<Record<string, unknown>>()
    f.upsert.mockReturnValueOnce(write.promise)
    f.mount()
    await settle()
    f.account.ready = false
    f.account.primaryActivation = null
    f.account.state = null
    await settle()
    f.account.state = {
      ...({ revision: 4, status: 'signed_in', keyStatus: 'ready', source: 'manual', selectedAccountId: null } as TokenRhythmAccountState),
      profile: { accountId: 'b', sessionId: 'session-b', displayName: 'B', maskedPhone: '' },
    }
    f.account.primaryActivation = intent('login-b', 'b')
    f.account.ready = true
    await settle()
    write.resolve({})
    await settle()
    expect(f.upsert).toHaveBeenCalledTimes(2)
    expect(f.account.selectSource).toHaveBeenCalledExactlyOnceWith('account', intent('login-b', 'b'))
    expect(f.account.state.selectedAccountId).toBe('b')
  })

  it('lets a manual choice supersede a pending readiness read', async () => {
    const f = fixture()
    const config = deferred<SettingsObject>()
    f.readAll.mockReturnValueOnce(config.promise)
    f.mount()
    await settle()
    await f.account.cancelPrimaryActivation()
    config.resolve({ llm: { provider: 'openrouter' } })
    await settle()
    expect(f.upsert).not.toHaveBeenCalled()
    expect(f.account.selectSource).not.toHaveBeenCalled()
  })

  it('does not continue on a changed Gateway delivery identity', async () => {
    const f = fixture()
    const write = deferred<Record<string, unknown>>()
    f.upsert.mockReturnValueOnce(write.promise)
    f.mount()
    await settle()
    f.gateway.deliveryIdentity = 'gateway-b'
    f.gateway.subscriptionEpoch += 1
    write.resolve({})
    await settle()
    expect(f.account.selectSource).not.toHaveBeenCalled()
    expect(f.upsert).toHaveBeenCalledOnce()
  })

  it('makes a cancelled router confirmation terminal', async () => {
    const f = fixture()
    f.upsert.mockRejectedValueOnce(conflict())
    f.mount()
    await settle()
    expect(confirmChoice).toHaveBeenCalledOnce()
    expect(f.account.primaryActivation).toBeNull()
    invalidateReadiness()
    await settle()
    expect(f.upsert).toHaveBeenCalledOnce()
    expect(f.account.selectSource).not.toHaveBeenCalled()
  })

  it('checks cancellation again after router confirmation before resubmitting', async () => {
    const f = fixture()
    const choice = deferred<'primary' | 'cancel'>()
    confirmChoice.mockImplementationOnce(() => choice.promise as Promise<'cancel'>)
    f.upsert.mockRejectedValueOnce(conflict())
    f.mount()
    await settle()
    await f.account.cancelPrimaryActivation()
    choice.resolve('primary')
    await settle()
    expect(f.upsert).toHaveBeenCalledOnce()
    expect(f.account.selectSource).not.toHaveBeenCalled()
  })

  it('reconciles uncertain writes by reads and never replays the mutation', async () => {
    const f = fixture()
    f.upsert.mockRejectedValueOnce(new Error('timeout'))
    f.mount()
    await settle()
    expect(f.readAll).toHaveBeenCalledTimes(2)
    expect(f.account.selectSource).not.toHaveBeenCalled()
    expect(f.pushToast).toHaveBeenCalledWith(expect.any(String), { tone: 'danger' })
    invalidateReadiness()
    await settle()
    expect(f.upsert).toHaveBeenCalledOnce()
  })

  it('warns on committed provider / binding or refresh failure without retrying', async () => {
    const f = fixture()
    f.account.selectSource.mockRejectedValueOnce(new Error('binding failed'))
    f.mount()
    await settle()
    expect(f.pushToast).toHaveBeenCalledWith(expect.any(String), { tone: 'warn' })
    expect(f.upsert).toHaveBeenCalledOnce()
    invalidateReadiness()
    await settle()
    expect(f.upsert).toHaveBeenCalledOnce()
  })
})
