import { describe, expect, it, vi } from 'vitest'

import {
  RpcAbortError,
  RpcTimeoutError,
  RpcTransportError,
  type RpcCallOptions,
  type RpcEventHandler,
} from '@/lib/rpc'
import { ApprovalCenterError } from '@/modules/approvalCenter'
import { HttpTransportError } from './privateHttpTransport'
import { createApprovalCenterV4 } from './approvalCenterV4'

function harness() {
  const handlers = new Map<string, RpcEventHandler>()
  const calls: Array<{ method: string, params?: Record<string, unknown>, options?: RpcCallOptions }> = []
  const rpc = {
    request: vi.fn(async <T = unknown>(method: string, params?: Record<string, unknown>, options?: RpcCallOptions) => {
      calls.push({ method, params, options })
      const values: Record<string, unknown> = {
        'exec.approval.status': {
          found: true, id: 'a1', namespace: 'exec', pending: true,
          resolutionInProgress: false, resolved: false, approved: false,
          resolution: '', consumed: false, deadline: null,
        },
        'plugin.approval.status': {
          found: true, id: 'p1', namespace: 'plugin', pending: true,
          resolutionInProgress: false, resolved: false, approved: false,
          resolution: '', consumed: false, deadline: null,
        },
        'exec.approval.extend': {
          id: 'a1', mode: 'prompt', approved: false, resolved: false,
          resolution: '', deadline: 123, consumed: false, pending: true,
        },
      }
      return values[method] as T
    }) as unknown as <T = unknown>(
      method: string,
      params?: Record<string, unknown>,
      options?: RpcCallOptions,
    ) => Promise<T>,
  }
  const events = {
    subscribe: vi.fn((event: string, handler: RpcEventHandler) => {
      handlers.set(event, handler)
      return { close: vi.fn(() => handlers.delete(event)) }
    }),
  }
  const requestJson = vi.fn(async <T = unknown>(endpoint: string, _options?: unknown) => {
      if (endpoint === '/api/approvals') {
        return {
          mode: 'prompt', pending: [{ id: 'a1', namespace: 'exec', toolName: 'shell', sessionKey: 's', args: { command: 'ls', token: 'secret' }, command: 'ls' }],
          allowPatterns: [], denyPatterns: [],
        } as T
      }
      return {
        id: 'a1', mode: 'prompt', approved: true, resolved: true,
        resolution: 'approved', deadline: null, consumed: false, pending: false,
      } as T
    })
  const http = {
    requestJson: requestJson as unknown as <T = unknown>(endpoint: string, options?: unknown) => Promise<T>,
  }
  return { rpc, events, http, requestJson, handlers, calls }
}

describe('ApprovalCenter v4 adapter', () => {
  it('projects HTTP snapshot and validates all RPC operations', async () => {
    const h = harness()
    const center = createApprovalCenterV4(h.rpc, h.events, { http: h.http })
    await expect(center.setElevatedMode('session-1', 'on')).resolves.toBeUndefined()
    await expect(center.snapshot()).resolves.toMatchObject({ mode: 'prompt', pending: [{ id: 'a1', args: { command: 'ls' } }] })
    await expect(center.status('exec', 'a1')).resolves.toMatchObject({ id: 'a1', namespace: 'exec', pending: true })
    await expect(center.extend('exec', 'a1', 30)).resolves.toMatchObject({ deadline: 123 })
    await expect(center.resolve({ id: 'a1', namespace: 'exec', decision: 'allow-once' })).resolves.toMatchObject({ approved: true, resolved: true })
    expect(h.calls.map(call => call.method)).toEqual(['exec.approval.status', 'exec.approval.extend'])
    expect(h.requestJson).toHaveBeenCalledWith('/api/elevated-mode', expect.objectContaining({
      method: 'POST', json: { sessionKey: 'session-1', mode: 'on' },
    }))
    expect(h.requestJson).toHaveBeenCalledWith('/api/approvals/resolve', expect.objectContaining({
      method: 'POST', json: { id: 'a1', namespace: 'exec', approved: true, choice: 'allow_once' },
    }))
    center.dispose()
  })

  it('normalizes events, redacts args, and forwards connection state', () => {
    const h = harness()
    const center = createApprovalCenterV4(h.rpc, h.events, { http: h.http })
    const received: unknown[] = []
    const states: string[] = []
    center.subscribe(event => received.push(event))
    center.subscribeAvailability(state => states.push(state))
    h.handlers.get('exec.approval.requested')?.({
      approval_id: 'a1', namespace: 'exec', session_key: 's', tool_name: 'shell',
      approval_kind: '', args: { command: 'ls', token: 'secret' }, warning: '', stream_seq: 4,
    })
    h.handlers.get('_state')?.('connected')
    expect(received).toHaveLength(1)
    expect(received[0]).toMatchObject({ kind: 'requested', approvalId: 'a1', activityOrder: 4, approval: { args: { command: 'ls' } } })
    expect(JSON.stringify(received)).not.toContain('secret')
    expect(states).toEqual(['available'])
    center.dispose()
    expect(h.handlers.size).toBe(0)
  })

  it('fails closed on malformed HTTP result and maps transport errors', async () => {
    const h = harness()
    h.requestJson.mockImplementation(async () => ({ approved: true }))
    const center = createApprovalCenterV4(h.rpc, h.events, { http: h.http })
    await expect(center.resolve({ id: 'a1', namespace: 'exec', decision: 'deny' })).rejects.toThrow(/Contract/)
    h.requestJson.mockRejectedValueOnce(new HttpTransportError('http-status', 'forbidden', 403))
    await expect(center.snapshot()).rejects.toBeInstanceOf(ApprovalCenterError)
    await expect(center.resolve({ id: '', namespace: 'exec', decision: 'deny' })).rejects.toMatchObject({ kind: 'invalid' })
    await expect(center.status('other' as 'exec', 'a1')).rejects.toMatchObject({ kind: 'invalid' })
    await expect(center.extend('exec', 'a1', 0)).rejects.toMatchObject({ kind: 'invalid' })
    center.dispose()
  })

  it.each([
    ['timeout', new RpcTimeoutError('exec.approval.status', 1000)],
    ['abort', new RpcAbortError('exec.approval.status')],
    ['transport', new RpcTransportError('socket closed', null)],
  ] as const)('maps private RPC %s errors to domain unavailable', async (_label, error) => {
    const h = harness()
    const requestMock = h.rpc.request as unknown as {
      mockRejectedValueOnce: (value: unknown) => unknown
    }
    requestMock.mockRejectedValueOnce(error)
    const center = createApprovalCenterV4(h.rpc, h.events, { http: h.http })
    await expect(center.status('exec', 'a1')).rejects.toMatchObject({
      name: 'ApprovalCenterError',
      kind: 'unavailable',
    })
    center.dispose()
  })
})
