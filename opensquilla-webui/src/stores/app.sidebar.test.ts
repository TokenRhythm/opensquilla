// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useAppStore } from './app'
import {
  SIDEBAR_WIDTH_PRESETS,
  SIDEBAR_WIDTH_STORAGE_KEY,
} from '@/utils/sidebarLayout'

function stubMatchMedia() {
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })) as unknown as typeof window.matchMedia
}

describe('app store — sidebar width preference', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.clear()
    stubMatchMedia()
  })

  it('hydrates the default synchronously when storage is absent', () => {
    const store = useAppStore()
    expect(store.sidebarWidthPreference).toEqual(SIDEBAR_WIDTH_PRESETS.default)
  })

  it('hydrates and normalizes the versioned storage payload', () => {
    localStorage.setItem(SIDEBAR_WIDTH_STORAGE_KEY, JSON.stringify({
      version: 1,
      width: 350,
      source: 'custom',
    }))

    const store = useAppStore()
    expect(store.sidebarWidthPreference).toEqual({ version: 1, width: 350, source: 'custom' })
  })

  it('persists a normalized complete preference', () => {
    const store = useAppStore()
    store.setSidebarWidthPreference({ version: 1, width: 333, source: 'compact' })

    expect(store.sidebarWidthPreference).toEqual(SIDEBAR_WIDTH_PRESETS.compact)
    expect(JSON.parse(localStorage.getItem(SIDEBAR_WIDTH_STORAGE_KEY)!)).toEqual(
      SIDEBAR_WIDTH_PRESETS.compact,
    )

    setActivePinia(createPinia())
    expect(useAppStore().sidebarWidthPreference).toEqual(SIDEBAR_WIDTH_PRESETS.compact)
  })

  it('resets in memory and removes the persisted override', () => {
    const store = useAppStore()
    store.setSidebarWidthPreference({ version: 1, width: 380, source: 'custom' })
    store.resetSidebarWidthPreference()

    expect(store.sidebarWidthPreference).toEqual(SIDEBAR_WIDTH_PRESETS.default)
    expect(localStorage.getItem(SIDEBAR_WIDTH_STORAGE_KEY)).toBeNull()
  })
})

describe('app store — approval focus request', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.clear()
    stubMatchMedia()
  })

  it('creates a fresh request even when the same approval is clicked twice', () => {
    const store = useAppStore()
    const requestApprovalFocus = (store as unknown as {
      requestApprovalFocus?: (approval: {
        approvalId: string
        sessionKey: string
        tool: string
        command: string
      }) => void
    }).requestApprovalFocus

    expect(typeof requestApprovalFocus).toBe('function')
    requestApprovalFocus!({
      approvalId: 'approval-1',
      sessionKey: 'agent:main:webchat:test',
      tool: 'exec_command',
      command: 'printf ok',
    })
    const first = (store as unknown as {
      approvalFocusRequest?: { requestId: number; approvalId: string; sessionKey: string }
    }).approvalFocusRequest

    requestApprovalFocus!({
      approvalId: 'approval-1',
      sessionKey: 'agent:main:webchat:test',
      tool: 'exec_command',
      command: 'printf ok',
    })
    const second = (store as unknown as {
      approvalFocusRequest?: { requestId: number; approvalId: string; sessionKey: string }
    }).approvalFocusRequest

    expect(first).toMatchObject({
      approvalId: 'approval-1',
      sessionKey: 'agent:main:webchat:test',
    })
    expect(second?.requestId).toBeGreaterThan(first?.requestId ?? 0)
  })
})

describe('app store — deleted-session approval cleanup', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.clear()
    stubMatchMedia()
  })

  it('removes only matching session approvals and clears their focus request', () => {
    const store = useAppStore()
    store.setPendingApprovals([
      {
        approvalId: 'delete-me',
        sessionKey: 'agent:main:webchat:deleted',
        tool: 'exec_command',
        command: 'printf deleted',
      },
      {
        approvalId: 'keep-me',
        sessionKey: 'agent:main:webchat:other',
        tool: 'exec_command',
        command: 'printf other',
      },
      {
        approvalId: 'keep-unscoped',
        sessionKey: '',
        tool: 'plugin',
        command: '',
      },
    ])
    store.requestApprovalFocus(store.pendingApprovals[0]!)

    store.removePendingApprovalsForSessions([
      'agent:main:webchat:deleted',
      'agent:main:webchat:deleted',
      '',
    ])
    store.removePendingApprovalsForSessions(['agent:main:webchat:deleted'])

    expect(store.pendingApprovals.map(item => item.approvalId)).toEqual([
      'keep-me',
      'keep-unscoped',
    ])
    expect(store.approvalCount).toBe(2)
    expect(store.approvalFocusRequest).toBeNull()

    // A delayed Gateway resolved event is safe after the local session purge.
    store.removePendingApproval('delete-me')
    expect(store.pendingApprovals.map(item => item.approvalId)).toEqual([
      'keep-me',
      'keep-unscoped',
    ])
  })

  it('is idempotent when the resolved event arrives before session deletion', () => {
    const store = useAppStore()
    store.setPendingApprovals([{
      approvalId: 'resolved-first',
      sessionKey: 'agent:main:webchat:deleted',
      tool: 'exec_command',
      command: 'printf done',
    }])

    store.removePendingApproval('resolved-first')
    store.removePendingApprovalsForSessions(['agent:main:webchat:deleted'])

    expect(store.pendingApprovals).toEqual([])
    expect(store.approvalCount).toBe(0)
  })
})
