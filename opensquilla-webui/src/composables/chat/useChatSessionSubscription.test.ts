import { ref } from 'vue'
import { describe, expect, it, vi } from 'vitest'

import { useChatSessionSubscription } from './useChatSessionSubscription'
import { SESSION_PHASE_ATTEMPT_BUDGET_MS } from './sessionBootstrapContract'
import { RpcTimeoutError, type RpcCallOptions } from '@/lib/rpc'
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
  it('keeps mixed-version compatibility when an old Gateway lacks snapshot', async () => {
    const unsupported = Object.assign(new Error('method not found'), {
      code: 'METHOD_NOT_FOUND',
    })
    const call = vi.fn(async (method: string) => {
      if (method === 'sessions.messages.snapshot') throw unsupported
      return {
        subscribed: true,
        run_status: 'idle',
        replay_complete: true,
        current_stream_seq: 9,
      }
    })
    const rpc = {
      waitForConnection: vi.fn(async () => {}),
      call: call as unknown as <T = unknown>(
        method: string,
        params?: Record<string, unknown>,
        options?: RpcCallOptions,
      ) => Promise<T>,
    }
    const subscription = useChatSessionSubscription({
      rpc,
      sessionKey: ref('agent:main:webchat:old-gateway'),
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
      onLiveSnapshot: vi.fn(),
    })

    const outcome = await subscription.subscribeSession()

    expect(outcome.authoritative).toBe(true)
    expect(rpc.call).toHaveBeenNthCalledWith(
      1,
      'sessions.messages.subscribe',
      {
        key: 'agent:main:webchat:old-gateway',
        since_stream_seq: 0,
        fast_ack: true,
      },
    )
    expect(rpc.call).toHaveBeenNthCalledWith(
      2,
      'sessions.messages.snapshot',
      { key: 'agent:main:webchat:old-gateway' },
    )
  })

  it('skips snapshot on the second bounded bootstrap attempt', async () => {
    const now = Date.now()
    const rpc = {
      waitForConnection: vi.fn(async () => {}),
      call: vi.fn().mockResolvedValue({
        subscribed: true,
        run_status: 'idle',
        replay_complete: true,
        current_stream_seq: 0,
      }),
    }
    const subscription = useChatSessionSubscription({
      rpc,
      sessionKey: ref('agent:main:webchat:skip-snapshot'),
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
      onLiveSnapshot: vi.fn(),
    })

    await subscription.subscribeSession({
      generation: 1,
      key: 'agent:main:webchat:skip-snapshot',
      attempt: 1,
      deadlineAt: now + 15_000,
      attemptDeadlineAt: now + 7_000,
      signal: new AbortController().signal,
      skipSnapshot: true,
    })

    expect(rpc.call).toHaveBeenCalledOnce()
    expect(rpc.call).toHaveBeenCalledWith(
      'sessions.messages.subscribe',
      {
        key: 'agent:main:webchat:skip-snapshot',
        since_stream_seq: 0,
        fast_ack: true,
      },
      expect.objectContaining({
        timeoutMs: expect.any(Number),
        timeoutAction: 'reconnect',
      }),
    )
  })

  it('propagates a snapshot timeout to the coordinator instead of subscribing on a blocked socket', async () => {
    const now = Date.now()
    const timeout = new RpcTimeoutError('sessions.messages.snapshot', 3_000)
    const rpc = {
      waitForConnection: vi.fn(async () => {}),
      call: vi.fn().mockRejectedValue(timeout),
    }
    const subscription = useChatSessionSubscription({
      rpc,
      sessionKey: ref('agent:main:webchat:snapshot-timeout'),
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
      onLiveSnapshot: vi.fn(),
    })
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})

    const outcome = await subscription.subscribeSession({
      generation: 1,
      key: 'agent:main:webchat:snapshot-timeout',
      attempt: 0,
      deadlineAt: now + 15_000,
      attemptDeadlineAt: now + 7_000,
      signal: new AbortController().signal,
      skipSnapshot: false,
    })

    expect(outcome).toMatchObject({
      authoritative: false,
      error: timeout,
      cancelled: false,
    })
    expect(rpc.call).toHaveBeenCalledTimes(2)
    expect(rpc.call).toHaveBeenNthCalledWith(
      1,
      'sessions.messages.subscribe',
      expect.any(Object),
      expect.any(Object),
    )
    expect(rpc.call).toHaveBeenNthCalledWith(
      2,
      'sessions.messages.snapshot',
      expect.any(Object),
      expect.any(Object),
    )
    warn.mockRestore()
  })

  it('restores the compact live snapshot before subscribing from its cursor', async () => {
    const onLiveSnapshot = vi.fn()
    const rpc = {
      waitForConnection: vi.fn(async () => {}),
      call: vi.fn(async <T = unknown>(method: string) => {
        if (method === 'sessions.messages.snapshot') {
          return {
            key: 'agent:main:webchat:resume',
            task_id: 'task-resume',
            current_stream_seq: 2400,
            events: [
              {
                event: 'session.event.thinking',
                payload: {
                  task_id: 'task-resume',
                  text: 'Recovered reasoning',
                  stream_seq: 10,
                },
              },
            ],
          } as T
        }
        return {
          subscribed: true,
          run_status: 'running',
          active_task: { task_id: 'task-resume', status: 'running' },
          current_stream_seq: 2402,
          replay_complete: true,
        } as T
      }) as unknown as <T = unknown>(
        method: string,
        params?: Record<string, unknown>,
      ) => Promise<T>,
    }
    const lastStreamSeq = ref(0)
    const subscription = useChatSessionSubscription({
      rpc,
      sessionKey: ref('agent:main:webchat:resume'),
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
      onLiveSnapshot,
    })

    await subscription.subscribeSession()

    expect(rpc.call).toHaveBeenNthCalledWith(1, 'sessions.messages.subscribe', {
      key: 'agent:main:webchat:resume',
      since_stream_seq: 0,
      fast_ack: true,
    })
    expect(rpc.call).toHaveBeenNthCalledWith(2, 'sessions.messages.snapshot', {
      key: 'agent:main:webchat:resume',
    })
    expect(onLiveSnapshot).toHaveBeenCalledWith({
      key: 'agent:main:webchat:resume',
      task_id: 'task-resume',
      current_stream_seq: 2400,
      events: [
        {
          event: 'session.event.thinking',
          payload: {
            task_id: 'task-resume',
            text: 'Recovered reasoning',
            stream_seq: 10,
          },
        },
      ],
    })
    expect(lastStreamSeq.value).toBe(2402)
  })

  it('hydrates the authoritative run-mode lock from the subscription snapshot', async () => {
    const onRunModeLock = vi.fn()
    const rpc = {
      waitForConnection: vi.fn(async () => {}),
      call: async <T = unknown>() => ({
        subscribed: true,
        run_status: 'running',
        active_task: { task_id: 'task-mode', status: 'running' },
        active_task_group_ids: [],
        run_mode_lock: {
          locked: true,
          runMode: 'standard',
          source: 'task',
        },
        current_stream_seq: 1,
        replay_complete: true,
      }) as T,
    }
    const subscription = useChatSessionSubscription({
      rpc,
      sessionKey: ref('agent:main:webchat:mode-lock'),
      lastStreamSeq: ref(0),
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
      onRunModeLock,
    })

    await subscription.subscribeSession()

    expect(onRunModeLock).toHaveBeenCalledWith({
      locked: true,
      runMode: 'standard',
      source: 'task',
    })
  })

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
  it('exposes the authoritative subscribe snapshot to feature hydrators', async () => {
    const onSnapshot = vi.fn()
    const snapshot = {
      subscribed: true,
      key: 'agent:main:webchat:test',
      run_status: 'idle',
      current_stream_seq: 0,
      collaboration: { mode: 'plan', revision: 2 },
    }
    const subscription = useChatSessionSubscription({
      rpc: {
        waitForConnection: vi.fn().mockResolvedValue(undefined),
        call: vi.fn().mockResolvedValue(snapshot),
      },
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
      onSnapshot,
    })

    await subscription.subscribeSession()

    expect(onSnapshot).toHaveBeenCalledWith(snapshot)
  })

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

    expect(outcome).toMatchObject({
      authoritative: false,
      live: false,
      backgroundOnly: false,
      cancelled: false,
      error: expect.any(Error),
    })
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

    await expect(older).resolves.toMatchObject({
      authoritative: false,
      live: false,
      backgroundOnly: false,
      cancelled: true,
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

  it('forwards authoritative project metadata with its own resolution generation', async () => {
    const beginSessionMetadataResolution = vi.fn(() => 41)
    const onSessionMetadata = vi.fn()
    const rpc = {
      waitForConnection: vi.fn(async () => {}),
      call: vi.fn().mockResolvedValue({
        subscribed: true,
        run_status: 'idle',
        workspaceId: 'project-a',
        projectWorkspace: {
          id: 'project-a',
          name: 'Project A',
          path: '/repos/a',
          available: true,
          removed: false,
        },
      }),
    }
    const subscription = useChatSessionSubscription({
      rpc,
      sessionKey: ref('agent:main:webchat:project-a'),
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
      beginSessionMetadataResolution,
      onSessionMetadata,
    })

    await subscription.subscribeSession()

    expect(beginSessionMetadataResolution).toHaveBeenCalledWith(
      'agent:main:webchat:project-a',
    )
    expect(onSessionMetadata).toHaveBeenCalledWith(
      'agent:main:webchat:project-a',
      41,
      expect.objectContaining({ workspaceId: 'project-a' }),
    )
  })

  it('reports only the current subscription metadata failure', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const sessionKey = ref('agent:main:webchat:first')
    const pending: Array<(reason: unknown) => void> = []
    let generation = 0
    const onSessionMetadataError = vi.fn()
    const rpc = {
      waitForConnection: vi.fn(async () => {}),
      call: <T = unknown>() => new Promise<T>((_resolve, reject) => {
        pending.push(reject)
      }),
    }
    const subscription = useChatSessionSubscription({
      rpc,
      sessionKey,
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
      beginSessionMetadataResolution: () => ++generation,
      onSessionMetadataError,
    })

    const stale = subscription.subscribeSession()
    await vi.waitFor(() => expect(pending).toHaveLength(1))
    sessionKey.value = 'agent:main:webchat:second'
    const current = subscription.subscribeSession()
    await vi.waitFor(() => expect(pending).toHaveLength(2))
    pending[0]?.(new Error('stale failure'))
    pending[1]?.(new Error('current failure'))
    await Promise.all([stale, current])

    expect(onSessionMetadataError).toHaveBeenCalledOnce()
    expect(onSessionMetadataError).toHaveBeenCalledWith(
      'agent:main:webchat:second',
      2,
    )
    warn.mockRestore()
  })

  it('keeps fast ACK defaults non-authoritative and hydrates project metadata separately', async () => {
    const onSnapshot = vi.fn()
    const onSessionMetadata = vi.fn()
    const resetStreamLiveTurnState = vi.fn()
    const runStatus = ref<ChatRunStatus>({
      status: 'running',
      label: 'Running',
      task: null,
    })
    let resolveHistory!: () => void
    const historyTerminal = new Promise<void>(resolve => {
      resolveHistory = resolve
    })
    const markLiveSubscribeSent = vi.fn()
    const rpc = {
      waitForConnection: vi.fn(async () => {}),
      call: async <T = unknown>(method: string) => {
        if (method === 'sessions.messages.subscribe') {
          return {
            subscribed: true,
            hydration_complete: false,
            deferred_fields: ['workspaceId', 'projectWorkspace', 'run_status'],
            projectWorkspaceDeferred: true,
            replay_complete: true,
            current_stream_seq: 12,
            run_status: 'idle',
          } as T
        }
        if (method === 'sessions.messages.hydrate') {
          return {
            hydration_complete: true,
            workspaceId: 'project-deferred',
            projectWorkspaceDeferred: true,
            run_status: 'running',
            active_task: { task_id: 'active-task', status: 'running' },
          } as T
        }
        throw new Error(`Unexpected method: ${method}`)
      },
    }
    const subscription = useChatSessionSubscription({
      rpc,
      sessionKey: ref('agent:main:webchat:fast-ack'),
      lastStreamSeq: ref(0),
      runStatus,
      isStreaming: ref(true),
      hasActiveInterrupt: ref(false),
      activeStreamTaskId: ref('active-task'),
      activeTaskGroups: ref(new Set<string>()),
      sessionRunStatus: source => ({
        status: source?.run_status === 'running' ? 'running' : 'idle',
        label: source?.run_status === 'running' ? 'Running' : 'Idle',
        task: null,
      }),
      startStreaming: vi.fn(),
      loadHistory: vi.fn(),
      resetStreamIdleTimer: vi.fn(),
      resetStreamLiveTurnState,
      beginSessionMetadataResolution: () => 7,
      onSessionMetadata,
      onSnapshot,
    })

    const now = Date.now()
    const outcome = await subscription.subscribeSession({
      generation: 1,
      key: 'agent:main:webchat:fast-ack',
      attempt: 0,
      deadlineAt: now + 15_000,
      attemptDeadlineAt: now + 7_000,
      signal: new AbortController().signal,
      skipSnapshot: false,
      markLiveSubscribeSent,
      waitForHistoryTerminal: () => historyTerminal,
    })

    expect(markLiveSubscribeSent).toHaveBeenCalledOnce()
    expect(onSessionMetadata).not.toHaveBeenCalled()
    expect(runStatus.value.status).toBe('running')
    resolveHistory()
    await vi.waitFor(() => expect(onSessionMetadata).toHaveBeenCalledOnce())

    expect(outcome.authoritative).toBe(true)
    expect(runStatus.value.status).toBe('running')
    expect(onSnapshot).toHaveBeenCalledOnce()
    expect(resetStreamLiveTurnState).not.toHaveBeenCalled()
    expect(onSessionMetadata).toHaveBeenCalledWith(
      'agent:main:webchat:fast-ack',
      7,
      {
        workspaceId: 'project-deferred',
        projectWorkspace: undefined,
      },
    )
  })

  it('gives deferred metadata a fresh bounded window after history exhausts its budget', async () => {
    let now = 10_000
    const dateNow = vi.spyOn(Date, 'now').mockImplementation(() => now)
    const onSessionMetadata = vi.fn()
    let resolveHistory!: () => void
    const historyTerminal = new Promise<void>(resolve => {
      resolveHistory = resolve
    })
    const call = vi.fn(async <T = unknown>(method: string, _params?: unknown, options?: {
        timeoutMs?: number
      }) => {
        if (method === 'sessions.messages.subscribe') {
          return {
            subscribed: true,
            hydration_complete: false,
            current_stream_seq: 0,
          } as T
        }
        if (method === 'sessions.messages.hydrate') {
          expect(options?.timeoutMs).toBeGreaterThan(0)
          expect(options?.timeoutMs).toBeLessThanOrEqual(
            SESSION_PHASE_ATTEMPT_BUDGET_MS,
          )
          return {
            hydration_complete: true,
            workspaceId: null,
          } as T
        }
        throw new Error(`Unexpected method: ${method}`)
      })
    const rpc = {
      waitForConnection: vi.fn(async () => {}),
      call: call as unknown as <T = unknown>(
        method: string,
        params?: Record<string, unknown>,
        options?: RpcCallOptions,
      ) => Promise<T>,
    }
    const subscription = useChatSessionSubscription({
      rpc,
      sessionKey: ref('agent:main:webchat:expired-history'),
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
      beginSessionMetadataResolution: () => 9,
      onSessionMetadata,
    })

    const outcome = await subscription.subscribeSession({
      generation: 1,
      key: 'agent:main:webchat:expired-history',
      attempt: 1,
      deadlineAt: now + 100,
      attemptDeadlineAt: now + 100,
      signal: new AbortController().signal,
      skipSnapshot: true,
      waitForHistoryTerminal: () => historyTerminal,
    })

    expect(outcome.authoritative).toBe(true)
    now += 101
    resolveHistory()
    await vi.waitFor(() => expect(onSessionMetadata).toHaveBeenCalledOnce())
    expect(call.mock.calls.map(([method]) => method)).toEqual([
      'sessions.messages.subscribe',
      'sessions.messages.hydrate',
    ])
    dateNow.mockRestore()
  })

  it('retries failed session metadata without replacing a healthy live subscription', async () => {
    const beginSessionMetadataResolution = vi.fn(() => 12)
    const onSessionMetadata = vi.fn()
    const rpc = {
      waitForConnection: vi.fn(async () => {}),
      call: vi.fn(async <T = unknown>(method: string) => {
        if (method !== 'sessions.messages.hydrate') {
          throw new Error(`Unexpected method: ${method}`)
        }
        return {
          hydration_complete: true,
          workspaceId: 'project-recovered',
          projectWorkspace: {
            id: 'project-recovered',
            name: 'Recovered',
            path: '/repos/recovered',
            available: true,
            removed: false,
          },
        } as T
      }) as unknown as <T = unknown>(
        method: string,
        params?: Record<string, unknown>,
        options?: RpcCallOptions,
      ) => Promise<T>,
    }
    const subscription = useChatSessionSubscription({
      rpc,
      sessionKey: ref('agent:main:webchat:metadata-retry'),
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
      beginSessionMetadataResolution,
      onSessionMetadata,
    })

    await expect(subscription.retrySessionMetadata()).resolves.toBe(true)

    expect(rpc.waitForConnection).toHaveBeenCalledOnce()
    expect(rpc.call).toHaveBeenCalledOnce()
    expect(rpc.call).toHaveBeenCalledWith(
      'sessions.messages.hydrate',
      { key: 'agent:main:webchat:metadata-retry' },
      expect.objectContaining({
        timeoutAction: 'reconnect',
        abortAction: 'reconnect',
      }),
    )
    expect(onSessionMetadata).toHaveBeenCalledWith(
      'agent:main:webchat:metadata-retry',
      12,
      expect.objectContaining({ workspaceId: 'project-recovered' }),
    )
  })
})
