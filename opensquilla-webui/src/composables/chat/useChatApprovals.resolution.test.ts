import { ref } from 'vue'
import { afterEach, describe, it, expect, vi } from 'vitest'
import {
  formatCountdown,
  resolutionFromPayload,
  resolutionFromResolveResponse,
  useChatApprovals,
} from './useChatApprovals'
import { approvalChoiceForDecision } from '@/modules/approvalCenter'
import type { ChatApprovalEntry } from './useChatApprovals'
import type { InterruptViewState } from '@/types/parts'

afterEach(() => {
  vi.unstubAllGlobals()
})

function installApprovalFetch(resolvePayload: Record<string, unknown>) {
  const fetchMock = vi.fn(async (_input: unknown, init?: RequestInit) => ({
    ok: true,
    status: 200,
    json: async () => init?.method === 'POST' ? resolvePayload : { pending: [] },
  }))
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function approvalEntry(): ChatApprovalEntry {
  return {
    approval: {
      id: 'approval-1',
      namespace: 'exec',
      toolName: 'shell',
      command: 'echo hello',
      approvalKind: '',
      args: null,
      warning: '',
      agent: 'main',
      sessionKey: 'agent:main:web',
      deadline: 0,
    },
    resolution: null,
    error: '',
  }
}

function approvalHarness(statusResponse: Record<string, unknown> = {
  found: true,
  pending: true,
  resolved: false,
  resolutionInProgress: true,
}) {
  const interruptState = ref<ReadonlyMap<string, InterruptViewState>>(new Map())
  const approvals = useChatApprovals({
    approvalCenter: {
      snapshot: vi.fn(async () => ({ pending: [], mode: 'prompt' as const })),
      status: vi.fn(async () => ({
        id: 'approval-1', namespace: 'exec' as const, pending: statusResponse.pending === true,
        resolutionInProgress: statusResponse.resolutionInProgress === true,
        resolved: statusResponse.resolved === true, approved: statusResponse.approved === true,
        resolution: String(statusResponse.resolution || ''), consumed: false, deadline: null,
        ...(statusResponse.found === undefined ? {} : { found: statusResponse.found === true }),
      })),
      resolve: vi.fn(async () => {
        const response = await fetch('/api/approvals/resolve', { method: 'POST' })
        return await response.json() as Record<string, unknown>
      }),
      extend: vi.fn(async () => ({ id: 'approval-1', namespace: 'exec' as const, pending: true, resolutionInProgress: false, resolved: false, approved: false, resolution: '', consumed: false, deadline: null })),
      subscribe: vi.fn(() => ({ close: vi.fn() })),
      subscribeAvailability: vi.fn(() => ({ close: vi.fn() })),
      dispose: vi.fn(),
    },
    rpc: {
      call: vi.fn(async () => statusResponse) as <T = unknown>(
        method: string,
        params?: Record<string, unknown>,
      ) => Promise<T>,
      on: vi.fn(() => () => {}),
    },
    sessionKey: ref('agent:main:web'),
    runStatus: ref({ status: 'idle', label: '', task: null }),
    stream: {
      isStreaming: ref(false),
      appendInterruptFrame: vi.fn(),
      ensureInterruptBubble: vi.fn(),
    },
    interruptState,
  })
  return { approvals, interruptState }
}

describe('resolutionFromPayload', () => {
  it('maps an explicit expiry to a distinct expired state', () => {
    expect(resolutionFromPayload({ approved: false, resolution: 'expired' })).toBe('expired')
  })

  it('keeps an explicit deny distinct from an expiry', () => {
    expect(resolutionFromPayload({ approved: false, resolution: 'denied' })).toBe('denied')
  })

  it('maps an approval to approved', () => {
    expect(resolutionFromPayload({ approved: true, resolution: 'approved' })).toBe('approved')
  })

  it('falls back to denied/approved when no resolution field is present', () => {
    // Back-compat: older payloads without `resolution` still resolve.
    expect(resolutionFromPayload({ approved: false })).toBe('denied')
    expect(resolutionFromPayload({ approved: true })).toBe('approved')
  })

  it('treats expired as not-denied even though approved is false', () => {
    const r = resolutionFromPayload({ approved: false, resolution: 'expired' })
    expect(r).not.toBe('denied')
  })
})

describe('resolutionFromResolveResponse', () => {
  it('keeps an in-flight cross-surface resolution pending', () => {
    expect(resolutionFromResolveResponse({
      approved: false,
      resolved: false,
      pending: true,
      resolutionInProgress: true,
    })).toBeNull()
  })

  it('uses the Gateway canonical decision', () => {
    expect(resolutionFromResolveResponse({ approved: true, resolved: true })).toBe('approved')
    expect(resolutionFromResolveResponse({ approved: false, resolved: true })).toBe('denied')
  })

  it('does not infer an outcome from a malformed non-canonical response', () => {
    expect(resolutionFromResolveResponse({ approved: true })).toBeNull()
    expect(resolutionFromResolveResponse({ resolved: true })).toBeNull()
  })
})

describe('cross-surface resolve behavior', () => {
  it('labels a legacy card with the opposite canonical result', async () => {
    installApprovalFetch({ approved: true, resolved: true, pending: false })
    const { approvals } = approvalHarness()
    const entry = approvalEntry()

    await approvals.resolveApproval(entry, 'deny')

    expect(entry.resolution).toBe('approved')
  })

  it('sends exactly one request for an explicit denial', async () => {
    const fetchMock = installApprovalFetch({ approved: false, resolved: true, pending: false })
    const { approvals } = approvalHarness()
    const entry = approvalEntry()

    await approvals.resolveApproval(entry, 'deny')

    expect(entry.resolution).toBe('denied')
    const resolveCalls = fetchMock.mock.calls.filter(([, init]) => init?.method === 'POST')
    expect(resolveCalls).toHaveLength(1)
  })

  it('keeps a legacy card unresolved while another surface is resolving', async () => {
    installApprovalFetch({
      approved: false,
      resolved: false,
      pending: true,
      resolutionInProgress: true,
    })
    const { approvals } = approvalHarness()
    const entry = approvalEntry()

    await approvals.resolveApproval(entry, 'deny')

    expect(entry.resolution).toBeNull()
    expect(entry.error).toBe('')
  })

  it('uses the canonical result for an inline approval', async () => {
    installApprovalFetch({ approved: false, resolved: true, pending: false })
    const { approvals, interruptState } = approvalHarness()

    await approvals.resolveInterrupt('approval-1', 'allow-once')

    expect(interruptState.value.get('approval-1')).toMatchObject({
      resolution: 'denied',
      busy: false,
      error: '',
    })
  })

  it('keeps an inline approval open while another surface is resolving', async () => {
    installApprovalFetch({
      approved: false,
      resolved: false,
      pending: true,
      resolutionInProgress: true,
    })
    const { approvals, interruptState } = approvalHarness()

    await approvals.resolveInterrupt('approval-1', 'allow-once')

    expect(interruptState.value.get('approval-1')).toMatchObject({
      resolution: null,
      busy: true,
      error: '',
    })
  })

  it('uses status recovery to settle a resolve response that lost the final push', async () => {
    installApprovalFetch({ pending: true, resolved: false, resolutionInProgress: true })
    const { approvals, interruptState } = approvalHarness({
      found: true,
      pending: false,
      resolved: true,
      approved: false,
      resolution: 'expired',
    })

    await approvals.resolveInterrupt('approval-1', 'allow-once')

    expect(interruptState.value.get('approval-1')).toMatchObject({
      resolution: 'expired',
      busy: false,
    })
  })

  it('marks a missing approval unavailable without guessing approve or deny', async () => {
    installApprovalFetch({ pending: true, resolved: false, resolutionInProgress: true })
    const { approvals, interruptState } = approvalHarness({ found: false })

    await approvals.resolveInterrupt('approval-1', 'allow-once')

    expect(interruptState.value.get('approval-1')).toMatchObject({
      resolution: 'unavailable',
      busy: false,
    })
  })
})

describe('formatCountdown', () => {
  it('renders sub-minute counts as seconds', () => {
    expect(formatCountdown(0)).toBe('0s')
    expect(formatCountdown(45)).toBe('45s')
    expect(formatCountdown(59)).toBe('59s')
  })

  it('renders minute counts as m:ss', () => {
    expect(formatCountdown(60)).toBe('1:00')
    expect(formatCountdown(125)).toBe('2:05')
    expect(formatCountdown(300)).toBe('5:00')
  })

  it('clamps negatives to 0s', () => {
    expect(formatCountdown(-10)).toBe('0s')
  })
})

describe('approvalChoiceForDecision', () => {
  it('maps the three visible approval buttons to backend choices', () => {
    expect(approvalChoiceForDecision('allow-once')).toBe('allow_once')
    expect(approvalChoiceForDecision('allow-always')).toBe('allow_same_type')
    expect(approvalChoiceForDecision('deny')).toBe('deny')
  })
})
