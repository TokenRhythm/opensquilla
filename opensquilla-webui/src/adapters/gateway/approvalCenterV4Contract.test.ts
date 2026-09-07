import { readFileSync } from 'node:fs'
import { describe, expect, it, vi } from 'vitest'

import {
  APPROVAL_EVENT_WIRE_NAMES,
  ApprovalCenterContractError,
  createApprovalCenterV4Contract,
  decodeApprovalEvent,
  projectApprovalHttpSnapshot,
} from './approvalCenterV4Contract'

interface FixtureCase {
  id: string
  wire_name?: string
  wire: Record<string, unknown>
}

interface FixtureDocument {
  methods: {
    snapshot: { requests: FixtureCase[], responses: FixtureCase[] }
    status: { requests: FixtureCase[], results: FixtureCase[] }
    resolve: { requests: FixtureCase[], results: FixtureCase[], errors: FixtureCase[] }
    extend: { requests: FixtureCase[], results: FixtureCase[] }
  }
  events: FixtureCase[]
  http_snapshot: Record<string, unknown>
}

function fixture(): FixtureDocument {
  const url = new URL(
    '../../../../contracts/gateway/v4/approvals/fixtures/approval-center.json',
    import.meta.url,
  )
  return JSON.parse(readFileSync(url, 'utf8')) as FixtureDocument
}

describe('S16-A ApprovalCenter V4 Contract adapter', () => {
  it('derives the complete lifecycle event set from Contract metadata', () => {
    expect(APPROVAL_EVENT_WIRE_NAMES).toEqual([
      'exec.approval.requested',
      'exec.approval.updated',
      'exec.approval.resolved',
      'plugin.approval.requested',
      'plugin.approval.updated',
      'plugin.approval.resolved',
    ])
  })

  it('accepts current, camelCase legacy, and lean legacy event projections', () => {
    for (const testCase of fixture().events) {
      const original = JSON.parse(JSON.stringify(testCase.wire)) as Record<string, unknown>
      const decoded = decodeApprovalEvent(testCase.wire_name as string, testCase.wire)
      expect(testCase.wire, testCase.id).toEqual(original)
      expect(decoded.approvalId, testCase.id).toBe(
        testCase.wire.approval_id ?? testCase.wire.approvalId,
      )
      expect(decoded.namespace, testCase.id).toBe(testCase.wire.namespace)
    }
  })

  it('normalizes aliases without leaking private queue fields', () => {
    const payload = {
      approvalId: 'plugin-1',
      namespace: 'plugin',
      sessionKey: 'agent:main:webchat:demo',
      toolName: 'demo-plugin',
      approvalKind: 'plugin_permission',
      displayKind: 'plugin_permission',
      displayTarget: 'demo-plugin',
      backupState: 'not_applicable',
      createdAt: 10,
      deadline: null,
      args: { permissions: ['filesystem.read'] },
      params: { authorization: 'Bearer secret' },
      future_resolution_metadata: { source: 'user_web' },
    }
    const decoded = decodeApprovalEvent('plugin.approval.updated', payload)
    expect(decoded.approvalId).toBe('plugin-1')
    expect(decoded.sessionKey).toBe('agent:main:webchat:demo')
    expect(decoded.metadata).toEqual({ future_resolution_metadata: { source: 'user_web' } })
    expect(decoded.metadata).not.toHaveProperty('params')
    expect(decoded.args).toEqual({ permissions: ['filesystem.read'] })
    expect(payload).toHaveProperty('params')
  })

  it('redacts nested sensitive event arguments before domain projection', () => {
    const decoded = decodeApprovalEvent('exec.approval.requested', {
      approval_id: 'exec-1',
      namespace: 'exec',
      args: {
        command: 'curl',
        headers: { authorization: 'Bearer secret', accept: 'application/json' },
        nested: { token: 'nested-secret', visible: true },
      },
      future_metadata: { review_action: 'claim', label: 'safe' },
    })
    expect(decoded.args).toEqual({
      command: 'curl',
      headers: { accept: 'application/json' },
      nested: { visible: true },
    })
    expect(decoded.metadata).toEqual({ future_metadata: { label: 'safe' } })
  })

  it('rejects conflicting HTTP timestamp aliases at the boundary', () => {
    expect(() => projectApprovalHttpSnapshot({
      mode: 'prompt',
      pending: [{ id: 'a', namespace: 'exec', created_at: 1, createdAt: 2 }],
    })).toThrow(/created_at and createdAt aliases conflict/)
  })

  it('rejects conflicting aliases, bad versions, and non-object args', () => {
    expect(() => decodeApprovalEvent('exec.approval.requested', {
      approval_id: 'a', approvalId: 'b',
    })).toThrow(/conflicting aliases/)
    expect(() => decodeApprovalEvent('exec.approval.requested', {
      approval_id: 'a', schema_version: 2,
    })).toThrow(ApprovalCenterContractError)
    expect(() => decodeApprovalEvent('exec.approval.requested', {
      approval_id: 'a', args: [],
    })).toThrow(ApprovalCenterContractError)
    expect(() => decodeApprovalEvent('exec.approval.requested', {
      approval_id: 'a',
    }, { allowLegacy: false })).toThrow(/legacy schema_version/)
    expect(() => decodeApprovalEvent('plugin.approval.updated', {
      approval_id: 'a', namespace: 'exec',
    })).toThrow(/namespace does not match event name/)
  })

  it('routes exec/plugin aliases and projects all read/command results', async () => {
    const data = fixture()
    const calls: Array<{ method: string, params?: Record<string, unknown> }> = []
    const responses: Record<string, unknown> = {
      'exec.approval.snapshot': data.methods.snapshot.responses[0]?.wire.payload,
      'exec.approval.status': data.methods.status.results[0]?.wire,
      'plugin.approval.status': data.methods.status.results[1]?.wire,
      'exec.approval.resolve': data.methods.resolve.results[0]?.wire,
      'plugin.approval.resolve': data.methods.resolve.results[1]?.wire,
      'exec.approval.extend': data.methods.extend.results[0]?.wire,
      'plugin.approval.extend': data.methods.extend.results[1]?.wire,
    }
    const transport = {
      request: vi.fn(async (method: string, params?: Record<string, unknown>) => {
        calls.push({ method, params })
        return responses[method]
      }),
    }
    const events = { subscribe: vi.fn(() => ({ close: vi.fn() })) }
    const center = createApprovalCenterV4Contract(transport, events)

    await expect(center.snapshot()).resolves.toMatchObject({ mode: 'prompt' })
    await expect(center.status('exec', 'approval-exec-1')).resolves.toMatchObject({
      id: 'approval-exec-1', namespace: 'exec', found: true, pending: true,
    })
    await expect(center.status('plugin', 'approval-plugin-1')).resolves.toMatchObject({
      id: 'approval-plugin-1', namespace: 'plugin', resolutionInProgress: true,
    })
    await expect(center.resolve('exec', {
      id: 'approval-exec-1', approved: true, choice: 'allow_once',
    })).resolves.toMatchObject({ resolved: true, approved: true })
    await expect(center.extend('plugin', 'approval-plugin-1', 120.5)).resolves.toMatchObject({
      id: 'approval-plugin-1', deadline: 1730000123.5,
    })

    expect(calls.map(call => call.method)).toEqual([
      'exec.approval.snapshot',
      'exec.approval.status',
      'plugin.approval.status',
      'exec.approval.resolve',
      'plugin.approval.extend',
    ])
    expect(calls[3]?.params).toEqual({
      id: 'approval-exec-1', approved: true, choice: 'allow_once',
    })
    expect(calls[4]?.params).toEqual({ id: 'approval-plugin-1', seconds: 120.5 })
    center.dispose()
  })

  it('keeps removed resolve flags visible to the Gateway instead of dropping them', async () => {
    const transport = {
      request: vi.fn(async () => ({
        id: 'approval-exec-1', mode: 'prompt', approved: false, resolved: false,
        resolution: '', deadline: null, consumed: false, pending: true,
      })),
    }
    const center = createApprovalCenterV4Contract(transport, { subscribe: () => ({ close() {} }) })
    await center.resolve('exec', {
      id: 'approval-exec-1', approved: true, allowAlways: true, rememberIntent: false,
    })
    expect(transport.request).toHaveBeenCalledWith(
      'exec.approval.resolve',
      { id: 'approval-exec-1', approved: true, allowAlways: true, rememberIntent: false },
      undefined,
    )
    center.dispose()
  })

  it('fails closed for malformed results and fail-open for malformed events', async () => {
    const violations: ApprovalCenterContractError[] = []
    const handlers = new Map<string, (payload: unknown) => void>()
    const center = createApprovalCenterV4Contract(
      { request: vi.fn(async () => ({ mode: 'invalid' })) },
      { subscribe: (event, handler) => {
        handlers.set(event, handler)
        return { close: vi.fn() }
      } },
      { onViolation: error => violations.push(error) },
    )
    await expect(center.snapshot()).rejects.toThrow(ApprovalCenterContractError)
    const received: unknown[] = []
    const subscription = center.subscribe(value => received.push(value))
    handlers.get('exec.approval.requested')?.({ approval_id: 'ok', schema_version: 2 })
    handlers.get('exec.approval.requested')?.({ approval_id: 'ok' })
    expect(received).toHaveLength(1)
    expect(violations).toHaveLength(1)
    subscription.close()
    center.dispose()
  })

  it('projects the redacted HTTP snapshot and rejects queue internals', () => {
    const projected = projectApprovalHttpSnapshot(fixture().http_snapshot)
    expect(projected.mode).toBe('prompt')
    expect(projected.pending[0]).toMatchObject({
      id: 'approval-exec-1', namespace: 'exec', toolName: 'http_request',
      createdAt: 1730000002.5, actionKind: 'http_request', mode: 'prompt',
    })
    expect(projected.pending[0]).not.toHaveProperty('params')
    expect(projected.pending[0]?.args?.headers).toEqual({
      Authorization: '[REDACTED]',
      'X-Trace-Id': 'trace-visible',
    })
    expect(() => projectApprovalHttpSnapshot({
      ...fixture().http_snapshot,
      pending: [{ id: 'a', namespace: 'exec', params: { token: 'secret' } }],
    })).toThrow(/private queue field/)
    expect(() => projectApprovalHttpSnapshot({
      ...fixture().http_snapshot,
      pending: [{ id: 'a', namespace: 'exec', claimToken: 'opaque' }],
    })).toThrow(/private queue field/)
  })
})
