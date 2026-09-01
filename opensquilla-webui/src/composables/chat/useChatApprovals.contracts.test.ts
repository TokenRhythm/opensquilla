import { afterEach, describe, expect, it, vi } from 'vitest'
import { effectScope, ref } from 'vue'
import type { RpcEventHandler } from '@/lib/rpc'
import type { InterruptViewState } from '@/types/parts'
import { projectApprovalDisplayArgs } from '@/adapters/gateway/approvalCenterV4Contract'
import { sessionConversationFromTestRpc } from '@/testing/sessionConversation.test-helper'
import {
  useChatApprovals,
} from './useChatApprovals'

const safeApprovalDisplayArgs = projectApprovalDisplayArgs

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
  const listeners = new Set<(event: any) => void>()
  const rpcCall = vi.fn(async <T,>(_method?: string, _params?: Record<string, unknown>) => statusResult as T)
  const appendInterruptFrame = vi.fn()
  const interruptState = ref<ReadonlyMap<string, InterruptViewState>>(new Map())
  const scope = effectScope()
  const approvalCenter: any = {
    snapshot: vi.fn(async () => {
      const response = await fetch('/api/approvals')
      const data = await response.json() as { pending?: any[] }
      return { mode: 'prompt' as const, pending: (data.pending || []).map(item => {
        const params = item.params && typeof item.params === 'object' ? item.params : null
        const kind = String(item.approvalKind || params?.approvalKind || params?.approval_kind || '')
        const command = String(item.command || '')
        const args = item.args || (params?.args && typeof params.args === 'object' ? params.args : null)
        const displayKind = item.displayKind || (kind === 'sandbox_path' ? 'path_access' : kind === 'sandbox_network' ? 'network_access' : command ? 'run_command' : 'sensitive_operation')
        const displayTarget = item.displayTarget || (displayKind === 'path_access' ? String(args?.path || '') : displayKind === 'network_access' ? String(args?.host || args?.bundle_id || '') : '')
        return {
        id: String(item.id || ''), namespace: item.namespace === 'plugin' ? 'plugin' : 'exec',
        toolName: String(item.toolName || item.pluginId || item.actionKind || ''),
        command, approvalKind: kind, args: safeApprovalDisplayArgs(kind, args), warning: String(item.warning || ''), agent: String(item.agent || ''),
        sessionKey: String(item.sessionKey || ''), deadline: Number(item.deadline) || 0,
        displayKind, displayTarget,
        destructive: item.destructive === true, irreversible: item.irreversible === true,
        backupState: item.backupState,
        }
      }) }
    }),
    status: vi.fn(async (_namespace: string, id: string) => {
      await rpcCall('exec.approval.status', { id })
      return { ...(statusResult as any), id, namespace: 'exec', consumed: false, resolutionInProgress: (statusResult as any).resolutionInProgress === true, approved: (statusResult as any).approved === true, resolution: String((statusResult as any).resolution || ''), deadline: null }
    }),
    resolve: vi.fn(async () => statusResult as any),
    extend: vi.fn(async () => ({ ...(statusResult as any), id: 'approval', namespace: 'exec', deadline: 0, consumed: false, resolutionInProgress: false, approved: false, resolution: '' })),
    subscribe: vi.fn((listener: (event: any) => void) => { listeners.add(listener); return { close: () => listeners.delete(listener) } }),
    subscribeAvailability: vi.fn((listener: (state: 'available' | 'recovering' | 'unavailable') => void) => { handlers.set('_state', listener as any); return { close: vi.fn() } }),
    dispose: vi.fn(),
  }
  for (const wire of ['exec.approval.requested', 'exec.approval.updated', 'exec.approval.resolved', 'plugin.approval.requested', 'plugin.approval.updated', 'plugin.approval.resolved']) {
    handlers.set(wire, ((payload: any) => {
      const requested = wire.endsWith('.requested')
      const updated = wire.endsWith('.updated')
      const id = String(payload.approval_id || payload.approvalId || '')
      const namespace = wire.startsWith('plugin.') ? 'plugin' : 'exec'
      const approval = requested || updated ? {
        ...(() => {
          const kind = String(payload.approval_kind || payload.approvalKind || '')
          const command = String(payload.command || '')
          const displayKind = payload.display_kind || payload.displayKind || (kind === 'sandbox_path' ? 'path_access' : kind === 'sandbox_network' ? 'network_access' : command ? 'run_command' : 'sensitive_operation')
          const args = safeApprovalDisplayArgs(kind, payload.args || null)
          return { approvalKind: kind, command, args, displayKind, displayTarget: payload.display_target || payload.displayTarget || (displayKind === 'path_access' ? String(args?.path || '') : displayKind === 'network_access' ? String(args?.host || args?.bundle_id || '') : '') }
        })(),
        id, namespace, toolName: String(payload.tool_name || payload.toolName || ''),
        warning: String(payload.warning || ''), agent: String(payload.agent || ''),
        sessionKey: String(payload.session_key || payload.sessionKey || ''), deadline: Number(payload.deadline) || 0,

        destructive: payload.destructive === true, irreversible: payload.irreversible === true, backupState: payload.backup_state || payload.backupState,
      } : undefined
      listeners.forEach(listener => listener({ kind: requested ? 'requested' : updated ? 'updated' : 'resolved', approvalId: id, namespace, approval, sessionKey: approval?.sessionKey || null, approved: typeof payload.approved === 'boolean' ? payload.approved : null, resolution: payload.resolution || null, emittedAt: payload.emitted_at || payload.created_at || null, activityOrder: payload.stream_seq, needsHydration: requested && (!Object.prototype.hasOwnProperty.call(payload, 'args') || !Object.prototype.hasOwnProperty.call(payload, 'warning')) }))
    }) as any)
  }
  const approvals = scope.run(() => useChatApprovals({
    approvalCenter,
    sessionConversation: sessionConversationFromTestRpc({
      call: rpcCall as <T = unknown>(
        method: string,
        params?: Record<string, unknown>,
      ) => Promise<T>,
      on: vi.fn((event: string, handler: RpcEventHandler) => {
        handlers.set(event, handler)
        return () => handlers.delete(event)
      }),
    }),
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
        data: expect.objectContaining({
          toolName: '',
          displayKind: 'path_access',
          args: null,
          warning: '',
        }),
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
          toolName: '',
          displayKind: 'path_access',
          displayTarget: '/workspace/report.md',
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
      runtime.handlers.get('_state')?.('available')
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
      runtime.handlers.get('_state')?.('available')
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

describe('timed approval updates', () => {
  it('applies a post-request deadline without reconnecting or losing it to a late zero', async () => {
    installSnapshot()
    const runtime = await harness()
    try {
      const future = Date.now() / 1000 + 120
      const payload = {
        approval_id: 'timed',
        namespace: 'exec',
        session_key: 'agent:main:web',
        tool_name: 'shell',
        args: null,
        warning: '',
      }
      runtime.handlers.get('exec.approval.requested')?.({
        ...payload,
        deadline: 0,
      })
      runtime.handlers.get('exec.approval.updated')?.({
        ...payload,
        deadline: future,
      })
      runtime.handlers.get('exec.approval.requested')?.({
        ...payload,
        deadline: 0,
      })

      expect(runtime.appendInterruptFrame).toHaveBeenLastCalledWith(
        expect.objectContaining({
          approvalId: 'timed',
          data: expect.objectContaining({ deadline: future }),
        }),
      )
      expect(runtime.handlers.has('plugin.approval.updated')).toBe(true)
    } finally {
      runtime.unsubscribe()
      runtime.scope.stop()
    }
  })

  it('updates rollback approval entries and ignores updates after resolution', async () => {
    installSnapshot([{
      id: 'timed-snapshot',
      namespace: 'exec',
      sessionKey: 'agent:main:web',
      toolName: 'shell',
      deadline: 0,
    }])
    const runtime = await harness()
    try {
      const future = Date.now() / 1000 + 120
      const payload = {
        approval_id: 'timed-snapshot',
        namespace: 'exec',
        session_key: 'agent:main:web',
        tool_name: 'shell',
        args: null,
        warning: '',
        deadline: future,
      }
      runtime.handlers.get('exec.approval.updated')?.(payload)
      expect(
        runtime.approvals.approvalEntries.value[0]?.approval.deadline,
      ).toBe(future)

      runtime.handlers.get('exec.approval.resolved')?.({
        approval_id: 'timed-snapshot',
        approved: false,
        resolution: 'expired',
      })
      const appendCount = runtime.appendInterruptFrame.mock.calls.length
      runtime.handlers.get('exec.approval.updated')?.({
        ...payload,
        deadline: future + 300,
      })
      expect(runtime.appendInterruptFrame).toHaveBeenCalledTimes(appendCount)
      expect(
        runtime.approvals.approvalEntries.value[0]?.approval.deadline,
      ).toBe(future)
    } finally {
      runtime.unsubscribe()
      runtime.scope.stop()
    }
  })
})

describe('clarify tool-result recovery', () => {
  const clarifyResult = {
    kind: 'user_input',
    paused: true,
    request_id: 'input-request-1',
    run_id: 'plan-run-1',
    step: 'confirm_scope',
    clarify_schema: {
      intro: 'Confirm the implementation scope.',
      fields: [{
        name: 'scope',
        type: 'enum',
        required: true,
        prompt: 'Which scope?',
        choices: ['focused', 'complete'],
      }],
    },
  }
  const planClarifyResult = {
    ...clarifyResult,
    clarify_schema: {
      ...clarifyResult.clarify_schema,
      presentation: 'plan_questionnaire_v1',
    },
  }

  it.each([
    ['object result', clarifyResult],
    ['serialized JSON result', JSON.stringify(clarifyResult)],
  ])('renders and submits a request_user_input %s', async (_label, result) => {
    installSnapshot()
    const runtime = await harness()
    try {
      runtime.handlers.get('session.event.tool_result')?.({
        session_key: 'agent:main:web',
        tool_use_id: 'request-input-1',
        name: 'request_user_input',
        result,
      })

      expect(runtime.approvals.pendingClarify.value).toEqual({
        intro: 'Confirm the implementation scope.',
        fields: [{
          name: 'scope',
          type: 'enum',
          required: true,
          prompt: 'Which scope?',
          defaultValue: '',
          choices: ['focused', 'complete'],
        }],
        requestId: 'input-request-1',
        runId: 'plan-run-1',
        step: 'confirm_scope',
      })
      expect(runtime.appendInterruptFrame).toHaveBeenLastCalledWith(expect.objectContaining({
        interruptKind: 'clarify',
        approvalId: 'input-request-1',
      }))

      await runtime.approvals.submitClarify({ scope: 'focused' })
      expect(runtime.rpcCall).toHaveBeenLastCalledWith('chat.clarify_submit', {
        sessionKey: 'agent:main:web',
        fields: { scope: 'focused' },
        request_id: 'input-request-1',
        run_id: 'plan-run-1',
      })
      expect(runtime.approvals.pendingClarify.value).toBeNull()
      expect(runtime.approvals.clarifySubmitted.value).toBe(false)
      expect(runtime.approvals.clarifyBusy.value).toBe(false)
    } finally {
      runtime.unsubscribe()
      runtime.scope.stop()
    }
  })

  it('hydrates a pending deferred request after reconnect', async () => {
    installSnapshot()
    const runtime = await harness()
    try {
      runtime.approvals.applyUserInputBootstrap({
        pendingUserInputs: [clarifyResult],
      })

      expect(runtime.approvals.pendingClarify.value?.requestId).toBe('input-request-1')
      expect(runtime.appendInterruptFrame).toHaveBeenLastCalledWith(expect.objectContaining({
        interruptKind: 'clarify',
        approvalId: 'input-request-1',
      }))
      expect(runtime.interruptState.value.get('input-request-1')?.resolution).toBeNull()
    } finally {
      runtime.unsubscribe()
      runtime.scope.stop()
    }
  })

  it('releases a failed submit when reconnect says that request is no longer pending', async () => {
    installSnapshot()
    const runtime = await harness()
    try {
      runtime.handlers.get('session.event.tool_result')?.({
        session_key: 'agent:main:web',
        tool_use_id: 'request-input-1',
        name: 'request_user_input',
        result: planClarifyResult,
      })
      runtime.rpcCall.mockRejectedValueOnce(new Error('connection lost after send'))
      await runtime.approvals.submitClarify({ scope: 'focused' })

      expect(runtime.approvals.pendingClarify.value?.requestId).toBe('input-request-1')
      expect(runtime.approvals.clarifyError.value).toContain('connection lost after send')

      runtime.approvals.applyUserInputBootstrap({ pendingUserInputs: [] })

      expect(runtime.approvals.pendingClarify.value).toBeNull()
      expect(runtime.approvals.clarifySubmitted.value).toBe(false)
      expect(runtime.approvals.clarifyBusy.value).toBe(false)
      expect(runtime.approvals.clarifyError.value).toBe('')
      expect(runtime.interruptState.value.get('input-request-1')).toEqual({
        resolution: 'replied',
        busy: false,
        error: '',
      })
    } finally {
      runtime.unsubscribe()
      runtime.scope.stop()
    }
  })

  it('does not settle a failed submit from a partial snapshot without pending-input state', async () => {
    installSnapshot()
    const runtime = await harness()
    try {
      runtime.handlers.get('session.event.tool_result')?.({
        session_key: 'agent:main:web',
        tool_use_id: 'request-input-1',
        name: 'request_user_input',
        result: planClarifyResult,
      })
      runtime.rpcCall.mockRejectedValueOnce(new Error('gateway unavailable'))
      await runtime.approvals.submitClarify({ scope: 'focused' })

      runtime.approvals.applyUserInputBootstrap({})

      expect(runtime.approvals.pendingClarify.value?.requestId).toBe('input-request-1')
      expect(runtime.approvals.clarifyError.value).toContain('gateway unavailable')
      expect(runtime.interruptState.value.get('input-request-1')?.resolution).toBeNull()
    } finally {
      runtime.unsubscribe()
      runtime.scope.stop()
    }
  })

  it('settles the matching card when the same tool id returns answered', async () => {
    installSnapshot()
    const runtime = await harness()
    try {
      const handler = runtime.handlers.get('session.event.tool_result')
      handler?.({
        session_key: 'agent:main:web',
        tool_use_id: 'request-input-1',
        name: 'request_user_input',
        result: clarifyResult,
      })
      handler?.({
        session_key: 'agent:main:web',
        tool_use_id: 'request-input-1',
        name: 'request_user_input',
        result: {
          kind: 'user_input',
          status: 'answered',
          paused: false,
          request_id: 'input-request-1',
          answers: { scope: 'focused' },
        },
      })

      expect(runtime.interruptState.value.get('input-request-1')).toEqual({
        resolution: 'replied',
        busy: false,
        error: '',
      })
      expect(runtime.approvals.pendingClarify.value).toBeNull()
      expect(runtime.approvals.clarifySubmitted.value).toBe(false)
      expect(runtime.approvals.clarifyBusy.value).toBe(false)
      expect(runtime.approvals.clarifyError.value).toBe('')
    } finally {
      runtime.unsubscribe()
      runtime.scope.stop()
    }
  })

  it('keeps a Plan questionnaire actionable when submission fails', async () => {
    installSnapshot()
    const runtime = await harness()
    try {
      runtime.handlers.get('session.event.tool_result')?.({
        session_key: 'agent:main:web',
        tool_use_id: 'request-input-1',
        name: 'request_user_input',
        result: planClarifyResult,
      })
      runtime.rpcCall.mockRejectedValueOnce(new Error('gateway unavailable'))

      await runtime.approvals.submitClarify({ scope: 'focused' })

      expect(runtime.approvals.pendingClarify.value).toEqual(expect.objectContaining({
        requestId: 'input-request-1',
        presentation: 'plan_questionnaire_v1',
      }))
      expect(runtime.approvals.clarifySubmitted.value).toBe(false)
      expect(runtime.approvals.clarifyBusy.value).toBe(false)
      expect(runtime.approvals.clarifyError.value).toContain('gateway unavailable')
      expect(runtime.interruptState.value.get('input-request-1')).toEqual(expect.objectContaining({
        resolution: null,
        busy: false,
      }))
    } finally {
      runtime.unsubscribe()
      runtime.scope.stop()
    }
  })

  it('does not let a delayed submit response dismiss a newer questionnaire', async () => {
    installSnapshot()
    const runtime = await harness()
    const submitted = deferred<unknown>()
    try {
      const handler = runtime.handlers.get('session.event.tool_result')
      handler?.({
        session_key: 'agent:main:web',
        tool_use_id: 'request-input-1',
        name: 'request_user_input',
        result: planClarifyResult,
      })
      runtime.rpcCall.mockImplementationOnce(async <T,>() => await submitted.promise as T)
      const firstSubmit = runtime.approvals.submitClarify({ scope: 'focused' })
      await vi.waitFor(() => expect(runtime.rpcCall).toHaveBeenCalledWith(
        'chat.clarify_submit',
        expect.objectContaining({ request_id: 'input-request-1' }),
      ))

      handler?.({
        session_key: 'agent:main:web',
        tool_use_id: 'request-input-2',
        name: 'request_user_input',
        result: {
          ...planClarifyResult,
          request_id: 'input-request-2',
          run_id: 'plan-run-2',
        },
      })
      submitted.resolve({ resolved: true, request_id: 'input-request-1' })
      await firstSubmit

      expect(runtime.approvals.pendingClarify.value).toEqual(expect.objectContaining({
        requestId: 'input-request-2',
        runId: 'plan-run-2',
      }))
      expect(runtime.approvals.clarifySubmitted.value).toBe(false)
      expect(runtime.approvals.clarifyBusy.value).toBe(false)
      expect(runtime.approvals.clarifyError.value).toBe('')
    } finally {
      runtime.unsubscribe()
      runtime.scope.stop()
    }
  })

  it('does not clear a newer request for a non-matching terminal outcome', async () => {
    installSnapshot()
    const runtime = await harness()
    try {
      const handler = runtime.handlers.get('session.event.tool_result')
      handler?.({
        session_key: 'agent:main:web',
        tool_use_id: 'request-input-2',
        name: 'request_user_input',
        result: {
          ...planClarifyResult,
          request_id: 'input-request-2',
          run_id: 'plan-run-2',
        },
      })
      handler?.({
        session_key: 'agent:main:web',
        tool_use_id: 'request-input-1',
        name: 'request_user_input',
        result: {
          kind: 'user_input',
          status: 'answered',
          paused: false,
          request_id: 'input-request-1',
          answers: { scope: 'focused' },
        },
      })

      expect(runtime.approvals.pendingClarify.value?.requestId).toBe('input-request-2')
      expect(runtime.interruptState.value.get('input-request-1')?.resolution).toBe('replied')
    } finally {
      runtime.unsubscribe()
      runtime.scope.stop()
    }
  })

  it('does not resurrect a settled request from a late paused-event replay', async () => {
    installSnapshot()
    const runtime = await harness()
    try {
      const handler = runtime.handlers.get('session.event.tool_result')
      handler?.({
        session_key: 'agent:main:web',
        tool_use_id: 'request-input-1',
        name: 'request_user_input',
        result: {
          kind: 'user_input',
          status: 'answered',
          paused: false,
          request_id: 'input-request-1',
          answers: { scope: 'focused' },
        },
      })
      const appendCount = runtime.appendInterruptFrame.mock.calls.length

      handler?.({
        session_key: 'agent:main:web',
        tool_use_id: 'request-input-1',
        name: 'request_user_input',
        result: planClarifyResult,
      })

      expect(runtime.approvals.pendingClarify.value).toBeNull()
      expect(runtime.appendInterruptFrame).toHaveBeenCalledTimes(appendCount)
      expect(runtime.interruptState.value.get('input-request-1')?.resolution).toBe('replied')
    } finally {
      runtime.unsubscribe()
      runtime.scope.stop()
    }
  })

  it('retains the legacy cross-turn clarify receipt after a successful send acknowledgement', async () => {
    installSnapshot()
    const runtime = await harness()
    try {
      runtime.handlers.get('session.event.tool_result')?.({
        session_key: 'agent:main:web',
        tool_use_id: 'legacy-clarify',
        name: 'request_user_input',
        result: {
          ...clarifyResult,
          request_id: undefined,
        },
      })

      await runtime.approvals.submitClarify({ scope: 'focused' })

      expect(runtime.rpcCall).toHaveBeenLastCalledWith('chat.clarify_submit', {
        sessionKey: 'agent:main:web',
        fields: { scope: 'focused' },
        run_id: 'plan-run-1',
      })
      expect(runtime.approvals.pendingClarify.value?.requestId).toBeUndefined()
      expect(runtime.approvals.clarifySubmitted.value).toBe(true)
      expect(runtime.approvals.clarifyBusy.value).toBe(false)
    } finally {
      runtime.unsubscribe()
      runtime.scope.stop()
    }
  })
})
