import { describe, expect, it } from 'vitest'

import {
  createSessionTaskAttentionStore,
  SESSION_TASK_ATTENTION_STORAGE_KEY,
} from './useSessionTaskAttention'

class MemoryStorage {
  private values = new Map<string, string>()

  getItem(key: string): string | null {
    return this.values.get(key) ?? null
  }

  setItem(key: string, value: string) {
    this.values.set(key, value)
  }
}

const BACKGROUND_CONTEXT = {
  currentSessionKey: 'agent:main:webchat:current',
  currentSessionVisible: true,
}

function terminalPayload(
  key: string,
  taskId: string,
  status: string,
): Record<string, unknown> {
  return {
    key,
    reason: 'task_terminal',
    status,
    last_task: {
      task_id: taskId,
      status,
    },
  }
}

describe('useSessionTaskAttention', () => {
  it('folds queued and running sessions into the same running attention state', () => {
    const store = createSessionTaskAttentionStore(null)

    expect(store.attentionFor('session-a', 'queued')).toBe('running')
    expect(store.attentionFor('session-a', 'running')).toBe('running')
    expect(store.attentionFor('session-a', 'idle')).toBe('none')
  })

  it('marks successful background tasks as completed and persists them', () => {
    const storage = new MemoryStorage()
    const store = createSessionTaskAttentionStore(storage)

    store.handleSessionsChanged(
      terminalPayload('session-a', 'task-a', 'succeeded'),
      BACKGROUND_CONTEXT,
    )

    expect(store.attentionFor('session-a', 'idle')).toBe('completed')
    const persisted = JSON.parse(
      storage.getItem(SESSION_TASK_ATTENTION_STORAGE_KEY) || '{}',
    )
    expect(persisted.unread['session-a']).toEqual({
      taskId: 'task-a',
      state: 'completed',
    })
    expect(createSessionTaskAttentionStore(storage).attentionFor('session-a', 'idle'))
      .toBe('completed')
  })

  it.each(['failed', 'timeout', 'abandoned', 'interrupted'])(
    'uses the quiet failed state for %s background tasks',
    status => {
      const store = createSessionTaskAttentionStore(null)

      store.handleSessionsChanged(
        terminalPayload('session-a', `task-${status}`, status),
        BACKGROUND_CONTEXT,
      )

      expect(store.attentionFor('session-a', status)).toBe('failed')
    },
  )

  it('does not create unread attention for user-cancelled tasks', () => {
    const store = createSessionTaskAttentionStore(null)

    store.handleSessionsChanged(
      terminalPayload('session-a', 'task-cancelled', 'cancelled'),
      BACKGROUND_CONTEXT,
    )

    expect(store.attentionFor('session-a', 'cancelled')).toBe('none')
  })

  it('does not mark a terminal task unread while its visible session is open', () => {
    const storage = new MemoryStorage()
    const payload = terminalPayload('session-a', 'task-a', 'succeeded')
    const store = createSessionTaskAttentionStore(storage)

    store.handleSessionsChanged(
      payload,
      {
        currentSessionKey: 'session-a',
        currentSessionVisible: true,
      },
    )

    expect(store.attentionFor('session-a', 'idle')).toBe('none')

    const reloadedStore = createSessionTaskAttentionStore(storage)
    reloadedStore.handleSessionsChanged(payload, BACKGROUND_CONTEXT)
    expect(reloadedStore.attentionFor('session-a', 'idle')).toBe('none')
  })

  it('marks a current task unread when its tab is hidden or unfocused', () => {
    const store = createSessionTaskAttentionStore(null)

    store.handleSessionsChanged(
      terminalPayload('session-a', 'task-a', 'succeeded'),
      {
        currentSessionKey: 'session-a',
        currentSessionVisible: false,
      },
    )

    expect(store.attentionFor('session-a', 'idle')).toBe('completed')
  })

  it('clears attention on read or when the next task starts', () => {
    const storage = new MemoryStorage()
    const store = createSessionTaskAttentionStore(storage)
    store.handleSessionsChanged(
      terminalPayload('session-a', 'task-a', 'succeeded'),
      BACKGROUND_CONTEXT,
    )

    store.markRead('session-a')
    expect(store.attentionFor('session-a', 'idle')).toBe('none')

    store.handleSessionsChanged(
      terminalPayload('session-a', 'task-b', 'failed'),
      BACKGROUND_CONTEXT,
    )
    store.handleSessionsChanged(
      { key: 'session-a', reason: 'task_running' },
      BACKGROUND_CONTEXT,
    )
    expect(store.attentionFor('session-a', 'idle')).toBe('none')
  })

  it('does not relight a read task when a duplicate terminal event arrives', () => {
    const store = createSessionTaskAttentionStore(null)
    const payload = terminalPayload('session-a', 'task-a', 'succeeded')
    store.handleSessionsChanged(payload, BACKGROUND_CONTEXT)
    store.markRead('session-a')

    store.handleSessionsChanged(payload, BACKGROUND_CONTEXT)

    expect(store.attentionFor('session-a', 'idle')).toBe('none')
  })

  it('does not relight a read task after reload when its terminal event is replayed', () => {
    const storage = new MemoryStorage()
    const payload = terminalPayload('session-a', 'task-a', 'succeeded')
    const store = createSessionTaskAttentionStore(storage)
    store.handleSessionsChanged(payload, BACKGROUND_CONTEXT)
    store.markRead('session-a')
    const persisted = JSON.parse(
      storage.getItem(SESSION_TASK_ATTENTION_STORAGE_KEY) || '{}',
    )
    expect(persisted.read['session-a']).toBe('task-a')

    const reloadedStore = createSessionTaskAttentionStore(storage)
    reloadedStore.handleSessionsChanged(payload, BACKGROUND_CONTEXT)

    expect(reloadedStore.attentionFor('session-a', 'idle')).toBe('none')
  })

  it('loads persisted unread state written before read task IDs were added', () => {
    const storage = new MemoryStorage()
    storage.setItem(SESSION_TASK_ATTENTION_STORAGE_KEY, JSON.stringify({
      version: 1,
      unread: {
        'session-a': {
          taskId: 'task-a',
          state: 'completed',
        },
      },
    }))

    const store = createSessionTaskAttentionStore(storage)

    expect(store.attentionFor('session-a', 'idle')).toBe('completed')
  })

  it('removes deleted sessions and ignores corrupt persisted state', () => {
    const storage = new MemoryStorage()
    storage.setItem(SESSION_TASK_ATTENTION_STORAGE_KEY, '{invalid json')
    const store = createSessionTaskAttentionStore(storage)
    expect(store.attentionFor('session-a', 'idle')).toBe('none')

    store.handleSessionsChanged(
      terminalPayload('session-a', 'task-a', 'succeeded'),
      BACKGROUND_CONTEXT,
    )
    store.removeMany(new Set(['session-a']))

    expect(store.attentionFor('session-a', 'idle')).toBe('none')
  })

  it('removes persisted read task IDs when their sessions are deleted', () => {
    const storage = new MemoryStorage()
    const payload = terminalPayload('session-a', 'task-a', 'succeeded')
    const store = createSessionTaskAttentionStore(storage)
    store.handleSessionsChanged(payload, BACKGROUND_CONTEXT)
    store.markRead('session-a')

    store.removeMany(new Set(['session-a']))

    const reloadedStore = createSessionTaskAttentionStore(storage)
    reloadedStore.handleSessionsChanged(payload, BACKGROUND_CONTEXT)
    expect(reloadedStore.attentionFor('session-a', 'idle')).toBe('completed')
  })
})
