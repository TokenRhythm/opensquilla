import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'

import type { MetaSetupJob, MetaSetupReadiness } from '@/types/metaSetup'
import type { HiddenControlDispatchResult } from '@/types/chat'
import {
  META_SETUP_PROVIDER_HANDOFF_TTL_MS,
  metaSetupLaunchStorageKey,
  metaSetupManualStorageKey,
  metaSetupStorageKey,
  useMetaSkillSetup,
  type MetaDraftDiscardOutcome,
  type MetaSetupStorage,
} from './useMetaSkillSetup'

const SESSION = 'agent:main:webchat:meta-setup'

function readiness(overrides: Partial<MetaSetupReadiness> = {}): MetaSetupReadiness {
  return {
    ready: false,
    status: 'needs_setup',
    missing_bins: ['xelatex', 'bibtex'],
    setup_actions: [{
      id: 'meta-paper-write:paper-toolchain',
      label: 'Install paper tools',
      bins: ['xelatex', 'bibtex'],
      available: true,
    }],
    ...overrides,
  }
}

function job(overrides: Partial<MetaSetupJob> = {}): MetaSetupJob {
  return {
    job_id: 'job-1',
    name: 'meta-paper-write',
    sessionKey: SESSION,
    action_ids: ['meta-paper-write:paper-toolchain'],
    status: 'running',
    phase: 'installing',
    message: 'Installing paper tools',
    current_action: 'meta-paper-write:paper-toolchain',
    completed_actions: [],
    readiness: null,
    ...overrides,
  }
}

function memoryStorage(initial: Record<string, string> = {}): MetaSetupStorage {
  const values = new Map(Object.entries(initial))
  return {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: key => values.delete(key),
  }
}

function harness(
  call: (method: string, params?: Record<string, unknown>) => Promise<unknown>,
  options: {
    storage?: MetaSetupStorage | null
    discardStorage?: MetaSetupStorage | null
    session?: string
    waitForConnection?: (timeoutMs?: number) => Promise<void>
    autoRestore?: boolean
    dispatchHidden?: (
      providerText: string,
      displayText: string,
      clientRequestId?: string,
    ) => HiddenControlDispatchResult | Promise<HiddenControlDispatchResult>
    restoreDraft?: (launchText: string, sessionKey: string) => void
    discardDraft?: (
      sessionKey: string,
      clientRequestId: string,
    ) => boolean | MetaDraftDiscardOutcome | Promise<boolean | MetaDraftDiscardOutcome>
    onDraftAlreadyAccepted?: (sessionKey: string, clientRequestId: string) => void
    forgetHiddenControl?: (sessionKey: string, clientRequestId: string) => void
  } = {},
) {
  const currentSessionKey = ref(options.session || SESSION)
  const setupStorage = options.storage === undefined ? memoryStorage() : options.storage
  const discardStorage = options.discardStorage === undefined
    ? setupStorage
    : options.discardStorage
  const dispatchHidden = vi.fn(options.dispatchHidden || (async (
    _providerText: string,
    _displayText: string,
    clientRequestId = '',
  ): Promise<HiddenControlDispatchResult> => ({
    status: 'accepted',
    reason: 'accepted',
    clientRequestId,
    sessionKey: currentSessionKey.value,
  })))
  const api = useMetaSkillSetup({
    rpc: {
      call: async <T = unknown>(method: string, params?: Record<string, unknown>) => (
        await call(method, params) as T
      ),
      waitForConnection: options.waitForConnection,
    },
    currentSessionKey,
    dispatchHidden,
    pollIntervalMs: 750,
    storage: setupStorage,
    discardStorage,
    autoRestore: options.autoRestore,
    restoreDraft: options.restoreDraft,
    discardDraft: options.discardDraft,
    onDraftAlreadyAccepted: options.onDraftAlreadyAccepted,
    forgetHiddenControl: options.forgetHiddenControl,
  })
  return { api, currentSessionKey, dispatchHidden }
}

async function flushPromises(): Promise<void> {
  for (let i = 0; i < 10; i += 1) await Promise.resolve()
}

beforeEach(() => {
  vi.useFakeTimers()
})

afterEach(() => {
  vi.clearAllTimers()
  vi.useRealTimers()
})

describe('useMetaSkillSetup', () => {
  it('confirms, reports real phases, verifies readiness, and resumes exactly once', async () => {
    let statusCalls = 0
    const call = vi.fn(async (method: string, _params?: Record<string, unknown>) => {
      if (method === 'meta.setup.install') return { job: job({ status: 'queued', phase: 'queued' }) }
      if (method === 'meta.setup.status') {
        statusCalls += 1
        if (statusCalls === 1) {
          return { job: job({ phase: 'verifying', message: 'Verifying installed capabilities' }) }
        }
        return {
          job: job({
            status: 'completed',
            phase: 'completed',
            message: 'Setup complete',
            completed_actions: ['meta-paper-write:paper-toolchain'],
            readiness: readiness({ ready: true, status: 'ready', missing_bins: [] }),
          }),
        }
      }
      if (method === 'meta.run') return { ok: true }
      throw new Error(`Unexpected RPC: ${method}`)
    })
    const storage = memoryStorage()
    const { api, dispatchHidden } = harness(call, { storage })

    const launchText = '/meta meta-paper-write -- Write a ten-page cited paper'
    await api.requestSetup('meta-paper-write', readiness(), SESSION, launchText)
    expect(api.setupState.value?.phase).toBe('confirm')

    await api.confirmSetup()
    expect(api.setupState.value?.phase).toBe('installing')
    expect(storage.getItem(metaSetupLaunchStorageKey(SESSION))).toBe(launchText)

    await vi.advanceTimersByTimeAsync(750)
    await flushPromises()
    expect(api.setupState.value?.phase).toBe('verifying')

    await vi.advanceTimersByTimeAsync(750)
    await flushPromises()

    expect(call).toHaveBeenCalledWith('meta.setup.install', {
      name: 'meta-paper-write',
      sessionKey: SESSION,
      confirmed: true,
      action_ids: ['meta-paper-write:paper-toolchain'],
    })
    expect(call).toHaveBeenCalledWith('meta.run', {
      name: 'meta-paper-write',
      sessionKey: SESSION,
      clientRequestId: expect.any(String),
      launchText,
    })
    expect(dispatchHidden).toHaveBeenCalledOnce()
    expect(dispatchHidden).toHaveBeenCalledWith(
      launchText,
      launchText,
      expect.any(String),
    )
    expect(storage.getItem(metaSetupStorageKey(SESSION))).toBeNull()
    expect(storage.getItem(metaSetupLaunchStorageKey(SESSION))).toBeNull()
    expect(storage.getItem(metaSetupManualStorageKey(SESSION))).toBeNull()
    expect(api.setupState.value).toBeNull()
    api.dispose()
  })

  it('hides a running setup without polling or dispatching', async () => {
    const storage = memoryStorage()
    const call = vi.fn(async (method: string, _params?: Record<string, unknown>) => {
      if (method === 'meta.setup.install') return { job: job() }
      if (method === 'meta.setup.status') return { job: job() }
      throw new Error(`Unexpected RPC: ${method}`)
    })
    const restoreDraft = vi.fn()
    const { api, dispatchHidden } = harness(call, { storage, restoreDraft })

    await api.requestSetup('meta-paper-write', readiness(), SESSION)
    await api.confirmSetup()
    expect(storage.getItem(metaSetupManualStorageKey(SESSION))).toContain('meta-paper-write')
    await api.cancelSetup()
    await vi.advanceTimersByTimeAsync(2000)

    expect(api.setupState.value).toBeNull()
    expect(call.mock.calls.filter(([method]) => method === 'meta.setup.status')).toHaveLength(0)
    expect(dispatchHidden).not.toHaveBeenCalled()
    expect(restoreDraft).not.toHaveBeenCalled()
    expect(storage.getItem(metaSetupStorageKey(SESSION))).toBe('job-1')
    expect(storage.getItem(metaSetupLaunchStorageKey(SESSION))).toBe('/meta meta-paper-write')
    expect(storage.getItem(metaSetupManualStorageKey(SESSION))).toBeNull()
    api.dispose()
  })

  it('returns the exact unlaunched request to the composer when setup is dismissed', async () => {
    const launchText = '/meta meta-short-drama -- Keep this exact five-shot request'
    const restoreDraft = vi.fn()
    const discardDraft = vi.fn(async () => true)
    const { api } = harness(vi.fn(), { restoreDraft, discardDraft })

    await api.requestSetup(
      'meta-short-drama',
      readiness(),
      SESSION,
      launchText,
      'dismissed-server-draft',
    )
    await api.cancelSetup()

    expect(restoreDraft).toHaveBeenCalledOnce()
    expect(restoreDraft).toHaveBeenCalledWith(launchText, SESSION)
    expect(discardDraft).toHaveBeenCalledWith(SESSION, 'dismissed-server-draft')
    expect(api.setupState.value).toBeNull()
    api.dispose()
  })

  it('preserves a second setup request while the first installation stays active', async () => {
    const storage = memoryStorage()
    const restoreDraft = vi.fn()
    const call = vi.fn(async (method: string) => {
      if (method === 'meta.setup.install') return { job: job() }
      throw new Error(`Unexpected RPC: ${method}`)
    })
    const { api } = harness(call, { storage, restoreDraft })

    await api.requestSetup(
      'meta-paper-write',
      readiness(),
      SESSION,
      '/meta meta-paper-write -- First request',
    )
    await api.confirmSetup()
    await api.requestSetup(
      'meta-short-drama',
      readiness({ missing_bins: ['ffmpeg'] }),
      SESSION,
      '/meta meta-short-drama -- Second request',
    )

    expect(api.setupState.value?.name).toBe('meta-paper-write')
    expect(api.setupState.value?.phase).toBe('installing')
    expect(restoreDraft).not.toHaveBeenCalled()
    api.dispose()
  })

  it('keeps the stable retry card when server discard cannot be confirmed', async () => {
    const launchText = '/meta meta-short-drama -- Do not duplicate this request'
    const restoreDraft = vi.fn()
    const discardDraft = vi.fn(async () => false)
    const { api } = harness(vi.fn(), { restoreDraft, discardDraft })

    await api.requestSetup(
      'meta-short-drama',
      readiness(),
      SESSION,
      launchText,
      'not-discarded-server-draft',
    )
    await api.cancelSetup()

    expect(restoreDraft).not.toHaveBeenCalled()
    expect(api.setupState.value).toMatchObject({
      phase: 'failed',
      retryMode: 'discard',
      resumeRequestId: 'not-discarded-server-draft',
      launchText,
    })
    api.dispose()
  })

  it('does not restore a request that crossed the accepted launch boundary', async () => {
    const storage = memoryStorage()
    const launchText = '/meta meta-short-drama -- This accepted run must not duplicate'
    const restoreDraft = vi.fn()
    const discardDraft = vi.fn(async () => 'accepted' as const)
    const onDraftAlreadyAccepted = vi.fn()
    const { api } = harness(vi.fn(), {
      storage,
      restoreDraft,
      discardDraft,
      onDraftAlreadyAccepted,
    })

    await api.requestSetup(
      'meta-short-drama',
      readiness(),
      SESSION,
      launchText,
      'accepted-before-cancel',
    )
    await api.cancelSetup()

    expect(discardDraft).toHaveBeenCalledWith(SESSION, 'accepted-before-cancel')
    expect(restoreDraft).not.toHaveBeenCalled()
    expect(onDraftAlreadyAccepted).toHaveBeenCalledWith(
      SESSION,
      'accepted-before-cancel',
    )
    expect(api.setupState.value).toBeNull()
    expect(storage.getItem(metaSetupStorageKey(SESSION))).toBeNull()
    expect(storage.getItem(metaSetupLaunchStorageKey(SESSION))).toBeNull()
    expect(storage.getItem(metaSetupManualStorageKey(SESSION))).toBeNull()
    api.dispose()
  })

  it('retries a lost cancel response after reload without launching', async () => {
    const storage = memoryStorage()
    const launchText = '/meta meta-short-drama -- Cancel this paid request'
    const first = harness(vi.fn(), {
      storage,
      discardDraft: vi.fn(async () => { throw new Error('response lost') }),
    })
    await first.api.requestSetup(
      'meta-short-drama',
      readiness(),
      SESSION,
      launchText,
      'cancel-response-lost',
    )
    await first.api.cancelSetup()
    expect(first.api.setupState.value?.retryMode).toBe('discard')
    first.api.dispose()

    const restoreDraft = vi.fn()
    const discardDraft = vi.fn(async () => true)
    const call = vi.fn(async () => ({ ok: true }))
    const second = harness(call, { storage, restoreDraft, discardDraft })
    await flushPromises()

    expect(discardDraft).toHaveBeenCalledWith(SESSION, 'cancel-response-lost')
    expect(call).not.toHaveBeenCalled()
    expect(second.dispatchHidden).not.toHaveBeenCalled()
    expect(restoreDraft).toHaveBeenCalledWith(launchText, SESSION)
    expect(second.api.setupState.value).toBeNull()
    second.api.dispose()
  })

  it('preserves a new stable id through missing persisted-job recovery', async () => {
    const storage = memoryStorage({
      [metaSetupStorageKey(SESSION)]: 'missing-job',
    })
    const call = vi.fn(async (method: string) => {
      if (method === 'meta.setup.status') return { ok: false, error: 'setup job not found' }
      throw new Error(`Unexpected RPC: ${method}`)
    })
    const { api } = harness(call, { storage, autoRestore: false })

    await api.requestSetup(
      'meta-paper-write',
      readiness(),
      SESSION,
      '/meta meta-paper-write -- preserve this identity',
      'stable-after-missing-job',
    )

    expect(api.setupState.value?.resumeRequestId).toBe('stable-after-missing-job')
    expect(storage.getItem(metaSetupManualStorageKey(SESSION)))
      .toContain('stable-after-missing-job')
    api.dispose()
  })

  it('keeps a new durable request deferred behind a different persisted job', async () => {
    const oldLaunch = '/meta meta-paper-write -- incumbent paper'
    const storage = memoryStorage({
      [metaSetupStorageKey(SESSION)]: 'incumbent-job',
      [metaSetupLaunchStorageKey(SESSION)]: oldLaunch,
    })
    const call = vi.fn(async (method: string) => {
      if (method === 'meta.setup.status') {
        return { job: job({ job_id: 'incumbent-job' }) }
      }
      throw new Error(`Unexpected RPC: ${method}`)
    })
    const { api, dispatchHidden } = harness(call, { storage, autoRestore: false })

    const disposition = await api.requestSetup(
      'meta-short-drama',
      readiness({ missing_bins: ['ffmpeg'] }),
      SESSION,
      '/meta meta-short-drama -- independent request',
      'independent-request-id',
    )

    expect(disposition).toBe('deferred')
    expect(api.setupState.value?.name).toBe('meta-paper-write')
    expect(api.setupState.value?.launchText).toBe(oldLaunch)
    expect(api.setupState.value?.resumeRequestId).toBeUndefined()
    expect(dispatchHidden).not.toHaveBeenCalled()
    api.dispose()
  })

  it('preserves a stable id while restoring the matching persisted job', async () => {
    const launchText = '/meta meta-paper-write -- matching persisted setup'
    const storage = memoryStorage({
      [metaSetupStorageKey(SESSION)]: 'matching-job',
      [metaSetupLaunchStorageKey(SESSION)]: launchText,
    })
    const call = vi.fn(async (method: string) => {
      if (method === 'meta.setup.status') return { job: job({ job_id: 'matching-job' }) }
      throw new Error(`Unexpected RPC: ${method}`)
    })
    const { api } = harness(call, { storage, autoRestore: false })

    await api.requestSetup(
      'meta-paper-write',
      readiness(),
      SESSION,
      launchText,
      'matching-stable-id',
    )

    expect(api.setupState.value?.resumeRequestId).toBe('matching-stable-id')
    expect(storage.getItem(metaSetupManualStorageKey(SESSION)))
      .toContain('matching-stable-id')
    api.dispose()
  })

  it('retains the resume identity when readiness is still not ready', async () => {
    const storage = memoryStorage()
    const clientRequestId = 'still-not-ready-request'
    const call = vi.fn(async (method: string) => {
      if (method === 'meta.setup.plan') {
        return {
          ok: true,
          readiness: readiness({
            missing_bins: [],
            missing_env: ['OPENROUTER_API_KEY'],
            setup_actions: [],
          }),
        }
      }
      throw new Error(`Unexpected RPC: ${method}`)
    })
    const { api } = harness(call, { storage })
    await api.requestSetup(
      'meta-short-drama',
      readiness({
        missing_bins: [],
        missing_env: ['OPENROUTER_API_KEY'],
        setup_actions: [],
      }),
      SESSION,
      '/meta meta-short-drama -- Preserve my id',
      clientRequestId,
    )

    await api.retrySetup()

    expect(api.setupState.value?.resumeRequestId).toBe(clientRequestId)
    expect(storage.getItem(metaSetupManualStorageKey(SESSION))).toContain(clientRequestId)
    api.dispose()
  })

  it('persists delayed setup readiness under the originating session', async () => {
    const storage = memoryStorage()
    const { api, currentSessionKey } = harness(vi.fn(), { storage })
    const launchText = '/meta meta-paper-write -- Return to this request'
    currentSessionKey.value = 'agent:main:another-chat'

    await api.requestSetup('meta-paper-write', readiness(), SESSION, launchText)

    expect(api.setupState.value).toBeNull()
    expect(storage.getItem(metaSetupManualStorageKey(SESSION))).toContain(launchText)
    api.dispose()
  })

  it('restores the stable checkpoint when an accepted job disappears after Gateway restart', async () => {
    const launchText = '/meta meta-paper-write -- Keep this paper request through restart'
    const storage = memoryStorage()
    const call = vi.fn(async (method: string) => {
      if (method === 'meta.setup.install') return { job: job() }
      if (method === 'meta.setup.status') throw new Error('meta setup job not found (404)')
      throw new Error(`Unexpected RPC: ${method}`)
    })
    const { api, dispatchHidden } = harness(call, { storage })

    await api.requestSetup('meta-paper-write', readiness(), SESSION, launchText)
    await api.confirmSetup()
    expect(storage.getItem(metaSetupStorageKey(SESSION))).toBe('job-1')
    expect(storage.getItem(metaSetupManualStorageKey(SESSION))).toContain(launchText)

    await vi.advanceTimersByTimeAsync(750)
    await flushPromises()

    expect(api.setupState.value?.phase).toBe('confirm')
    expect(api.setupState.value?.name).toBe('meta-paper-write')
    expect(api.setupState.value?.launchText).toBe(launchText)
    expect(api.setupState.value?.actionIds).toEqual(['meta-paper-write:paper-toolchain'])
    expect(storage.getItem(metaSetupStorageKey(SESSION))).toBeNull()
    expect(storage.getItem(metaSetupLaunchStorageKey(SESSION))).toBe(launchText)
    expect(storage.getItem(metaSetupManualStorageKey(SESSION))).toContain(launchText)
    expect(dispatchHidden).not.toHaveBeenCalled()
    api.dispose()
  })

  it('restores the original confirm card after remounting against a restarted Gateway', async () => {
    const launchText = '/meta meta-paper-write -- Resume this exact request after remount'
    const storage = memoryStorage()
    const first = harness(vi.fn(async (method: string) => {
      if (method === 'meta.setup.install') return { job: job() }
      throw new Error(`Unexpected RPC: ${method}`)
    }), { storage })

    await first.api.requestSetup('meta-paper-write', readiness(), SESSION, launchText)
    await first.api.confirmSetup()
    expect(storage.getItem(metaSetupStorageKey(SESSION))).toBe('job-1')
    expect(storage.getItem(metaSetupManualStorageKey(SESSION))).toContain(launchText)
    first.api.dispose()

    const secondCall = vi.fn(async (method: string) => {
      if (method === 'meta.setup.status') {
        return { ok: false, error: 'meta setup job not found' }
      }
      throw new Error(`Unexpected RPC: ${method}`)
    })
    const second = harness(secondCall, { storage })
    await flushPromises()

    expect(second.api.setupState.value?.phase).toBe('confirm')
    expect(second.api.setupState.value?.name).toBe('meta-paper-write')
    expect(second.api.setupState.value?.launchText).toBe(launchText)
    expect(second.api.setupState.value?.readiness.missing_bins).toEqual(['xelatex', 'bibtex'])
    expect(storage.getItem(metaSetupStorageKey(SESSION))).toBeNull()
    expect(storage.getItem(metaSetupLaunchStorageKey(SESSION))).toBe(launchText)
    expect(storage.getItem(metaSetupManualStorageKey(SESSION))).toContain(launchText)
    second.api.dispose()
  })

  it('recovers an old job-plus-launch record as a recheck card when the job is unknown', async () => {
    const launchText = '/meta meta-short-drama -- Restore this legacy request'
    const storage = memoryStorage({
      [metaSetupStorageKey(SESSION)]: 'legacy-job',
      [metaSetupLaunchStorageKey(SESSION)]: launchText,
    })
    const call = vi.fn(async (method: string) => {
      if (method === 'meta.setup.status') {
        return { ok: false, error: 'Unknown setup job' }
      }
      throw new Error(`Unexpected RPC: ${method}`)
    })

    const { api, dispatchHidden } = harness(call, { storage })
    await flushPromises()

    expect(api.setupState.value?.phase).toBe('blocked')
    expect(api.setupState.value?.retryMode).toBe('readiness')
    expect(api.setupState.value?.name).toBe('meta-short-drama')
    expect(api.setupState.value?.launchText).toBe(launchText)
    expect(storage.getItem(metaSetupStorageKey(SESSION))).toBeNull()
    expect(storage.getItem(metaSetupLaunchStorageKey(SESSION))).toBe(launchText)
    expect(storage.getItem(metaSetupManualStorageKey(SESSION))).toContain(launchText)
    expect(dispatchHidden).not.toHaveBeenCalled()
    api.dispose()
  })

  it('retains the checkpoint on launch failure and clears it only when the user cancels', async () => {
    const launchText = '/meta meta-paper-write -- Do not lose this failed launch'
    const storage = memoryStorage()
    const call = vi.fn(async (method: string) => {
      if (method === 'meta.setup.install') {
        return {
          job: job({
            status: 'completed',
            phase: 'completed',
            readiness: readiness({ ready: true, status: 'ready', missing_bins: [] }),
          }),
        }
      }
      if (method === 'meta.run') return { ok: false, error: 'Provider is temporarily unavailable' }
      throw new Error(`Unexpected RPC: ${method}`)
    })
    const discardDraft = vi.fn(async () => true)
    const { api, dispatchHidden } = harness(call, { storage, discardDraft })

    await api.requestSetup('meta-paper-write', readiness(), SESSION, launchText)
    await api.confirmSetup()

    expect(api.setupState.value?.phase).toBe('failed')
    expect(api.setupState.value?.retryMode).toBe('launch')
    expect(storage.getItem(metaSetupStorageKey(SESSION))).toBe('job-1')
    expect(storage.getItem(metaSetupLaunchStorageKey(SESSION))).toBe(launchText)
    expect(storage.getItem(metaSetupManualStorageKey(SESSION))).toContain(launchText)
    expect(dispatchHidden).not.toHaveBeenCalled()

    await api.cancelSetup()
    expect(discardDraft).toHaveBeenCalledWith(SESSION, expect.any(String))
    expect(storage.getItem(metaSetupStorageKey(SESSION))).toBeNull()
    expect(storage.getItem(metaSetupLaunchStorageKey(SESSION))).toBeNull()
    expect(storage.getItem(metaSetupManualStorageKey(SESSION))).toBeNull()
    api.dispose()
  })

  it('surfaces a hidden background job instead of replacing it with a second setup', async () => {
    const storage = memoryStorage()
    const call = vi.fn(async (method: string) => {
      if (method === 'meta.setup.install') return { job: job() }
      if (method === 'meta.setup.status') return { job: job() }
      throw new Error(`Unexpected RPC: ${method}`)
    })
    const { api } = harness(call, { storage })

    await api.requestSetup('meta-paper-write', readiness(), SESSION)
    await api.confirmSetup()
    api.cancelSetup()
    await api.requestSetup('meta-video-render', readiness({ missing_bins: ['ffmpeg'] }), SESSION)

    expect(api.setupState.value?.name).toBe('meta-paper-write')
    expect(api.setupState.value?.phase).toBe('installing')
    expect(api.setupState.value?.jobId).toBe('job-1')
    expect(storage.getItem(metaSetupStorageKey(SESSION))).toBe('job-1')
    expect(call.mock.calls.filter(([method]) => method === 'meta.setup.install')).toHaveLength(1)
    api.dispose()
  })

  it('keeps polling a visible background setup when another setup is requested', async () => {
    const call = vi.fn(async (method: string) => {
      if (method === 'meta.setup.install') return { job: job() }
      if (method === 'meta.setup.status') return { job: job() }
      throw new Error(`Unexpected RPC: ${method}`)
    })
    const { api } = harness(call)

    await api.requestSetup('meta-paper-write', readiness(), SESSION)
    await api.confirmSetup()
    await api.requestSetup('meta-video-render', readiness({ missing_bins: ['ffmpeg'] }), SESSION)
    await vi.advanceTimersByTimeAsync(750)
    await flushPromises()

    expect(api.setupState.value?.name).toBe('meta-paper-write')
    expect(api.setupState.value?.phase).toBe('installing')
    expect(call.mock.calls.filter(([method]) => method === 'meta.setup.status')).toHaveLength(1)
    api.dispose()
  })

  it('retries a failed install and skips actions that already completed', async () => {
    let installCalls = 0
    let statusCalls = 0
    const call = vi.fn(async (method: string, params?: Record<string, unknown>) => {
      if (method === 'meta.setup.install') {
        installCalls += 1
        if (installCalls === 2) {
          expect(params?.action_ids).toEqual(['child:media-tools'])
          return {
            job: job({
              job_id: 'job-2',
              action_ids: ['child:media-tools'],
              current_action: 'child:media-tools',
            }),
          }
        }
        return {
          job: job({
            action_ids: ['parent:paper-tools', 'child:media-tools'],
            current_action: 'parent:paper-tools',
          }),
        }
      }
      if (method === 'meta.setup.status') {
        statusCalls += 1
        if (statusCalls === 1) {
          return {
            job: job({
              status: 'failed',
              phase: 'failed',
              action_ids: ['parent:paper-tools', 'child:media-tools'],
              completed_actions: ['parent:paper-tools'],
              error: 'Network interrupted',
            }),
          }
        }
        return {
          job: job({
            job_id: 'job-2',
            status: 'completed',
            phase: 'completed',
            action_ids: ['child:media-tools'],
            completed_actions: ['child:media-tools'],
            readiness: readiness({ ready: true, status: 'ready', missing_bins: [] }),
          }),
        }
      }
      if (method === 'meta.run') return { ok: true }
      throw new Error(`Unexpected RPC: ${method}`)
    })
    const setupReadiness = readiness({
      setup_actions: [
        { id: 'parent:paper-tools', available: true },
        { id: 'child:media-tools', available: true },
      ],
    })
    const { api, dispatchHidden } = harness(call)

    await api.requestSetup('meta-paper-write', setupReadiness, SESSION)
    await api.confirmSetup()
    await vi.advanceTimersByTimeAsync(750)
    await flushPromises()

    expect(api.setupState.value?.phase).toBe('failed')
    expect(api.setupState.value?.actionIds).toEqual(['child:media-tools'])

    await api.retrySetup()
    await vi.advanceTimersByTimeAsync(750)
    await flushPromises()

    expect(installCalls).toBe(2)
    expect(dispatchHidden).toHaveBeenCalledOnce()
    api.dispose()
  })

  it('blocks without starting an install when no automatic action is available', async () => {
    const call = vi.fn(async () => ({ ok: true }))
    const { api, dispatchHidden } = harness(call)

    await api.requestSetup('meta-paper-write', readiness({
      setup_actions: [{
        id: 'paper:unsupported',
        available: false,
        reason: 'Unavailable on this platform',
      }],
    }), SESSION)
    await api.confirmSetup()

    expect(api.setupState.value?.phase).toBe('blocked')
    expect(api.setupState.value?.blockedReason).toBe('no_actions')
    expect(api.setupState.value?.retryMode).toBe('readiness')
    expect(call).not.toHaveBeenCalled()
    expect(dispatchHidden).not.toHaveBeenCalled()
    api.dispose()
  })

  it('rechecks a manual requirement and launches the original request when it becomes ready', async () => {
    const launchText = '/meta meta-short-drama -- Create a three-scene product launch drama'
    const call = vi.fn(async (method: string) => {
      if (method === 'meta.setup.plan') {
        return {
          ok: true,
          readiness: readiness({
            ready: true,
            status: 'ready',
            missing_bins: [],
            missing_env: [],
            setup_actions: [],
          }),
        }
      }
      if (method === 'meta.run') return { ok: true }
      throw new Error(`Unexpected RPC: ${method}`)
    })
    const storage = memoryStorage()
    const { api, dispatchHidden } = harness(call, { storage })

    await api.requestSetup('meta-short-drama', readiness({
      missing_bins: [],
      missing_env: ['OPENROUTER_API_KEY'],
      reasons: ['OPENROUTER_API_KEY is required'],
      setup_actions: [],
    }), SESSION, launchText)
    expect(api.setupState.value?.phase).toBe('blocked')
    expect(api.setupState.value?.retryMode).toBe('readiness')
    expect(storage.getItem(metaSetupManualStorageKey(SESSION))).toContain('meta-short-drama')

    await api.retrySetup()

    expect(call.mock.calls.map(([method]) => method)).toEqual(['meta.setup.plan', 'meta.run'])
    expect(dispatchHidden).toHaveBeenCalledOnce()
    expect(dispatchHidden).toHaveBeenCalledWith(launchText, launchText, expect.any(String))
    expect(api.setupState.value).toBeNull()
    expect(storage.getItem(metaSetupManualStorageKey(SESSION))).toBeNull()
    api.dispose()
  })

  it('restores a manual recovery card after the settings route remounts chat', async () => {
    const launchText = '/meta meta-short-drama -- Preserve this launch after settings'
    const storage = memoryStorage()
    const first = harness(vi.fn(async () => ({ ok: true })), { storage })
    await first.api.requestSetup('meta-short-drama', readiness({
      missing_bins: [],
      missing_env: ['OPENROUTER_API_KEY'],
      setup_actions: [],
    }), SESSION, launchText)
    first.api.dispose()

    const secondCall = vi.fn(async () => ({ ok: true }))
    const second = harness(secondCall, { storage })
    await flushPromises()

    expect(second.api.setupState.value?.phase).toBe('blocked')
    expect(second.api.setupState.value?.name).toBe('meta-short-drama')
    expect(second.api.setupState.value?.launchText).toBe(launchText)
    expect(second.api.setupState.value?.retryMode).toBe('readiness')
    expect(secondCall).not.toHaveBeenCalled()

    second.api.cancelSetup()
    expect(storage.getItem(metaSetupManualStorageKey(SESSION))).toBeNull()
    second.api.dispose()
  })

  it('consumes a provider handoff once and resumes the exact original launch', async () => {
    const launchText = '/meta meta-short-drama -- Keep this request while I add my API key'
    const combinedReadiness = readiness({
      missing_bins: ['ffmpeg', 'ffprobe'],
      missing_env: ['OPENROUTER_API_KEY'],
      setup_actions: [{
        id: 'meta-short-drama:media-ffmpeg',
        install_id: 'media-ffmpeg',
        available: true,
      }],
      manual_setup_actions: [{
        id: 'provider:openrouter',
        kind: 'provider_connection',
        provider_id: 'openrouter',
        label: 'OpenRouter',
        capability_ids: ['image.generate', 'video.generate'],
        available: true,
      }],
    })
    const storage = memoryStorage()
    const first = harness(vi.fn(async () => ({ ok: true })), { storage })

    await first.api.requestSetup('meta-short-drama', combinedReadiness, SESSION, launchText)
    expect(first.api.setupState.value?.phase).toBe('confirm')
    const clientRequestId = first.api.beginProviderHandoff('openrouter')
    expect(clientRequestId).toEqual(expect.any(String))
    const handoffCheckpoint = JSON.parse(
      storage.getItem(metaSetupManualStorageKey(SESSION)) || '{}',
    ) as Record<string, unknown>
    expect(handoffCheckpoint.providerHandoff).toEqual({
      kind: 'provider_settings',
      providerId: 'openrouter',
      startedAtMs: expect.any(Number),
      clientRequestId,
    })

    // Opening provider settings unmounts ChatView and its setup composable.
    first.api.dispose()
    const secondCall = vi.fn(async (method: string) => {
      if (method === 'meta.run') return { ok: true }
      throw new Error(`Unexpected RPC: ${method}`)
    })
    const second = harness(secondCall, { storage })
    await flushPromises()

    expect(secondCall.mock.calls.map(([method]) => method)).toEqual(['meta.run'])
    expect(secondCall).toHaveBeenCalledWith('meta.run', {
      name: 'meta-short-drama',
      sessionKey: SESSION,
      clientRequestId,
      launchText,
    })
    expect(second.dispatchHidden).toHaveBeenCalledOnce()
    expect(second.dispatchHidden).toHaveBeenCalledWith(
      launchText,
      launchText,
      clientRequestId,
    )
    expect(second.api.setupState.value).toBeNull()
    expect(storage.getItem(metaSetupManualStorageKey(SESSION))).toBeNull()
    second.api.dispose()

    const thirdCall = vi.fn(async () => ({ ok: true }))
    const third = harness(thirdCall, { storage })
    await flushPromises()
    expect(thirdCall).not.toHaveBeenCalled()
    expect(third.dispatchHidden).not.toHaveBeenCalled()
    third.api.dispose()
  })

  it('consumes a setup checkpoint cancelled by another tab without resurrecting it', async () => {
    const launchText = '/meta meta-short-drama -- cancelled while provider settings were open'
    const storage = memoryStorage()
    const providerReadiness = readiness({
      missing_bins: [],
      setup_actions: [],
      manual_setup_actions: [{
        id: 'provider:openrouter',
        kind: 'provider_connection',
        provider_id: 'openrouter',
        available: true,
      }],
    })
    const first = harness(vi.fn(async () => ({ ok: true })), { storage })
    await first.api.requestSetup('meta-short-drama', providerReadiness, SESSION, launchText)
    const clientRequestId = first.api.beginProviderHandoff('openrouter')
    first.api.dispose()

    const discarded = Object.assign(new Error('The saved request was already discarded.'), {
      code: 'META_DRAFT_DISCARDED',
      retryable: false,
      accepted: false,
    })
    const restoreDraft = vi.fn()
    const forgetHiddenControl = vi.fn()
    const call = vi.fn(async (method: string) => {
      if (method === 'meta.setup.plan') {
        return {
          ok: true,
          readiness: readiness({
            ready: true,
            status: 'ready',
            missing_bins: [],
            setup_actions: [],
            manual_setup_actions: [],
          }),
        }
      }
      if (method === 'meta.run') throw discarded
      throw new Error(`Unexpected RPC: ${method}`)
    })
    const second = harness(call, { storage, restoreDraft, forgetHiddenControl })
    await flushPromises()

    expect(call.mock.calls.map(([method]) => method)).toEqual(['meta.run'])
    expect(call).toHaveBeenCalledWith('meta.run', {
      name: 'meta-short-drama',
      sessionKey: SESSION,
      clientRequestId,
      launchText,
    })
    expect(second.dispatchHidden).not.toHaveBeenCalled()
    expect(restoreDraft).not.toHaveBeenCalled()
    expect(forgetHiddenControl).toHaveBeenCalledWith(SESSION, clientRequestId)
    expect(second.api.setupState.value).toBeNull()
    expect(storage.getItem(metaSetupManualStorageKey(SESSION))).toBeNull()
    second.api.dispose()

    const thirdCall = vi.fn(async () => ({ ok: true }))
    const third = harness(thirdCall, { storage })
    await flushPromises()
    expect(thirdCall).not.toHaveBeenCalled()
    third.api.dispose()
  })

  it('keeps the drafted request identity when install completion remains provider-blocked', async () => {
    const launchText = '/meta meta-short-drama -- Keep one paid launch identity'
    const clientRequestId = 'stable-provider-after-install'
    const providerAction = {
      id: 'provider:openrouter',
      kind: 'provider_connection',
      provider_id: 'openrouter',
      available: true,
    }
    const storage = memoryStorage()
    const call = vi.fn(async (method: string) => {
      if (method === 'meta.setup.install') {
        return {
          job: job({
            name: 'meta-short-drama',
            action_ids: ['meta-short-drama:media-ffmpeg'],
          }),
        }
      }
      if (method === 'meta.setup.status') {
        return {
          job: job({
            name: 'meta-short-drama',
            status: 'blocked',
            phase: 'blocked',
            action_ids: ['meta-short-drama:media-ffmpeg'],
            completed_actions: ['meta-short-drama:media-ffmpeg'],
            readiness: readiness({
              missing_bins: [],
              missing_env: ['OPENROUTER_API_KEY'],
              setup_actions: [],
              manual_setup_actions: [providerAction],
            }),
          }),
        }
      }
      throw new Error(`Unexpected RPC: ${method}`)
    })
    const { api } = harness(call, { storage })

    await api.requestSetup(
      'meta-short-drama',
      readiness({
        setup_actions: [{
          id: 'meta-short-drama:media-ffmpeg',
          install_id: 'media-ffmpeg',
          available: true,
        }],
        manual_setup_actions: [providerAction],
      }),
      SESSION,
      launchText,
      clientRequestId,
    )
    await api.confirmSetup()
    await vi.advanceTimersByTimeAsync(750)
    await flushPromises()

    expect(api.setupState.value?.phase).toBe('confirm')
    expect(api.setupState.value?.resumeRequestId).toBe(clientRequestId)
    expect(api.beginProviderHandoff('openrouter')).toBe(clientRequestId)
    expect(api.setupState.value?.providerHandoff?.clientRequestId).toBe(clientRequestId)
    expect(storage.getItem(metaSetupManualStorageKey(SESSION))).toContain(clientRequestId)
    api.dispose()
  })

  it('keeps the drafted request identity when launch readiness changes after recheck', async () => {
    const launchText = '/meta meta-short-drama -- Survive a provider readiness race'
    const clientRequestId = 'stable-provider-readiness-race'
    const providerAction = {
      id: 'provider:openrouter',
      kind: 'provider_connection',
      provider_id: 'openrouter',
      available: true,
    }
    const storage = memoryStorage()
    const call = vi.fn(async (method: string) => {
      if (method === 'meta.setup.plan') {
        return {
          ok: true,
          readiness: readiness({
            ready: true,
            status: 'ready',
            missing_bins: [],
            missing_env: [],
            setup_actions: [],
            manual_setup_actions: [],
          }),
        }
      }
      if (method === 'meta.run') {
        return {
          ok: false,
          setup_required: true,
          error: 'Provider configuration changed; review the requirement again.',
          readiness: readiness({
            missing_bins: [],
            missing_env: ['OPENROUTER_API_KEY'],
            setup_actions: [],
            manual_setup_actions: [providerAction],
          }),
        }
      }
      throw new Error(`Unexpected RPC: ${method}`)
    })
    const { api, dispatchHidden } = harness(call, { storage })

    await api.requestSetup(
      'meta-short-drama',
      readiness({
        missing_bins: [],
        missing_env: ['OPENROUTER_API_KEY'],
        setup_actions: [],
        manual_setup_actions: [providerAction],
      }),
      SESSION,
      launchText,
      clientRequestId,
    )
    await api.retrySetup()

    expect(api.setupState.value?.phase).toBe('confirm')
    expect(api.setupState.value?.resumeRequestId).toBe(clientRequestId)
    expect(api.beginProviderHandoff('openrouter')).toBe(clientRequestId)
    expect(api.setupState.value?.providerHandoff?.clientRequestId).toBe(clientRequestId)
    expect(storage.getItem(metaSetupManualStorageKey(SESSION))).toContain(clientRequestId)
    expect(dispatchHidden).not.toHaveBeenCalled()
    api.dispose()
  })

  it('keeps a queued provider resume durable across a full composable remount', async () => {
    const launchText = '/meta meta-short-drama -- Resume after refresh without duplicating'
    const providerReadiness = readiness({
      missing_bins: [],
      setup_actions: [],
      manual_setup_actions: [{
        id: 'provider:openrouter',
        kind: 'provider_connection',
        provider_id: 'openrouter',
        available: true,
      }],
    })
    const readyReadiness = readiness({
      ready: true,
      status: 'ready',
      missing_bins: [],
      setup_actions: [],
      manual_setup_actions: [],
    })
    const storage = memoryStorage()
    const first = harness(vi.fn(async () => ({ ok: true })), { storage })
    await first.api.requestSetup('meta-short-drama', providerReadiness, SESSION, launchText)
    const clientRequestId = first.api.beginProviderHandoff('openrouter')
    first.api.dispose()

    const call = vi.fn(async (method: string, _params?: Record<string, unknown>) => {
      if (method === 'meta.setup.plan') return { ok: true, readiness: readyReadiness }
      if (method === 'meta.run') return { ok: true }
      throw new Error(`Unexpected RPC: ${method}`)
    })
    const queued = harness(call, {
      storage,
      dispatchHidden: async (_providerText, _displayText, requestId = '') => ({
        status: 'queued',
        reason: 'queued',
        clientRequestId: requestId,
        sessionKey: SESSION,
      }),
    })
    await flushPromises()

    expect(queued.api.setupState.value?.phase).toBe('verifying')
    expect(queued.api.setupState.value?.resumeRequestId).toBe(clientRequestId)
    expect(storage.getItem(metaSetupManualStorageKey(SESSION))).toContain(clientRequestId)
    queued.api.dispose()

    const accepted = harness(call, { storage })
    await flushPromises()

    const runRequestIds = call.mock.calls
      .filter(([method]) => method === 'meta.run')
      .map(([, params]) => params?.clientRequestId)
    expect(runRequestIds).toEqual([clientRequestId, clientRequestId])
    expect(accepted.dispatchHidden).toHaveBeenCalledWith(
      launchText,
      launchText,
      clientRequestId,
    )
    expect(accepted.api.setupState.value).toBeNull()
    expect(storage.getItem(metaSetupManualStorageKey(SESSION))).toBeNull()
    accepted.api.dispose()
  })

  it.each([
    { status: 'rejected' as const, reason: 'queue_full' as const },
    { status: 'unknown' as const, reason: 'response_unknown' as const },
  ])('retries a $reason dispatch with the exact same ingress id', async (failure) => {
    const launchText = '/meta meta-short-drama -- Keep this request after dispatch failure'
    const storage = memoryStorage()
    const call = vi.fn(async (method: string, _params?: Record<string, unknown>) => {
      if (method === 'meta.setup.plan') return { ok: true, readiness: readiness({
        ready: true,
        status: 'ready',
        missing_bins: [],
        setup_actions: [],
      }) }
      if (method === 'meta.run') return { ok: true }
      throw new Error(`Unexpected RPC: ${method}`)
    })
    let dispatchCount = 0
    const instance = harness(call, {
      storage,
      dispatchHidden: async (_providerText, _displayText, clientRequestId = '') => {
        dispatchCount += 1
        return dispatchCount === 1
          ? { ...failure, clientRequestId, sessionKey: SESSION }
          : {
              status: 'accepted' as const,
              reason: 'accepted' as const,
              clientRequestId,
              sessionKey: SESSION,
            }
      },
    })
    await instance.api.requestSetup('meta-short-drama', readiness({
      missing_bins: [],
      missing_env: ['OPENROUTER_API_KEY'],
      setup_actions: [],
    }), SESSION, launchText)

    await instance.api.retrySetup()
    const firstRequestId = instance.dispatchHidden.mock.calls[0]?.[2]
    expect(firstRequestId).toEqual(expect.any(String))
    expect(instance.api.setupState.value?.phase).toBe('failed')
    expect(instance.api.setupState.value?.retryMode).toBe('launch')
    expect(storage.getItem(metaSetupManualStorageKey(SESSION))).toContain(firstRequestId)

    await instance.api.retrySetup()

    expect(instance.dispatchHidden.mock.calls.map((args) => args[2]))
      .toEqual([firstRequestId, firstRequestId])
    expect(call.mock.calls
      .filter(([method]) => method === 'meta.run')
      .map(([, params]) => params?.clientRequestId))
      .toEqual([firstRequestId, firstRequestId])
    expect(instance.api.setupState.value).toBeNull()
    expect(storage.getItem(metaSetupManualStorageKey(SESSION))).toBeNull()
    instance.api.dispose()
  })

  it('clears a queued resume only after the pending queue reports acceptance', async () => {
    const storage = memoryStorage()
    const call = vi.fn(async (method: string) => {
      if (method === 'meta.setup.plan') return { ok: true, readiness: readiness({
        ready: true,
        status: 'ready',
        missing_bins: [],
        setup_actions: [],
      }) }
      if (method === 'meta.run') return { ok: true }
      throw new Error(`Unexpected RPC: ${method}`)
    })
    const instance = harness(call, {
      storage,
      dispatchHidden: async (_providerText, _displayText, clientRequestId = '') => ({
        status: 'queued',
        reason: 'queued',
        clientRequestId,
        sessionKey: SESSION,
      }),
    })
    await instance.api.requestSetup('meta-short-drama', readiness({
      missing_bins: [],
      missing_env: ['OPENROUTER_API_KEY'],
      setup_actions: [],
    }), SESSION)
    await instance.api.retrySetup()
    const clientRequestId = instance.dispatchHidden.mock.calls[0]?.[2] || ''

    expect(instance.api.setupState.value?.phase).toBe('verifying')
    expect(storage.getItem(metaSetupManualStorageKey(SESSION))).toContain(clientRequestId)

    instance.api.handleHiddenDispatchResult({
      status: 'accepted',
      reason: 'accepted',
      clientRequestId,
      sessionKey: SESSION,
    })

    expect(instance.api.setupState.value).toBeNull()
    expect(storage.getItem(metaSetupManualStorageKey(SESSION))).toBeNull()
    instance.api.dispose()
  })

  it('restores a queued resume after switching away and back to its session', async () => {
    const storage = memoryStorage()
    let dispatchCount = 0
    const call = vi.fn(async (method: string) => {
      if (method === 'meta.setup.plan') return { ok: true, readiness: readiness({
        ready: true,
        status: 'ready',
        missing_bins: [],
        setup_actions: [],
      }) }
      if (method === 'meta.run') return { ok: true }
      throw new Error(`Unexpected RPC: ${method}`)
    })
    const instance = harness(call, {
      storage,
      dispatchHidden: async (_providerText, _displayText, clientRequestId = '') => {
        dispatchCount += 1
        return {
          status: dispatchCount === 1 ? 'queued' as const : 'accepted' as const,
          reason: dispatchCount === 1 ? 'queued' as const : 'accepted' as const,
          clientRequestId,
          sessionKey: SESSION,
        }
      },
    })
    await instance.api.requestSetup('meta-short-drama', readiness({
      missing_bins: [],
      missing_env: ['OPENROUTER_API_KEY'],
      setup_actions: [],
    }), SESSION)
    await instance.api.retrySetup()
    const clientRequestId = instance.dispatchHidden.mock.calls[0]?.[2]

    instance.currentSessionKey.value = 'agent:main:webchat:other'
    await flushPromises()
    expect(instance.api.setupState.value).toBeNull()

    instance.currentSessionKey.value = SESSION
    await flushPromises()

    expect(instance.dispatchHidden.mock.calls.map((args) => args[2]))
      .toEqual([clientRequestId, clientRequestId])
    expect(instance.api.setupState.value).toBeNull()
    expect(storage.getItem(metaSetupManualStorageKey(SESSION))).toBeNull()
    instance.api.dispose()
  })

  it('defers provider-handoff recovery until the host explicitly restores it', async () => {
    const launchText = '/meta meta-short-drama -- Resume only after session subscribe'
    const storage = memoryStorage()
    const first = harness(vi.fn(async () => ({ ok: true })), { storage })
    await first.api.requestSetup('meta-short-drama', readiness({
      missing_bins: [],
      setup_actions: [],
      manual_setup_actions: [{
        id: 'provider:acme-media',
        kind: 'provider_connection',
        provider_id: 'acme-media',
        available: true,
      }],
    }), SESSION, launchText)
    const clientRequestId = first.api.beginProviderHandoff('acme-media')
    first.api.dispose()

    const call = vi.fn(async (method: string) => {
      if (method === 'meta.run') return { ok: true }
      throw new Error(`Unexpected RPC: ${method}`)
    })
    const second = harness(call, { storage, autoRestore: false })
    await flushPromises()

    expect(call).not.toHaveBeenCalled()
    expect(second.dispatchHidden).not.toHaveBeenCalled()

    await second.api.restoreSetupJob()
    expect(call.mock.calls.map(([method]) => method)).toEqual(['meta.run'])
    expect(second.dispatchHidden).toHaveBeenCalledWith(
      launchText,
      launchText,
      clientRequestId,
    )
    second.api.dispose()
  })

  it('keeps the same ingress id when a handoff checkpoint is copied to another tab', async () => {
    const launchText = '/meta meta-short-drama -- Duplicate-tab safe request'
    const sourceStorage = memoryStorage()
    const first = harness(vi.fn(async () => ({ ok: true })), { storage: sourceStorage })
    await first.api.requestSetup('meta-short-drama', readiness({
      missing_bins: [],
      setup_actions: [],
      manual_setup_actions: [{
        id: 'provider:acme-media',
        kind: 'provider_connection',
        provider_id: 'acme-media',
        available: true,
      }],
    }), SESSION, launchText)
    const clientRequestId = first.api.beginProviderHandoff('acme-media')
    const storageKey = metaSetupManualStorageKey(SESSION)
    const copiedCheckpoint = sourceStorage.getItem(storageKey) || ''
    first.api.dispose()

    const readyCall = vi.fn(async (method: string) => {
      if (method === 'meta.setup.plan') return { ok: true, readiness: readiness({
        ready: true,
        status: 'ready',
        missing_bins: [],
        setup_actions: [],
        manual_setup_actions: [],
      }) }
      if (method === 'meta.run') return { ok: true }
      throw new Error(`Unexpected RPC: ${method}`)
    })
    const tabA = harness(readyCall, {
      storage: memoryStorage({ [storageKey]: copiedCheckpoint }),
      autoRestore: false,
    })
    const tabB = harness(readyCall, {
      storage: memoryStorage({ [storageKey]: copiedCheckpoint }),
      autoRestore: false,
    })

    await Promise.all([tabA.api.restoreSetupJob(), tabB.api.restoreSetupJob()])

    expect(tabA.dispatchHidden).toHaveBeenCalledWith(launchText, launchText, clientRequestId)
    expect(tabB.dispatchHidden).toHaveBeenCalledWith(launchText, launchText, clientRequestId)
    tabA.api.dispose()
    tabB.api.dispose()
  })

  it('does not begin a provider handoff when its checkpoint cannot be persisted', async () => {
    const first = harness(vi.fn(async () => ({ ok: true })), { storage: null })
    await first.api.requestSetup('meta-short-drama', readiness({
      missing_bins: [],
      setup_actions: [],
      manual_setup_actions: [{
        id: 'provider:acme-media',
        kind: 'provider_connection',
        provider_id: 'acme-media',
        available: true,
      }],
    }), SESSION)

    expect(first.api.beginProviderHandoff('acme-media')).toBe('')
    expect(first.api.setupState.value?.providerHandoff).toBeUndefined()
    first.api.dispose()
  })

  it('correlates provider handoff cancellation to its own ingress identity', async () => {
    const launchText = '/meta meta-short-drama -- Keep this server draft identity'
    const stableRequestId = 'server-draft-before-provider-handoff'
    const storage = memoryStorage()
    const first = harness(vi.fn(async () => ({ ok: true })), { storage })
    await first.api.requestSetup('meta-short-drama', readiness({
      missing_bins: [],
      setup_actions: [],
      manual_setup_actions: [{
        id: 'provider:acme-media',
        kind: 'provider_connection',
        provider_id: 'acme-media',
        available: true,
      }],
    }), SESSION, launchText, stableRequestId)

    const clientRequestId = first.api.beginProviderHandoff('acme-media')
    expect(clientRequestId).toBe(stableRequestId)
    expect(first.api.beginProviderHandoff('acme-media')).toBe('')
    first.api.cancelProviderHandoff('acme-media', 'different-request')
    expect(first.api.setupState.value?.providerHandoff?.clientRequestId).toBe(clientRequestId)

    first.api.cancelProviderHandoff('acme-media', clientRequestId)
    expect(first.api.setupState.value?.providerHandoff).toBeUndefined()
    expect(first.api.setupState.value?.resumeRequestId).toBe(stableRequestId)
    expect(storage.getItem(metaSetupManualStorageKey(SESSION))).not.toContain('providerHandoff')
    expect(storage.getItem(metaSetupManualStorageKey(SESSION))).toContain(stableRequestId)
    first.api.dispose()
  })

  it('discards the original server draft before restoring after a failed handoff', async () => {
    const launchText = '/meta meta-short-drama -- Cancel after provider navigation fails'
    const stableRequestId = 'server-draft-provider-navigation-failed'
    const storage = memoryStorage()
    const restoreDraft = vi.fn()
    let completeDiscard!: (outcome: MetaDraftDiscardOutcome) => void
    const discardDraft = vi.fn(() => new Promise<MetaDraftDiscardOutcome>((resolve) => {
      completeDiscard = resolve
    }))
    const first = harness(vi.fn(async () => ({ ok: true })), {
      storage,
      restoreDraft,
      discardDraft,
    })
    await first.api.requestSetup('meta-short-drama', readiness({
      missing_bins: [],
      setup_actions: [],
      manual_setup_actions: [{
        id: 'provider:acme-media',
        kind: 'provider_connection',
        provider_id: 'acme-media',
        available: true,
      }],
    }), SESSION, launchText, stableRequestId)

    expect(first.api.beginProviderHandoff('acme-media')).toBe(stableRequestId)
    first.api.cancelProviderHandoff('acme-media', stableRequestId)
    const cancellation = first.api.cancelSetup()
    await flushPromises()

    expect(discardDraft).toHaveBeenCalledOnce()
    expect(discardDraft).toHaveBeenCalledWith(SESSION, stableRequestId)
    expect(restoreDraft).not.toHaveBeenCalled()
    expect(first.api.setupState.value?.resumeRequestId).toBe(stableRequestId)

    completeDiscard('discarded')
    await cancellation

    expect(restoreDraft).toHaveBeenCalledOnce()
    expect(restoreDraft).toHaveBeenCalledWith(launchText, SESSION)
    expect(first.api.setupState.value).toBeNull()
    first.api.dispose()
  })

  it('expires a provider handoff without automatically checking or launching', async () => {
    vi.setSystemTime(new Date('2026-07-22T00:00:00Z'))
    const launchText = '/meta meta-short-drama -- Do not launch from an old handoff'
    const providerOnly = readiness({
      missing_bins: [],
      missing_env: ['ACME_MEDIA_TOKEN'],
      setup_actions: [],
      manual_setup_actions: [{
        id: 'provider:acme-media',
        kind: 'provider_connection',
        provider_id: 'acme-media',
        label: 'Acme Media',
        available: true,
      }],
    })
    const storage = memoryStorage()
    const first = harness(vi.fn(async () => ({ ok: true })), { storage })
    await first.api.requestSetup('meta-short-drama', providerOnly, SESSION, launchText)

    expect(first.api.setupState.value?.phase).toBe('confirm')
    expect(first.api.setupState.value?.actionIds).toEqual([])
    const clientRequestId = first.api.beginProviderHandoff('acme-media')
    expect(clientRequestId).toEqual(expect.any(String))
    first.api.dispose()

    vi.setSystemTime(Date.now() + META_SETUP_PROVIDER_HANDOFF_TTL_MS + 1)
    const secondCall = vi.fn(async () => ({ ok: true }))
    const restoreDraft = vi.fn()
    const discardDraft = vi.fn(async () => 'discarded' as const)
    const second = harness(secondCall, { storage, restoreDraft, discardDraft })
    await flushPromises()

    expect(second.api.setupState.value?.phase).toBe('confirm')
    expect(second.api.setupState.value?.providerHandoff).toBeUndefined()
    expect(second.api.setupState.value?.resumeRequestId).toBe(clientRequestId)
    expect(second.api.setupState.value?.suppressAutoResume).toBe(true)
    expect(secondCall).not.toHaveBeenCalled()
    expect(second.dispatchHidden).not.toHaveBeenCalled()
    expect(storage.getItem(metaSetupManualStorageKey(SESSION))).not.toContain('providerHandoff')
    expect(storage.getItem(metaSetupManualStorageKey(SESSION))).toContain(clientRequestId)

    await second.api.cancelSetup()
    expect(discardDraft).toHaveBeenCalledWith(SESSION, clientRequestId)
    expect(restoreDraft).toHaveBeenCalledWith(launchText, SESSION)
    expect(second.api.setupState.value).toBeNull()
    second.api.dispose()
  })

  it('keeps the server draft identity when provider navigation does not complete', async () => {
    const storage = memoryStorage()
    const first = harness(vi.fn(async () => ({ ok: true })), { storage })
    await first.api.requestSetup('meta-short-drama', readiness({
      missing_bins: [],
      setup_actions: [],
      manual_setup_actions: [{
        id: 'provider:openrouter',
        kind: 'provider_connection',
        provider_id: 'openrouter',
        available: true,
      }],
    }), SESSION)

    const clientRequestId = first.api.beginProviderHandoff('openrouter')
    expect(clientRequestId).toEqual(expect.any(String))
    first.api.cancelProviderHandoff('openrouter', clientRequestId)
    expect(storage.getItem(metaSetupManualStorageKey(SESSION))).not.toContain('providerHandoff')
    first.api.dispose()

    const secondCall = vi.fn(async (method: string) => {
      if (method === 'meta.run') {
        return {
          ok: false,
          setup_required: true,
          readiness: readiness({
            missing_bins: [],
            setup_actions: [],
            manual_setup_actions: [{
              id: 'provider:openrouter',
              kind: 'provider_connection',
              provider_id: 'openrouter',
              available: true,
            }],
          }),
        }
      }
      throw new Error(`Unexpected RPC: ${method}`)
    })
    const second = harness(secondCall, { storage })
    await flushPromises()
    expect(secondCall).toHaveBeenCalledWith('meta.run', {
      name: 'meta-short-drama',
      sessionKey: SESSION,
      clientRequestId,
      launchText: '/meta meta-short-drama',
    })
    expect(second.api.setupState.value?.phase).toBe('confirm')
    expect(second.api.setupState.value?.resumeRequestId).toBe(clientRequestId)
    second.api.dispose()
  })

  it('keeps the stable checkpoint when install fails before a job is accepted', async () => {
    const launchText = '/meta meta-paper-write -- Preserve this request across a network error'
    const storage = memoryStorage()
    const first = harness(vi.fn(async (method: string) => {
      if (method === 'meta.setup.install') throw new Error('Gateway disconnected')
      throw new Error(`Unexpected RPC: ${method}`)
    }), { storage })

    await first.api.requestSetup('meta-paper-write', readiness(), SESSION, launchText)
    await first.api.confirmSetup()
    expect(first.api.setupState.value?.phase).toBe('failed')
    expect(first.api.setupState.value?.jobId).toBeUndefined()
    expect(storage.getItem(metaSetupManualStorageKey(SESSION))).toContain(launchText)

    first.api.dispose()
    const second = harness(vi.fn(async () => ({ ok: true })), { storage })
    await flushPromises()
    expect(second.api.setupState.value?.phase).toBe('confirm')
    expect(second.api.setupState.value?.launchText).toBe(launchText)
    second.api.dispose()
  })

  it('keeps the manual recovery card actionable when a readiness recheck is still blocked', async () => {
    const call = vi.fn(async (method: string) => {
      if (method === 'meta.setup.plan') {
        return {
          ok: true,
          readiness: readiness({
            missing_bins: [],
            missing_env: ['OPENROUTER_API_KEY'],
            reasons: ['OpenRouter credentials are still missing'],
            setup_actions: [],
          }),
        }
      }
      throw new Error(`Unexpected RPC: ${method}`)
    })
    const { api, dispatchHidden } = harness(call)

    await api.requestSetup('meta-short-drama', readiness({
      missing_bins: [],
      missing_env: ['OPENROUTER_API_KEY'],
      setup_actions: [],
    }), SESSION)
    await api.retrySetup()

    expect(api.setupState.value?.phase).toBe('blocked')
    expect(api.setupState.value?.error).toContain('credentials are still missing')
    expect(api.setupState.value?.retryMode).toBe('readiness')
    expect(call.mock.calls.filter(([method]) => method === 'meta.run')).toHaveLength(0)
    expect(dispatchHidden).not.toHaveBeenCalled()
    api.dispose()
  })

  it('drops a delayed readiness result after the user switches sessions', async () => {
    let resolvePlan: ((value: unknown) => void) | undefined
    const plan = new Promise(resolve => { resolvePlan = resolve })
    const call = vi.fn(async (method: string) => {
      if (method === 'meta.setup.plan') return plan
      if (method === 'meta.run') return { ok: true }
      throw new Error(`Unexpected RPC: ${method}`)
    })
    const { api, currentSessionKey, dispatchHidden } = harness(call)

    await api.requestSetup('meta-short-drama', readiness({
      missing_bins: [],
      missing_env: ['OPENROUTER_API_KEY'],
      setup_actions: [],
    }), SESSION)
    const retry = api.retrySetup()
    await flushPromises()
    currentSessionKey.value = 'agent:main:webchat:another-session'
    await flushPromises()
    resolvePlan?.({
      ok: true,
      readiness: readiness({ ready: true, status: 'ready', missing_bins: [], setup_actions: [] }),
    })
    await retry

    expect(call.mock.calls.filter(([method]) => method === 'meta.run')).toHaveLength(0)
    expect(dispatchHidden).not.toHaveBeenCalled()
    expect(api.setupState.value).toBeNull()
    api.dispose()
  })

  it('never resumes into a session that is no longer current', async () => {
    const call = vi.fn(async (method: string) => {
      if (method === 'meta.setup.install') return { job: job() }
      if (method === 'meta.setup.status') {
        return {
          job: job({
            status: 'completed',
            phase: 'completed',
            readiness: readiness({ ready: true, status: 'ready', missing_bins: [] }),
          }),
        }
      }
      if (method === 'meta.run') return { ok: true }
      throw new Error(`Unexpected RPC: ${method}`)
    })
    const { api, currentSessionKey, dispatchHidden } = harness(call)

    await api.requestSetup('meta-paper-write', readiness(), SESSION)
    await api.confirmSetup()
    currentSessionKey.value = 'agent:main:webchat:another-session'
    await flushPromises()
    await vi.advanceTimersByTimeAsync(1000)

    expect(call.mock.calls.filter(([method]) => method === 'meta.run')).toHaveLength(0)
    expect(dispatchHidden).not.toHaveBeenCalled()
    expect(api.setupState.value).toBeNull()
    api.dispose()
  })

  it('restores a persisted job and resumes after the server reports completion', async () => {
    const launchText = '/meta meta-paper-write -- Restore my original paper request'
    const storage = memoryStorage({
      [metaSetupStorageKey(SESSION)]: 'restored-job',
      [metaSetupLaunchStorageKey(SESSION)]: launchText,
    })
    const call = vi.fn(async (method: string) => {
      if (method === 'meta.setup.status') {
        return {
          job: job({
            job_id: 'restored-job',
            status: 'completed',
            phase: 'completed',
            readiness: readiness({ ready: true, status: 'ready', missing_bins: [] }),
          }),
        }
      }
      if (method === 'meta.run') return { ok: true }
      throw new Error(`Unexpected RPC: ${method}`)
    })
    const { api, dispatchHidden } = harness(call, { storage })
    await flushPromises()

    expect(dispatchHidden).toHaveBeenCalledOnce()
    expect(dispatchHidden).toHaveBeenCalledWith(launchText, launchText, expect.any(String))
    expect(storage.getItem(metaSetupStorageKey(SESSION))).toBeNull()
    expect(storage.getItem(metaSetupLaunchStorageKey(SESSION))).toBeNull()
    expect(api.setupState.value).toBeNull()
    api.dispose()
  })

  it('waits for the RPC connection and retains a job across a transient restore failure', async () => {
    const storage = memoryStorage({ [metaSetupStorageKey(SESSION)]: 'restored-job' })
    const waitForConnection = vi.fn(async () => undefined)
    let statusCalls = 0
    const call = vi.fn(async () => {
      statusCalls += 1
      if (statusCalls === 1) throw new Error('Cannot call meta.setup.status: not connected')
      return { job: job({ job_id: 'restored-job' }) }
    })
    const { api } = harness(call, { storage, waitForConnection })
    await flushPromises()

    expect(waitForConnection).toHaveBeenCalledWith(15_000)
    expect(storage.getItem(metaSetupStorageKey(SESSION))).toBe('restored-job')
    expect(api.setupState.value?.phase).toBe('failed')
    expect(api.setupState.value?.retryMode).toBe('status')
    expect(api.setupState.value?.error).toContain('not connected')

    await api.retrySetup()
    expect(api.setupState.value?.phase).toBe('installing')
    expect(api.setupState.value?.name).toBe('meta-paper-write')
    expect(storage.getItem(metaSetupStorageKey(SESSION))).toBe('restored-job')
    api.dispose()
  })
})
