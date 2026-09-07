import { describe, expect, it, vi } from 'vitest'
import { MetaRunCenterError, type MetaRunCenter } from '@/modules/metaRunCenter'
import { createV4MetaRunCenter } from './metaRunCenterV4'

function centerWith(result: unknown) {
  const request = vi.fn(async (_method: string, _params?: Record<string, unknown>, _options?: unknown) => result)
  const center = createV4MetaRunCenter({ request: async <T>(method: string, params?: Record<string, unknown>, options?: unknown) =>
    await request(method, params, options) as T }, { subscribe: () => ({ close() {} }) })
  return { center, request }
}

const setupRequests = {
  plan: (center: MetaRunCenter, signal?: AbortSignal) => center.setupPlan('paper', { signal }),
  status: (center: MetaRunCenter, signal?: AbortSignal) => center.setupStatus({
    jobId: 'job-1', sessionKey: 'agent:main:setup',
  }, { signal }),
  install: (center: MetaRunCenter, signal?: AbortSignal) => center.setupInstall({
    name: 'paper', sessionKey: 'agent:main:setup', confirmed: true, actionIds: ['tools'],
  }, { signal }),
}

describe.each(Object.entries(setupRequests))('Meta setup %s failures', (_name, invoke) => {
  it.each([
    ['UNAUTHORIZED', 'forbidden'], ['INVALID_PARAMS', 'invalid'], ['UNAVAILABLE', 'unavailable'],
  ])('maps request rejection %s to its existing domain meaning', async (code, expected) => {
    const failure = Object.assign(new Error('Setup request failed'), { code })
    const center = createV4MetaRunCenter({ request: async () => { throw failure } }, {
      subscribe: () => ({ close() {} }),
    })
    await expect(invoke(center)).rejects.toMatchObject({
      name: 'MetaRunCenterError', code: expected, cause: failure,
    })
  })

  it('maps connection failure before issuing a request', async () => {
    const failure = new Error('Setup connection is unavailable')
    const request = vi.fn(async () => { throw new Error('must not send') })
    const center = createV4MetaRunCenter({ request, ready: async () => { throw failure } }, {
      subscribe: () => ({ close() {} }),
    })
    await expect(invoke(center)).rejects.toMatchObject({
      name: 'MetaRunCenterError', code: 'unavailable', cause: failure,
    })
    expect(request).not.toHaveBeenCalled()
  })

  it.each(['ready', 'request'].flatMap(stage => ['dom', 'rpc'].map(kind => ({ stage, kind }))))('preserves $kind cancellation during $stage without treating the job as missing', async ({ stage, kind }) => {
    const controller = new AbortController()
    const failure = kind === 'dom' ? new DOMException('Setup operation aborted', 'AbortError')
      : Object.assign(new Error('RPC call to meta.setup.status aborted'), { code: 'RPC_ABORTED', name: 'RpcAbortError' })
    controller.abort(failure)
    const request = vi.fn(async () => { throw failure })
    const center = createV4MetaRunCenter({
      request,
      ready: async options => {
        expect(options?.signal).toBe(controller.signal)
        if (stage === 'ready') throw failure
      },
    }, { subscribe: () => ({ close() {} }) })
    await expect(invoke(center, controller.signal)).rejects.toMatchObject({
      name: 'MetaRunCenterError', code: 'unavailable', cause: failure,
    })
    expect(request).toHaveBeenCalledTimes(stage === 'request' ? 1 : 0)
  })
})

describe('Meta setup boundary', () => {
  it('projects already-ready installation without exposing its wire envelope', async () => {
    const { center, request } = centerWith({
      ok: true, already_ready: true,
      readiness: { ready: true, missing_bins: [], ignored: 'wire-only' },
      reused: false,
    })
    await expect(center.setupInstall({
      name: 'paper', sessionKey: 'agent:main:setup', confirmed: true, actionIds: ['tools'],
    })).resolves.toEqual({ alreadyReady: true, readiness: { ready: true, missing_bins: [] } })
    expect(request).toHaveBeenCalledWith('meta.setup.install', {
      name: 'paper', sessionKey: 'agent:main:setup', confirmed: true, action_ids: ['tools'],
    }, undefined)
  })

  it('projects complete job progress and readiness while preserving checkpoint names', async () => {
    const { center } = centerWith({ ok: true, job: {
      jobId: 'job-1', name: 'paper', session_key: 'agent:main:setup',
      actionIds: ['tools'], status: 'running', phase: 'verifying',
      currentAction: 'tools', downloadedBytes: 0, downloadTotalBytes: 100,
      completedActions: ['tools'], startedAtMs: 10, finishedAtMs: 0,
      message: 'Verifying capabilities', ignored: true,
      readiness: {
        ready: false, missingEnvAny: [['A', 'B']],
        setupActions: [{ id: 'tools', installId: 'install', available: true,
          downloadSizeBytes: null, downloadSizeIsMinimum: false, requiresAdmin: false }],
        manualSetupActions: [{ id: 'provider', kind: 'provider_connection', providerId: 'example',
          capabilityIds: ['image'], reasonCode: 'missing', recommended: true }],
      },
    } })
    const { job } = await center.setupStatus({ jobId: 'job-1', sessionKey: 'agent:main:setup' })
    expect(job).toMatchObject({
      job_id: 'job-1', name: 'paper', sessionKey: 'agent:main:setup', action_ids: ['tools'],
      status: 'running', phase: 'verifying', current_action: 'tools', downloaded_bytes: 0,
      download_total_bytes: 100, completed_actions: ['tools'], started_at_ms: 10, finished_at_ms: 0,
      readiness: {
        ready: false, missing_env_any: [['A', 'B']],
        setup_actions: [{ id: 'tools', install_id: 'install', available: true,
          download_size_bytes: null, download_size_is_minimum: false, requires_admin: false }],
        manual_setup_actions: [{ id: 'provider', kind: 'provider_connection', provider_id: 'example',
          capability_ids: ['image'], reason_code: 'missing', recommended: true }],
      },
    })
    expect(job).not.toHaveProperty('ignored')
  })

  it.each(['setup job not found', 'Unknown setup job', 'setup job is unknown'])('maps legacy missing-job results: %s', async error => {
    const { center } = centerWith({ ok: false, error })
    await expect(center.setupStatus({ jobId: 'gone', sessionKey: 'agent:main:setup' }))
      .rejects.toMatchObject({ name: 'MetaRunCenterError', code: 'not-found', message: error })
  })

  it('maps old Gateway missing-job exceptions without leaking transport codes', async () => {
    const center = createV4MetaRunCenter({ request: async () => {
      throw new Error('meta setup job not found (404)')
    } }, { subscribe: () => ({ close() {} }) })
    await expect(center.setupStatus({ jobId: 'gone', sessionKey: 'agent:main:setup' }))
      .rejects.toMatchObject({ name: 'MetaRunCenterError', code: 'not-found' })
  })

  it.each([
    ['METHOD_NOT_FOUND', 'unsupported'], ['FORBIDDEN', 'forbidden'], ['NOT_FOUND', 'not-found'],
  ])('preserves explicit %s error meaning even if the message says not found', async (code, expected) => {
    const center = createV4MetaRunCenter({ request: async () => {
      throw Object.assign(new Error('not found'), { code })
    } }, { subscribe: () => ({ close() {} }) })
    await expect(center.setupStatus({ jobId: 'gone', sessionKey: 'agent:main:setup' }))
      .rejects.toMatchObject({ code: expected })
  })

  it('accepts a legacy job response without an ok envelope', async () => {
    const { center } = centerWith({ job: {
      job_id: 'job-1', name: 'paper', sessionKey: 'agent:main:setup',
      action_ids: [], status: 'queued', phase: 'queued', readiness: null,
    } })
    await expect(center.setupInstall({ name: 'paper', sessionKey: 'agent:main:setup',
      actionIds: ['tools'], confirmed: true })).resolves.toMatchObject({ job: {
      job_id: 'job-1', readiness: null,
    } })
  })

  it('keeps the caller readiness fallback when an older install result only says already ready', async () => {
    const { center } = centerWith({ already_ready: true })
    await expect(center.setupInstall({ name: 'paper', sessionKey: 'agent:main:setup',
      actionIds: ['tools'], confirmed: true })).resolves.toEqual({ alreadyReady: true })
  })

  it.each([null, [], 'invalid', { ok: true }, { ok: true, readiness: false }])('rejects malformed readiness instead of returning an arbitrary result: %j', async result => {
    const { center } = centerWith(result)
    await expect(center.setupPlan('paper')).rejects.toBeInstanceOf(MetaRunCenterError)
    await expect(center.setupPlan('paper')).rejects.toMatchObject({ code: 'invalid' })
  })

  it('preserves disabled setup as an unavailable failure, not unsupported', async () => {
    const { center } = centerWith({ ok: false, disabled: true, error: 'meta-skills are disabled' })
    await expect(center.setupPlan('paper')).rejects.toMatchObject({ code: 'unavailable', message: 'meta-skills are disabled' })
  })
})
