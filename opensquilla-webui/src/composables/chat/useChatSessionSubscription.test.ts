import { ref } from 'vue'
import { describe, expect, it, vi } from 'vitest'

import { useChatSessionSubscription } from './useChatSessionSubscription'
import type { ChatRunStatus, ChatRunStatusState } from '@/types/chat'

function createSubscription(hasActiveInterrupt = false) {
  const resetStreamLiveTurnState = vi.fn()
  const runStatus = ref({ status: 'idle' as const, label: 'Idle', task: null })
  const rpc = {
    waitForConnection: vi.fn().mockResolvedValue(undefined),
    call: vi.fn().mockResolvedValue({
      subscribed: true,
      status: 'idle',
      current_stream_seq: 0,
      replay_complete: true,
    }),
  }
  const api = useChatSessionSubscription({
    rpc,
    sessionKey: ref('agent:main:webchat:e2eapproval'),
    lastStreamSeq: ref(0),
    runStatus,
    isStreaming: ref(true),
    hasActiveInterrupt: ref(hasActiveInterrupt),
    activeStreamTaskId: ref(''),
    activeTaskGroups: ref(new Set<string>()),
    sessionRunStatus: source => {
      const status = source?.run_status === 'approval_pending' ? 'approval_pending' : 'idle'
      return { status, label: status === 'approval_pending' ? 'Approval pending' : 'Idle', task: null }
    },
    startStreaming: vi.fn(),
    loadHistory: vi.fn(),
    resetStreamIdleTimer: vi.fn(),
    resetStreamLiveTurnState,
  })
  return { api, resetStreamLiveTurnState, runStatus }
}

describe('useChatSessionSubscription', () => {
  it('preserves an interrupt bubble when a late idle subscription snapshot arrives', async () => {
    const { api, resetStreamLiveTurnState, runStatus } = createSubscription(true)

    await api.subscribeSession()

    expect(resetStreamLiveTurnState).not.toHaveBeenCalled()
    expect(runStatus.value.status).toBe('approval_pending')
  })

  it('still clears a stale replay bubble when no interrupt is active', async () => {
    const { api, resetStreamLiveTurnState } = createSubscription(false)

    const outcome = await api.subscribeSession()

    expect(resetStreamLiveTurnState).toHaveBeenCalledOnce()
    expect(outcome).toEqual({ authoritative: true, live: false, backgroundOnly: false })
  })
})

describe('useChatSessionSubscription', () => {
  it('marks an initial session subscription as hydrating until its snapshot arrives', async () => {
    let resolveSnapshot: ((value: unknown) => void) | undefined
    const snapshot = new Promise(resolve => { resolveSnapshot = resolve })
    const rpc = {
      waitForConnection: vi.fn(async () => {}),
      call: <T = unknown>() => snapshot as Promise<T>,
    }
    const lastStreamSeq = ref(0)
    const subscription = useChatSessionSubscription({
      rpc,
      sessionKey: ref('agent:main:webchat:test'),
      lastStreamSeq,
      runStatus: ref<ChatRunStatus>({ status: 'idle', label: '', task: null }),
      isStreaming: ref(false),
      hasActiveInterrupt: ref(false),
      activeStreamTaskId: ref(''),
      activeTaskGroups: ref(new Set<string>()),
      sessionRunStatus: source => ({
        status: String(source?.run_status || 'idle') as ChatRunStatusState,
        label: '',
        task: source?.active_task || null,
      }),
      startStreaming: vi.fn(),
      loadHistory: vi.fn(),
      resetStreamIdleTimer: vi.fn(),
      resetStreamLiveTurnState: vi.fn(),
    })

    const pending = subscription.subscribeSession()
    await Promise.resolve()

    expect(subscription.isHydrating.value).toBe(true)

    resolveSnapshot?.({
      subscribed: true,
      run_status: 'cancelled',
      active_task_group_ids: [],
      current_stream_seq: 20,
    })
    await pending

    expect(subscription.isHydrating.value).toBe(false)
  })

  it('hydrates a live backend task into the local streaming state', async () => {
    const isStreaming = ref(false)
    const runStatus = ref<ChatRunStatus>({ status: 'idle', label: '', task: null })
    const activeStreamTaskId = ref('')
    const startStreaming = vi.fn(() => { isStreaming.value = true })
    const rpc = {
      waitForConnection: vi.fn(async () => {}),
      call: async <T = unknown>() => ({
          subscribed: true,
          run_status: 'running',
          active_task: { task_id: 'task-live', status: 'running' },
          current_stream_seq: 12,
        }) as T,
    }

    const subscription = useChatSessionSubscription({
      rpc,
      sessionKey: ref('agent:main:webchat:test'),
      lastStreamSeq: ref(0),
      runStatus,
      isStreaming,
      hasActiveInterrupt: ref(false),
      activeStreamTaskId,
      activeTaskGroups: ref(new Set<string>()),
      sessionRunStatus: source => ({
        status: String(
          source?.run_status || source?.active_task?.status || 'idle',
        ) as ChatRunStatusState,
        label: '',
        task: source?.active_task || null,
      }),
      startStreaming,
      loadHistory: vi.fn(),
      resetStreamIdleTimer: vi.fn(),
      resetStreamLiveTurnState: vi.fn(),
    })

    const outcome = await subscription.subscribeSession()

    expect(runStatus.value.status).toBe('running')
    expect(startStreaming).toHaveBeenCalledOnce()
    expect(activeStreamTaskId.value).toBe('task-live')
    expect(outcome).toEqual({ authoritative: true, live: true, backgroundOnly: false })
  })

  it('reports a failed subscription as non-authoritative', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const rpc = {
      waitForConnection: vi.fn(async () => {}),
      call: vi.fn().mockRejectedValue(new Error('socket closed')),
    }
    const subscription = useChatSessionSubscription({
      rpc,
      sessionKey: ref('agent:main:webchat:test'),
      lastStreamSeq: ref(0),
      runStatus: ref<ChatRunStatus>({ status: 'idle', label: '', task: null }),
      isStreaming: ref(false),
      hasActiveInterrupt: ref(false),
      activeStreamTaskId: ref(''),
      activeTaskGroups: ref(new Set<string>()),
      sessionRunStatus: () => ({ status: 'idle', label: '', task: null }),
      startStreaming: vi.fn(),
      loadHistory: vi.fn(),
      resetStreamIdleTimer: vi.fn(),
      resetStreamLiveTurnState: vi.fn(),
    })

    const outcome = await subscription.subscribeSession()

    expect(outcome).toEqual({ authoritative: false, live: false, backgroundOnly: false })
    warn.mockRestore()
  })

  it('does not let an older same-session snapshot claim authoritative idle', async () => {
    const pendingSnapshots: Array<(value: unknown) => void> = []
    const rpc = {
      waitForConnection: vi.fn(async () => {}),
      call: <T = unknown>() => new Promise<T>((resolve) => {
        pendingSnapshots.push(resolve as (value: unknown) => void)
      }),
    }
    const runStatus = ref<ChatRunStatus>({ status: 'idle', label: '', task: null })
    const lastStreamSeq = ref(0)
    const subscription = useChatSessionSubscription({
      rpc,
      sessionKey: ref('agent:main:webchat:test'),
      lastStreamSeq,
      runStatus,
      isStreaming: ref(false),
      hasActiveInterrupt: ref(false),
      activeStreamTaskId: ref(''),
      activeTaskGroups: ref(new Set<string>()),
      sessionRunStatus: source => ({
        status: String(source?.run_status || 'idle') as ChatRunStatusState,
        label: '',
        task: source?.active_task || null,
      }),
      startStreaming: vi.fn(),
      loadHistory: vi.fn(),
      resetStreamIdleTimer: vi.fn(),
      resetStreamLiveTurnState: vi.fn(),
    })

    const older = subscription.subscribeSession()
    await vi.waitFor(() => expect(pendingSnapshots).toHaveLength(1))
    lastStreamSeq.value = 1
    const newer = subscription.subscribeSession()
    await vi.waitFor(() => expect(pendingSnapshots).toHaveLength(2))

    pendingSnapshots[1]?.({
      subscribed: true,
      run_status: 'running',
      active_task: { task_id: 'task-newer', status: 'running' },
    })
    await expect(newer).resolves.toEqual({
      authoritative: true,
      live: true,
      backgroundOnly: false,
    })
    pendingSnapshots[0]?.({ subscribed: true, run_status: 'idle' })

    await expect(older).resolves.toEqual({
      authoritative: false,
      live: false,
      backgroundOnly: false,
    })
    expect(runStatus.value.status).toBe('running')
  })

  it('reconciles stale replayed task groups with an empty authoritative snapshot', async () => {
    const activeTaskGroups = ref(new Set(['stale-group']))
    const runStatus = ref<ChatRunStatus>({ status: 'running', label: '', task: null })
    const rpc = {
      waitForConnection: vi.fn(async () => {}),
      call: async <T = unknown>() => ({
        subscribed: true,
        run_status: 'cancelled',
        active_task: null,
        active_task_group_ids: [],
        current_stream_seq: 18,
      }) as T,
    }

    const subscription = useChatSessionSubscription({
      rpc,
      sessionKey: ref('agent:main:webchat:test'),
      lastStreamSeq: ref(0),
      runStatus,
      isStreaming: ref(false),
      hasActiveInterrupt: ref(false),
      activeStreamTaskId: ref(''),
      activeTaskGroups,
      sessionRunStatus: source => ({
        status: String(source?.run_status || 'idle') as ChatRunStatusState,
        label: '',
        task: source?.active_task || null,
      }),
      startStreaming: vi.fn(),
      loadHistory: vi.fn(),
      resetStreamIdleTimer: vi.fn(),
      resetStreamLiveTurnState: vi.fn(),
    })

    await subscription.subscribeSession()

    expect(activeTaskGroups.value.size).toBe(0)
    expect(runStatus.value.status).toBe('cancelled')
  })

  it('hydrates active background groups even when the latest parent task is terminal', async () => {
    const activeTaskGroups = ref(new Set<string>())
    const runStatus = ref<ChatRunStatus>({ status: 'idle', label: '', task: null })
    const rpc = {
      waitForConnection: vi.fn(async () => {}),
      call: async <T = unknown>() => ({
        subscribed: true,
        run_status: 'idle',
        active_task: null,
        active_task_group_ids: ['group-live'],
        current_stream_seq: 19,
      }) as T,
    }

    const subscription = useChatSessionSubscription({
      rpc,
      sessionKey: ref('agent:main:webchat:test'),
      lastStreamSeq: ref(0),
      runStatus,
      isStreaming: ref(false),
      hasActiveInterrupt: ref(false),
      activeStreamTaskId: ref(''),
      activeTaskGroups,
      sessionRunStatus: source => ({
        status: String(source?.run_status || 'idle') as ChatRunStatusState,
        label: '',
        task: source?.active_task || null,
      }),
      startStreaming: vi.fn(),
      loadHistory: vi.fn(),
      resetStreamIdleTimer: vi.fn(),
      resetStreamLiveTurnState: vi.fn(),
    })

    const outcome = await subscription.subscribeSession()

    expect([...activeTaskGroups.value]).toEqual(['group-live'])
    expect(runStatus.value.status).toBe('running')
    expect(outcome).toEqual({ authoritative: true, live: true, backgroundOnly: true })
  })

  it('releases pending work when a reconnect later proves the session idle', async () => {
    const onAuthoritativeIdle = vi.fn()
    const rpc = {
      waitForConnection: vi.fn(async () => {}),
      call: vi.fn()
        .mockRejectedValueOnce(new Error('socket closed'))
        .mockResolvedValueOnce({ subscribed: true, run_status: 'idle' }),
    }
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const subscription = useChatSessionSubscription({
      rpc,
      sessionKey: ref('agent:main:webchat:test'),
      lastStreamSeq: ref(0),
      runStatus: ref<ChatRunStatus>({ status: 'idle', label: '', task: null }),
      isStreaming: ref(false),
      hasActiveInterrupt: ref(false),
      activeStreamTaskId: ref(''),
      activeTaskGroups: ref(new Set<string>()),
      sessionRunStatus: () => ({ status: 'idle', label: '', task: null }),
      startStreaming: vi.fn(),
      loadHistory: vi.fn(),
      resetStreamIdleTimer: vi.fn(),
      resetStreamLiveTurnState: vi.fn(),
      onAuthoritativeIdle,
    })

    await subscription.subscribeSession()
    expect(onAuthoritativeIdle).not.toHaveBeenCalled()
    await subscription.subscribeSession()

    expect(onAuthoritativeIdle).toHaveBeenCalledOnce()
    warn.mockRestore()
  })
})
