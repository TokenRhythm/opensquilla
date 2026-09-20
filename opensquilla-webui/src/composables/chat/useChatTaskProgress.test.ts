import { effectScope, ref } from 'vue'
import { afterEach, describe, expect, it } from 'vitest'
import type { SessionReadMetadata } from '@/modules/sessionReadLifecycle'
import { decodeConversationEvent } from '@/adapters/gateway/conversationEventsV4'
import { projectConversationEvent } from '@/adapters/gateway/conversationContentV4'
import { useChatTaskProgress } from './useChatTaskProgress'

const scopes: ReturnType<typeof effectScope>[] = []
afterEach(() => scopes.splice(0).forEach(scope => scope.stop()))
const wireProgress = (revision = 1, text = 'Inspect the project') => ({
  revision, explanation: 'Use the ordinary task.', steps: [{ step: text, status: 'in_progress' }],
})

function setup() {
  const sessionKey = ref('agent:main:progress')
  const currentEpoch = ref(3)
  const activeTaskId = ref('task-a')
  const scope = effectScope()
  scopes.push(scope)
  const api = scope.run(() => useChatTaskProgress({ sessionKey, currentEpoch, activeTaskId }))!
  const snapshot = (fields: Partial<SessionReadMetadata> = {}) => ({
    sessionKey: sessionKey.value, epoch: currentEpoch.value,
    tasks: [], activeTask: null, lastTask: null, ...fields,
  }) as SessionReadMetadata
  return { api, sessionKey, currentEpoch, activeTaskId, snapshot }
}

describe('ordinary task progress', () => {
  it('projects the common event and keeps the real task identity', () => {
    const { api } = setup()
    const event = projectConversationEvent(decodeConversationEvent('session.event.progress', {
      session_key: 'agent:main:progress', epoch: 3, task_id: 'task-a', progress: wireProgress(),
    }))
    expect(event.semanticKind).toBe('execution-progress')
    if (event.kind !== 'known' || event.semanticKind !== 'execution-progress') throw new Error('missing progress')
    api.applyEvent(event.payload)
    expect(api.taskId.value).toBe('task-a')
    expect(api.progress.value).toEqual({
      revision: 1, explanation: 'Use the ordinary task.',
      steps: [{ text: 'Inspect the project', status: 'in_progress' }],
    })
  })

  it('recovers persisted progress on refresh and fences stale snapshots and events', () => {
    const { api, snapshot } = setup()
    api.applySnapshot(snapshot({ tasks: [{ task_id: 'task-a', progress: wireProgress(4) }] }))
    api.applySnapshot(snapshot({ tasks: [{ task_id: 'task-a', progress: wireProgress(2, 'stale') }] }))
    for (const identity of [{ key: 'other', epoch: 3 }, { key: 'agent:main:progress', epoch: 2 }]) {
      api.applyEvent({ ...identity, task_id: 'task-a', progress: { revision: 8, explanation: null, steps: [] } })
    }
    expect(api.progress.value?.revision).toBe(4)
    expect(api.progress.value?.steps[0]?.text).toBe('Inspect the project')
  })

  it.each(['pending', 'in_progress', 'completed'])(
    'clears settled progress even when its steps are %s and ownership has not caught up', (status) => {
      const { api, snapshot } = setup()
      api.applySnapshot(snapshot({
        activeTask: {
          task_id: 'task-a', status: 'running',
          progress: { revision: 1, steps: [{ step: 'Inspect the project', status }] },
        },
      }))
      expect(api.progress.value?.steps[0]?.status).toBe(status)

      api.noteTaskSettled('task-a', 3)
      expect(api.progress.value).toBeNull()
      expect(api.taskId.value).toBe('')

      api.applyEvent({
        key: 'agent:main:progress', epoch: 3, task_id: 'task-a',
        progress: {
          revision: 2, explanation: null,
          steps: [{ text: 'Late progress', status: 'in_progress' }],
        },
      })
      api.applySnapshot(snapshot({ activeTask: { task_id: 'task-a', status: 'running', progress: wireProgress(3) } }))
      expect(api.progress.value).toBeNull()
    },
  )

  it('does not reuse progress after ownership ends or when a new task starts', () => {
    const { api, snapshot, activeTaskId } = setup()
    api.applySnapshot(snapshot({ tasks: [{ task_id: 'task-a', progress: wireProgress(1) }] }))
    activeTaskId.value = ''
    expect(api.taskId.value).toBe('')
    expect(api.progress.value).toBeNull()
    activeTaskId.value = 'task-b'
    expect(api.progress.value).toBeNull()
    api.applySnapshot(snapshot({ tasks: [{ task_id: 'task-b', progress: wireProgress(1, 'Second task') }] }))
    api.noteTaskSettled('task-a', 3)
    expect(api.taskId.value).toBe('task-b')
    expect(api.progress.value?.steps[0]?.text).toBe('Second task')
  })

  it.each(['succeeded', 'failed', 'cancelled', 'timeout', 'abandoned', 'interrupted'])(
    'never restores %s task progress from hydration, including conflicting active copies', (status) => {
      const { api, snapshot, activeTaskId } = setup()
      activeTaskId.value = ''
      api.applySnapshot(snapshot({
        tasks: [{ task_id: 'task-a', status: 'running', progress: wireProgress(4) }],
        activeTask: { task_id: 'task-a', status: 'running', progress: wireProgress(5) },
        lastTask: { task_id: 'task-a', status, progress: wireProgress(6) },
      }))
      expect(api.progress.value).toBeNull()
      activeTaskId.value = 'task-a'
      expect(api.taskId.value).toBe('')
      expect(api.progress.value).toBeNull()
      api.applySnapshot(snapshot({ tasks: [{ task_id: 'task-a', status: 'running', progress: wireProgress(7) }] }))
      expect(api.progress.value).toBeNull()
    },
  )

  it.each(['tasks', 'activeTask', 'lastTask'] as const)(
    'clears existing progress when a terminal task is received in %s', (field) => {
      const { api, snapshot } = setup()
      api.applySnapshot(snapshot({ activeTask: { task_id: 'task-a', status: 'running', progress: wireProgress() } }))
      const terminal = { task_id: 'task-a', status: 'succeeded', progress: wireProgress(2) }
      api.applySnapshot(snapshot({ [field]: field === 'tasks' ? [terminal] : terminal }))
      expect(api.progress.value).toBeNull()
    },
  )

  it.each(['running', 'approval_pending', 'queued'])(
    'restores %s progress when active ownership arrives after metadata hydration', (status) => {
      const { api, snapshot, activeTaskId } = setup()
      activeTaskId.value = ''
      api.applySnapshot(snapshot({ activeTask: { taskId: 'task-a', status, progress: wireProgress() } }))
      expect(api.progress.value).toBeNull()
      activeTaskId.value = 'task-a'
      expect(api.progress.value?.revision).toBe(1)
    },
  )

  it('does not use lastTask as the source of live progress, even without a terminal status', () => {
    const { api, snapshot, activeTaskId } = setup()
    activeTaskId.value = ''
    api.applySnapshot(snapshot({ lastTask: { task_id: 'task-a', progress: wireProgress() } }))
    expect(api.taskId.value).toBe('')
    activeTaskId.value = 'task-a'
    expect(api.progress.value).toBeNull()
  })

  it('fences stale terminal snapshots and events and resets progress and settlement on session changes', () => {
    const { api, snapshot, currentEpoch, sessionKey } = setup()
    api.applySnapshot(snapshot({ activeTask: { task_id: 'task-a', status: 'running', progress: wireProgress() } }))
    api.noteTaskSettled('task-a', 2)
    api.applySnapshot(snapshot({ epoch: 2, lastTask: { task_id: 'task-a', status: 'succeeded' } }))
    api.applySnapshot(snapshot({ sessionKey: 'other', lastTask: { task_id: 'task-a', status: 'succeeded' } }))
    expect(api.progress.value?.revision).toBe(1)

    api.noteTaskSettled('task-a', 3)
    expect(api.progress.value).toBeNull()
    currentEpoch.value = 4
    api.applySnapshot(snapshot({ activeTask: { task_id: 'task-a', status: 'running', progress: wireProgress(2) } }))
    expect(api.progress.value?.revision).toBe(2)
    api.noteTaskSettled('task-a', 3)
    expect(api.progress.value?.revision).toBe(2)

    api.noteTaskSettled('task-a', 4)
    sessionKey.value = 'agent:main:other'
    api.applySnapshot(snapshot({ activeTask: { task_id: 'task-a', status: 'running', progress: wireProgress(3) } }))
    expect(api.progress.value?.revision).toBe(3)
    currentEpoch.value = 5
    expect(api.progress.value).toBeNull()
  })

  it('ignores malformed progress without replacing the last valid list', () => {
    const { api, snapshot } = setup()
    api.applySnapshot(snapshot({ tasks: [{ task_id: 'task-a', progress: wireProgress() }] }))
    for (const progress of [wireProgress(0), wireProgress(1.5), { revision: 2, steps: [{ step: 'invalid', status: 'done' }] }]) {
      api.applySnapshot(snapshot({ tasks: [{ task_id: 'task-a', progress }] }))
    }
    expect(api.progress.value?.revision).toBe(1)
  })
})
