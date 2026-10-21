import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { effectScope, ref, type Ref } from 'vue'
import type { ChatMessage, ChatRunStatus } from '@/types/chat'
import {
  FINISHED_STREAM_TASK_ID,
  PENDING_STREAM_TASK_ID,
  STOPPED_STREAM_TASK_ID,
} from '@/utils/chat/streamEvents'
import { useChatRenderedMessages } from './useChatRenderedMessages'
import { useChatStream } from './useChatStream'
import { useChatToolDisclosureScope } from './useChatToolDisclosureScope'
import { projectConversationContent } from '@/adapters/gateway/conversationContentV4'

const cleanups: Array<() => void> = []

function fixture(initialMessages: ChatMessage[], initialTaskId = 'internal-turn') {
  const effects = effectScope()
  const result = effects.run(() => {
    const messages = ref(initialMessages)
    const sessionKey = ref('session-a')
    const activeTaskId = ref(initialTaskId)
    let scope: Readonly<Ref<string>> = ref('')
    const stream = useChatStream({
      messages,
      lastHeaderRole: ref(''),
      aborted: ref(false),
      autoScroll: ref(true),
      runStatus: ref<ChatRunStatus>({ status: 'idle', label: '', task: null }),
      applySessionRunState: () => {},
      renderMarkdown: text => text,
      stripDirectiveTags: text => text,
      stripGeneratedArtifactMarkers: text => text,
      scrollToBottom: () => {},
      toolStateScope: () => scope.value,
    })
    const { renderedMessages } = useChatRenderedMessages({
      messages, sessionKey,
      routerSlots: ref([]), routerModels: ref({}), routerTierConfigs: ref({}),
      routerVisualEffectsEnabled: ref(false), routerVisualMode: ref('real_candidates'),
      renderMarkdown: text => text,
      stripGeneratedArtifactMarkers: text => text,
      stripTimePrefix: text => text,
      isSubagentCompletionMessage: () => false,
    })
    scope = useChatToolDisclosureScope({
      sessionKey, activeTaskId, isStreaming: stream.isStreaming, messages, renderedMessages,
      migrateScope: stream.migrateToolDisclosureScope,
    }).stateScope
    return { messages, sessionKey, activeTaskId, stream, renderedMessages, scope }
  })!
  cleanups.push(() => { result.stream.cleanup(); effects.stop() })
  return result
}

function precedingTurn(): ChatMessage[] {
  return [
    { role: 'user', text: 'Earlier request', ts: 1, clientId: 'old-user', turnId: 'old-turn' },
    { role: 'assistant', text: 'Earlier answer', ts: 2, turnId: 'old-turn' },
  ]
}

beforeEach(() => vi.useFakeTimers())
afterEach(() => {
  while (cleanups.length) cleanups.pop()!()
  vi.useRealTimers()
})

describe('live tool disclosure owning turn', () => {
  it.each([
    [true, true], [false, true], [true, false], [false, false],
  ])('preserves manual %s with hidden user present=%s through settling', (open, hasUser) => {
    const api = fixture([
      ...precedingTurn(),
      ...(hasUser ? [{ role: 'user', text: '', ts: 3, clientId: 'internal-user', turnId: 'internal-turn' }] : []),
    ])
    // The renderer deliberately hides the internal user row: its last visible
    // turn is not the live tool's owner.
    expect(api.renderedMessages.value[api.renderedMessages.value.length - 1]?.turnKey).toBe('turn:old-turn')
    const expectedScope = JSON.stringify(['session-a', 'turn:internal-turn'])
    expect(api.scope.value).toBe(expectedScope)
    api.stream.startStreaming()
    api.stream.appendToolCall({ id: 'shared-tool', name: 'web_search' })
    api.stream.toggleToolItem(`${api.scope.value}:shared-tool`, open)
    api.stream.appendToolResult({ id: 'shared-tool', name: 'web_search', result: 'ok' })
    api.stream.endStreaming()
    // The done handler binds completedTurnId to this exact newly committed row.
    const done = projectConversationContent({ task_id: api.activeTaskId.value }, 'turn-completed')
    api.messages.value[api.messages.value.length - 1]!.turnId = done.completedTurnId
    api.activeTaskId.value = FINISHED_STREAM_TASK_ID
    const settled = api.renderedMessages.value[api.renderedMessages.value.length - 1]!
    expect(settled.turnKey).toBe('turn:internal-turn')
    expect(api.stream.isToolItemOpen(`${JSON.stringify(['session-a', settled.turnKey])}:shared-tool`)).toBe(open)
    expect(api.stream.isToolItemOpen('["session-a","turn:old-turn"]:shared-tool')).toBeUndefined()
    api.activeTaskId.value = 'next-turn'
    expect(api.stream.isToolItemOpen(`${api.scope.value}:shared-tool`)).toBeUndefined()
    api.sessionKey.value = 'session-b'
    api.activeTaskId.value = 'internal-turn'
    expect(api.stream.isToolItemOpen(`${api.scope.value}:shared-tool`)).toBeUndefined()
  })

  it('uses the accepted task before any new visible or raw user anchor arrives', () => {
    const api = fixture(precedingTurn())
    expect(api.scope.value).toBe('["session-a","turn:internal-turn"]')
  })

  it.each([PENDING_STREAM_TASK_ID, FINISHED_STREAM_TASK_ID, STOPPED_STREAM_TASK_ID, ''])(
    'ignores sentinel %s and honors a hidden raw user turn', sentinel => {
      const api = fixture([
        ...precedingTurn(),
        { role: 'user', text: '', ts: 3, turnId: 'internal-turn' },
      ], sentinel)
      expect(api.scope.value).toBe('["session-a","turn:internal-turn"]')
    },
  )

  it.each(['task-first', 'message-first'])('migrates both choices on optimistic adoption: %s', order => {
    const api = fixture([
      ...precedingTurn(),
      { role: 'user', text: 'New request', ts: 3, clientId: 'local-user' },
    ], PENDING_STREAM_TASK_ID)
    api.stream.startStreaming()
    expect(api.scope.value).toBe('["session-a","turn:local-user"]')
    api.stream.toggleToolItem(`${api.scope.value}:opened`, true)
    api.stream.toggleToolItem(`${api.scope.value}:closed`, false)
    if (order === 'message-first') api.messages.value[api.messages.value.length - 1]!.turnId = 'accepted-turn'
    api.activeTaskId.value = 'accepted-turn'
    api.messages.value[api.messages.value.length - 1]!.turnId = 'accepted-turn'
    expect(api.scope.value).toBe('["session-a","turn:accepted-turn"]')
    expect(api.stream.isToolItemOpen(`${api.scope.value}:opened`)).toBe(true)
    expect(api.stream.isToolItemOpen(`${api.scope.value}:closed`)).toBe(false)
  })

  it.each([FINISHED_STREAM_TASK_ID, ''])('does not migrate a completed legacy turn without a new user: %s', taskId => {
    const api = fixture([
      { role: 'user', text: 'Earlier request', ts: 1, clientId: 'old-user' },
      { role: 'assistant', text: 'Earlier answer', ts: 2 },
    ], taskId)
    api.stream.toggleToolItem(`${api.scope.value}:opened`, true)
    api.stream.toggleToolItem(`${api.scope.value}:closed`, false)
    api.stream.startStreaming()
    api.activeTaskId.value = 'internal-turn'
    expect(api.stream.isToolItemOpen(`${api.scope.value}:opened`)).toBeUndefined()
    expect(api.stream.isToolItemOpen(`${api.scope.value}:closed`)).toBeUndefined()
  })

  it('keeps pending choices when completion supplies the first durable turn identity', () => {
    const api = fixture([
      { role: 'user', text: 'New request', ts: 3, clientId: 'local-user' },
    ], PENDING_STREAM_TASK_ID)
    api.stream.startStreaming()
    api.stream.appendToolCall({ id: 'opened', name: 'web_search' })
    api.stream.appendToolCall({ id: 'closed', name: 'web_search' })
    api.stream.toggleToolItem(`${api.scope.value}:opened`, true)
    api.stream.toggleToolItem(`${api.scope.value}:closed`, false)
    api.stream.appendToolResult({ id: 'opened', name: 'web_search', result: 'ok' })
    api.stream.appendToolResult({ id: 'closed', name: 'web_search', result: 'ok' })
    api.stream.endStreaming()
    const done = projectConversationContent({ task_id: 'accepted-turn' }, 'turn-completed')
    api.messages.value[api.messages.value.length - 1]!.turnId = done.completedTurnId
    api.activeTaskId.value = FINISHED_STREAM_TASK_ID
    expect(api.scope.value).toBe('["session-a","turn:accepted-turn"]')
    expect(api.stream.isToolItemOpen(`${api.scope.value}:opened`)).toBe(true)
    expect(api.stream.isToolItemOpen(`${api.scope.value}:closed`)).toBe(false)
  })
})
