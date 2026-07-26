import { afterEach, describe, expect, it, vi } from 'vitest'
import { effectScope, ref } from 'vue'
import type { RpcEventHandler } from '@/lib/rpc'
import type { InterruptViewState } from '@/types/parts'
import {
  safeApprovalDisplayArgs,
  useChatApprovals,
} from './useChatApprovals'

afterEach(() => {
  vi.unstubAllGlobals()
})

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { promise, resolve }
}

async function harness(statusResult: unknown = { found: true, pending: true, resolved: false }) {
  const handlers = new Map<string, RpcEventHandler>()
  const rpcCall = vi.fn(async <T,>() => statusResult as T)
  const appendInterruptFrame = vi.fn()
  const interruptState = ref<ReadonlyMap<string, InterruptViewState>>(new Map())
  const scope = effectScope()
  const approvals = scope.run(() => useChatApprovals({
    rpc: {
      call: rpcCall as <T = unknown>(
        method: string,
        params?: Record<string, unknown>,
      ) => Promise<T>,
      on: vi.fn((event: string, handler: RpcEventHandler) => {
        handlers.set(event, handler)
        return () => handlers.delete(event)
      }),
    },
    sessionKey: ref('agent:main:web'),
    runStatus: ref({ status: 'idle', label: '', task: null }),
    stream: {
      isStreaming: ref(false),
      appendInterruptFrame,
      ensureInterruptBubble: vi.fn(),
    },
    interruptState,
  }))!
  await vi.waitFor(() => expect(fetch).toHaveBeenCalled())
  vi.mocked(fetch).mockClear()
  const unsubscribe = approvals.subscribe()
  await vi.waitFor(() => expect(fetch).toHaveBeenCalled())
  vi.mocked(fetch).mockClear()
  return { approvals, handlers, rpcCall, appendInterruptFrame, interruptState, unsubscribe, scope }
}

function installSnapshot(pending: unknown[] = []) {
  const fetchMock = vi.fn(async () => ({
    ok: true,
    status: 200,
    json: async () => ({ pending }),
  }))
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

describe('approval safe display contracts', () => {
  it('whitelists sandbox context and drops sensitive internals recursively', () => {
    expect(safeApprovalDisplayArgs('sandbox_path', {
      path: '/workspace/report.md',
      access: 'write',
      workspace: '/workspace',
      fingerprint: 'do-not-show',
      review_action: 'approve',
      token: 'secret',
    })).toEqual({
      path: '/workspace/report.md',
      access: 'write',
      workspace: '/workspace',
    })
    expect(safeApprovalDisplayArgs('sandbox_network', {
      host: 'example.com',
      bundle_id: 'curl',
      workspace: '/workspace',
      sessionKey: 'secret',
    })).toEqual({ host: 'example.com', bundle_id: 'curl', workspace: '/workspace' })
  })

  it('uses complete additive pushes without a snapshot and backfills old lean pushes once', async () => {
    const fetchMock = installSnapshot()
    const runtime = await harness()
    try {
      runtime.handlers.get('exec.approval.requested')?.({
        approval_id: 'new-push',
        namespace: 'exec',
        session_key: 'agent:main:web',
        approval_kind: 'sandbox_path',
        tool_name: '',
        args: null,
        warning: '',
      })
      await Promise.resolve()
      expect(fetchMock).not.toHaveBeenCalled()
      expect(runtime.appendInterruptFrame).toHaveBeenLastCalledWith(expect.objectContaining({
        approvalId: 'new-push',
        data: expect.objectContaining({ toolName: 'sandbox path', args: null, warning: '' }),
      }))

      const oldPush = {
        approval_id: 'old-push',
        namespace: 'exec',
        session_key: 'agent:main:web',
        approval_kind: 'sandbox_network',
      }
      runtime.handlers.get('exec.approval.requested')?.(oldPush)
      runtime.handlers.get('exec.approval.requested')?.(oldPush)
      await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    } finally {
      runtime.unsubscribe()
      runtime.scope.stop()
    }
  })

  it('hydrates the additive snapshot approvalKind without relying on legacy params', async () => {
    installSnapshot([{
      id: 'sandbox-snapshot',
      namespace: 'exec',
      sessionKey: 'agent:main:web',
      approvalKind: 'sandbox_path',
      args: { path: '/workspace/report.md', access: 'write', workspace: '/workspace' },
      warning: 'Outside the default write boundary',
    }])
    const runtime = await harness()
    try {
      expect(runtime.appendInterruptFrame).toHaveBeenCalledWith(expect.objectContaining({
        approvalId: 'sandbox-snapshot',
        data: expect.objectContaining({
          toolName: 'sandbox path',
          approvalKind: 'sandbox_path',
          args: { path: '/workspace/report.md', access: 'write', workspace: '/workspace' },
        }),
      }))
    } finally {
      runtime.unsubscribe()
      runtime.scope.stop()
    }
  })

  it('never renders a legacy sandbox policy action from a full params snapshot', async () => {
    installSnapshot([{
      id: 'legacy-elevation',
      namespace: 'exec',
      sessionKey: 'agent:main:web',
      params: {
        approvalKind: 'sandbox_elevation',
        action: { argv: ['sudo', 'cat', '/etc/shadow'], authorization: 'Bearer secret' },
        fingerprint: 'internal-review-fingerprint',
        reviewer: 'policy-engine',
      },
    }])
    const runtime = await harness()
    try {
      expect(runtime.appendInterruptFrame).toHaveBeenCalledWith(expect.objectContaining({
        approvalId: 'legacy-elevation',
        data: expect.objectContaining({
          approvalKind: 'sandbox_elevation',
          args: null,
        }),
      }))
      const rendered = JSON.stringify(runtime.appendInterruptFrame.mock.calls)
      expect(rendered).not.toContain('/etc/shadow')
      expect(rendered).not.toContain('fingerprint')
    } finally {
      runtime.unsubscribe()
      runtime.scope.stop()
    }
  })
})

describe('approval reconnect recovery', () => {
  it('marks an approval unavailable when the authoritative status says it is missing', async () => {
    installSnapshot()
    const runtime = await harness({ found: false, pending: false, resolved: false })
    try {
      runtime.handlers.get('exec.approval.requested')?.({
        approval_id: 'gone',
        namespace: 'exec',
        session_key: 'agent:main:web',
        tool_name: 'shell',
        args: null,
        warning: '',
      })
      runtime.handlers.get('_state')?.('connected')
      await vi.waitFor(() => {
        expect(runtime.interruptState.value.get('gone')?.resolution).toBe('unavailable')
      })
      expect(runtime.rpcCall).toHaveBeenCalledWith('exec.approval.status', { id: 'gone' })
    } finally {
      runtime.unsubscribe()
      runtime.scope.stop()
    }
  })

  it('does not let a late pending status reopen a card settled by a resolved push', async () => {
    installSnapshot()
    const status = deferred<{
      found: boolean
      pending: boolean
      resolved: boolean
      resolutionInProgress: boolean
    }>()
    const runtime = await harness(status.promise)
    // The helper returns the promise itself from the mock result. Replace it
    // with a genuinely delayed generic RPC for this race.
    runtime.rpcCall.mockImplementation(async <T,>() => await status.promise as T)
    try {
      runtime.handlers.get('exec.approval.requested')?.({
        approval_id: 'race',
        namespace: 'exec',
        session_key: 'agent:main:web',
        tool_name: 'shell',
        args: null,
        warning: '',
      })
      runtime.handlers.get('_state')?.('connected')
      await vi.waitFor(() => expect(runtime.rpcCall).toHaveBeenCalled())
      runtime.handlers.get('exec.approval.resolved')?.({
        approval_id: 'race',
        approved: true,
        resolution: 'approved',
      })
      status.resolve({ found: true, pending: true, resolved: false, resolutionInProgress: false })
      await Promise.resolve()
      await Promise.resolve()
      expect(runtime.interruptState.value.get('race')?.resolution).toBe('approved')
    } finally {
      runtime.unsubscribe()
      runtime.scope.stop()
    }
  })
})
