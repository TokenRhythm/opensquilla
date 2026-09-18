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

  it('does not reuse previous progress for a new task or let late terminal events resurrect it', () => {
    const { api, snapshot, activeTaskId } = setup()
    api.applySnapshot(snapshot({ tasks: [{ task_id: 'task-a', progress: wireProgress(1) }] }))
    api.noteTaskSettled('task-a', 3)
    api.applyEvent({ key: 'agent:main:progress', epoch: 3, task_id: 'task-a', progress: { revision: 2, explanation: null, steps: [] } })
    expect(api.progress.value?.revision).toBe(1)
    activeTaskId.value = 'task-b'
    expect(api.progress.value).toBeNull()
    api.applySnapshot(snapshot({ tasks: [{ task_id: 'task-b', progress: wireProgress(1, 'Second task') }] }))
    activeTaskId.value = ''
    expect(api.taskId.value).toBe('task-b')
    expect(api.progress.value?.steps[0]?.text).toBe('Second task')
  })

  it('restores the latest completed task and resets on a new session generation', () => {
    const { api, snapshot, activeTaskId, currentEpoch } = setup()
    activeTaskId.value = ''
    currentEpoch.value = 4
    api.applySnapshot(snapshot({ lastTask: { task_id: 'task-completed', progress: wireProgress() } }))
    expect(api.taskId.value).toBe('task-completed')
    expect(api.progress.value?.revision).toBe(1)
    currentEpoch.value = 5
    expect(api.progress.value).toBeNull()
    api.applySnapshot(snapshot({ epoch: 4, lastTask: { task_id: 'task-completed', progress: wireProgress(8) } }))
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
