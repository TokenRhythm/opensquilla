// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import * as Vue from 'vue'
import { compile } from '@vue/compiler-dom'
import { createPinia } from 'pinia'
import i18n from '@/i18n'
import ActivityDisclosure from '@/components/chat/ActivityDisclosure.vue'
import AssistantMessage from '@/components/chat/AssistantMessage.vue'
import UnifiedAssistantActivityTimeline from '@/components/chat/UnifiedAssistantActivityTimeline.vue'
import { useChatStream } from '@/composables/chat/useChatStream'
import { useChatRenderedMessages } from '@/composables/chat/useChatRenderedMessages'
import { useChatToolDisclosureScope } from '@/composables/chat/useChatToolDisclosureScope'
import { useToolDetailPreference } from '@/composables/useToolDetailPreference'
import { clearAssistantActivityExpansionState } from '@/utils/chat/activityDisclosureState'
import { projectAssistantActivityTimeline } from '@/utils/chat/assistantActivity'
import { FINISHED_STREAM_TASK_ID, PENDING_STREAM_TASK_ID } from '@/utils/chat/streamEvents'
import type { ChatMessage, ChatRunStatus } from '@/types/chat'
import chatViewSource from './ChatView.vue?raw'

// Compile the actual live disclosure opening tag. A manually reconstructed
// props object would miss the wiring omission that caused this regression.
const liveOpening = chatViewSource.match(/<ActivityDisclosure\b[^>]*>/)?.[0]
if (!liveOpening) throw new Error('ChatView live activity disclosure is missing')
const liveRender = new Function('Vue', compile(
  `${liveOpening}<span>Live process</span></ActivityDisclosure>`, { mode: 'function', prefixIdentifiers: true },
).code)(Vue) as Vue.RenderFunction
const cleanups: Array<() => void> = []

function mountTurn(initialTaskId = 'accepted-turn', seed?: (stream: ReturnType<typeof useChatStream>) => void, finalText = 'Ready.') {
  const effects = Vue.effectScope()
  const api = effects.run(() => {
    const sessionKey = Vue.ref('session-a')
    const activeTaskId = Vue.ref(initialTaskId)
    const messages = Vue.ref<ChatMessage[]>([
      { role: 'user', text: 'Work on this', ts: 1, clientId: 'local-user',
        ...(initialTaskId === PENDING_STREAM_TASK_ID ? {} : { turnId: initialTaskId }) },
    ])
    let toolStateScope: Readonly<Vue.Ref<string>> = Vue.ref('')
    const stream = useChatStream({
      messages, lastHeaderRole: Vue.ref(''), aborted: Vue.ref(false), autoScroll: Vue.ref(false),
      runStatus: Vue.ref<ChatRunStatus>({ status: 'running', label: '', task: null }),
      applySessionRunState: () => {}, renderMarkdown: text => text,
      stripDirectiveTags: text => text, stripGeneratedArtifactMarkers: text => text,
      scrollToBottom: () => {}, toolStateScope: () => toolStateScope.value,
    })
    const { renderedMessages } = useChatRenderedMessages({
      messages, sessionKey, routerSlots: Vue.ref([]), routerModels: Vue.ref({}), routerTierConfigs: Vue.ref({}),
      routerVisualEffectsEnabled: Vue.ref(false), routerVisualMode: Vue.ref('real_candidates'),
      renderMarkdown: text => text, stripGeneratedArtifactMarkers: text => text,
      stripTimePrefix: text => text, isSubagentCompletionMessage: () => false,
    })
    const disclosure = useChatToolDisclosureScope({
      sessionKey, activeTaskId, messages, renderedMessages, isStreaming: stream.isStreaming,
      migrateScope: stream.migrateToolDisclosureScope,
    })
    toolStateScope = disclosure.stateScope
    const LiveTurn = Vue.defineComponent({
      components: { ActivityDisclosure },
      setup: () => ({
        presentedLive: Vue.computed(() => ({
          liveActivityContinuityKey: disclosure.activityContinuityKey.value,
          liveActivityExecuting: false, liveActivityStepCount: 1,
          liveActivityFailureCount: 0, liveActivityPhaseLabel: 'Working', streamTurnElapsed: '',
          liveActivityPhaseAnnouncement: '', liveActivityWaiting: false, streamActivityStale: false,
          queued: false,
        })),
      }),
      render: liveRender,
    })
    const Host = Vue.defineComponent({
      setup: () => () => {
        if (stream.isStreaming.value && stream.streamBubble.value) return Vue.h(LiveTurn)
        const message = renderedMessages.value[renderedMessages.value.length - 1]
        if (!message || message.displayRole !== 'assistant') return null
        return Vue.h(AssistantMessage, {
          message, index: 0, sessionKey: sessionKey.value,
          shareMode: false, shareSelected: false, shareMessageId: 'history',
          renderMarkdown: text => text, fmtTok: String, toolCallGroups: () => [],
          isToolGroupOpen: stream.isToolGroupOpen, isToolItemOpen: stream.isToolItemOpen,
          toolGroupStatusText: () => '', toolStatusText: () => '', toolSecondaryText: () => '',
          copyMessage: async () => true,
          onToggleToolGroup: stream.toggleToolGroup, onToggleToolItem: stream.toggleToolItem,
        })
      },
    })
    stream.startStreaming()
    if (seed) seed(stream)
    else {
      stream.appendToolCall({ id: 'tool-a', name: 'custom_tool' })
      stream.appendToolResult({ id: 'tool-a', name: 'custom_tool', result: 'Tool detail' })
    }
    stream.reconcileFinalText(finalText)
    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = Vue.createApp(Host)
    app.use(i18n)
    app.use(createPinia())
    app.mount(host)
    cleanups.push(() => { app.unmount(); stream.cleanup(); effects.stop(); host.remove() })
    return { host, messages, sessionKey, activeTaskId, stream }
  })!
  return {
    ...api,
    settle(turnId = 'accepted-turn') {
      api.stream.endStreaming()
      // The normal turn-completed handler binds the newly committed message
      // to its accepted task before retiring the live task id.
      api.messages.value[api.messages.value.length - 1]!.turnId = turnId
      api.activeTaskId.value = FINISHED_STREAM_TASK_ID
    },
  }
}

function liveButton(host: HTMLElement) {
  return host.querySelector<HTMLButtonElement>('.assistant-activity__live-head')!
}
function historyButton(host: HTMLElement) {
  return host.querySelector<HTMLButtonElement>('.assistant-activity__summary')!
}
async function chooseOpen(host: HTMLElement, open: boolean) {
  liveButton(host).click()
  await Vue.nextTick()
  if (open) { liveButton(host).click(); await Vue.nextTick() }
}

beforeEach(() => {
  if (!document.doctype) document.insertBefore(document.implementation.createDocumentType('html', '', ''), document.documentElement)
  clearAssistantActivityExpansionState()
  useToolDetailPreference().setMode('collapsed')
  i18n.global.locale.value = 'en'
})
afterEach(() => { while (cleanups.length) cleanups.pop()!() })

describe('ChatView live activity to canonical history handoff', () => {
  it('keeps both pre-tool narrations in place when a completed turn contains a failed tool', async () => {
    const firstNarration = 'I will read the task brief.'
    const secondNarration = 'I will check the checklist and run the command.'
    const finalAnswer = 'The files were checked, but the command failed.'
    const aggregate = firstNarration + secondNarration + finalAnswer
    const api = mountTurn('accepted-turn', stream => {
      const text = (value: string, order: number) => {
        stream.setAcceptedActivityOrder(order)
        stream.appendDelta(value, 'answer')
      }
      const tool = (id: string, name: string, order: number, isError = false) => {
        stream.setAcceptedActivityOrder(order)
        stream.appendToolCall({ id, name, input: { path: `${id}.txt` } })
        stream.appendToolResult({ id, name, result: isError ? 'Command failed' : `${id} contents`, is_error: isError })
      }
      text(firstNarration, 10)
      tool('brief', 'read_file', 11)
      text(secondNarration, 28)
      tool('checklist', 'read_file', 29)
      tool('command', 'exec', 30, true)
      text(finalAnswer, 44)
    }, aggregate)
    await Vue.nextTick()
    await chooseOpen(api.host, true)

    const liveFold = api.stream.foldedTurn.value
    const liveActivity = liveFold.timelineItems.filter(item => item.type !== 'text' || item.rawText !== finalAnswer)
    expect(liveActivity.map(item => item.type === 'text' ? item.rawText : item.type === 'tool-group' ? item.group.calls[0]?.toolId : item.type))
      .toEqual([firstNarration, 'brief', secondNarration, 'checklist', 'command'])

    api.settle()
    await Vue.nextTick()
    const saved = api.messages.value[api.messages.value.length - 1]!
    expect(saved.text).toBe(aggregate)
    expect(saved.interrupted).toBeUndefined()
    expect(saved.tool_calls?.find(call => call.tool_use_id === 'command')).toMatchObject({ is_error: true })
    // A canonical history refresh has not occurred: this is the first local
    // settled render, where the two narrations previously jumped into Answer.
    expect(api.host.querySelector('.assistant-answer')?.textContent).toBe(finalAnswer)
    expect([...api.host.querySelectorAll('[data-activity-entry="true"]')]
      .map(node => Number(node.getAttribute('data-activity-order'))))
      .toEqual([10, 11, 28, 29, 30])
    expect([...api.host.querySelectorAll('.activity-narration')].map(node => node.textContent))
      .toEqual([firstNarration, secondNarration])
    expect(api.host.textContent?.split(firstNarration)).toHaveLength(2)
    expect(api.host.textContent?.split(secondNarration)).toHaveLength(2)
    expect(api.host.querySelectorAll('.tool-row')).toHaveLength(3)
    expect(historyButton(api.host).getAttribute('aria-expanded')).toBe('true')
  })

  it('keeps later tools after their provider wait and reasoning when finalizing a shared tool group', async () => {
    const api = mountTurn('accepted-turn', stream => {
      let reasoningIndex = 0
      const reasoning = (id: string, order: number) => {
        const blockIndex = reasoningIndex++
        stream.appendFrame({ kind: 'thinking', blockId: id, blockIndex, text: id, at: order * 1_000, activityOrder: order })
        stream.appendFrame({ kind: 'thinking-end', blockId: id, blockIndex, at: order * 1_000 + 500, status: 'completed', activityOrder: order })
      }
      const tool = (id: string, name: string, order: number) => {
        stream.setAcceptedActivityOrder(order)
        stream.appendToolCall({ id, name, input: { path: `${id}.txt` } })
        stream.appendToolResult({ id, name, result: `${id} contents` })
      }
      reasoning('reasoning-before-tools', 10)
      tool('brief', 'read_file', 20)
      tool('directory', 'list_dir', 21)
      stream.appendFrame({
        kind: 'status', action: 'provider:rate_limited:20', label: 'Waiting', at: 30_000, activityOrder: 30,
        providerRecovery: { phase: 'retry_wait', reason: 'rate_limited', httpStatus: 429 }, httpStatus: 429,
      })
      stream.appendFrame({
        kind: 'status', action: 'provider:retrying:1:3', label: 'Retrying', at: 40_000, activityOrder: 40,
        providerRecovery: { phase: 'retrying', reason: 'rate_limited', attempt: 1, limit: 3 },
      })
      reasoning('reasoning-before-checklist', 50)
      tool('checklist', 'read_file', 60)
      reasoning('reasoning-before-answer', 70)
      stream.setAcceptedActivityOrder(80)
    })
    await Vue.nextTick()
    await chooseOpen(api.host, true)

    const liveFold = api.stream.foldedTurn.value
    expect(new Set(liveFold.toolCalls.map(call => call.groupId)).size).toBe(1)
    const liveHost = document.createElement('div')
    document.body.appendChild(liveHost)
    const liveApp = Vue.createApp({
      render: () => Vue.h(UnifiedAssistantActivityTimeline, {
        projection: projectAssistantActivityTimeline(liveFold.timelineItems, {
          lifecycle: 'working', statusHistory: liveFold.statusHistory, endedAt: 80_000,
        }),
        timelineItems: liveFold.timelineItems.filter(item => item.type !== 'text'),
        reasoningBlocks: liveFold.reasoningBlocks,
        isToolGroupOpen: () => undefined, isToolItemOpen: () => undefined,
        toolGroupStatusText: () => '', toolStatusText: () => '', toolSecondaryText: () => '',
      }),
    })
    liveApp.use(i18n)
    liveApp.mount(liveHost)
    cleanups.push(() => { liveApp.unmount(); liveHost.remove() })
    await Vue.nextTick()
    const entryOrders = (host: HTMLElement) => [...host.querySelectorAll('[data-activity-entry="true"]')]
      .map(node => Number(node.getAttribute('data-activity-order')))
    const liveOrders = entryOrders(liveHost)
    expect(liveOrders).toEqual([10, 20, 21, 30, 40, 50, 60, 70])

    api.settle()
    await Vue.nextTick()
    expect(entryOrders(api.host)).toEqual(liveOrders)
    expect(api.messages.value[api.messages.value.length - 1]?.tool_calls?.map(call => call.activityOrder)).toEqual([20, 21, 60])
    expect(historyButton(api.host).getAttribute('aria-expanded')).toBe('true')
    expect([...api.host.querySelectorAll('.tool-row')].map(row => row.getAttribute('aria-expanded')))
      .toEqual(['false', 'false', 'false'])
  })

  it.each([undefined, false, true])('preserves manual %s while untouched history stays collapsed', async choice => {
    const api = mountTurn()
    await Vue.nextTick()
    expect(liveButton(api.host).getAttribute('aria-expanded')).toBe('true')
    if (choice !== undefined) await chooseOpen(api.host, choice)
    api.settle()
    await Vue.nextTick()
    expect(historyButton(api.host).getAttribute('aria-expanded')).toBe(String(choice === true))
    expect(api.host.querySelector('.tool-row')?.getAttribute('aria-expanded')).toBe('false')
  })

  it.each(['task-first', 'message-first', 'completion-first'])('keeps an optimistic explicit reopen when identity arrives %s', async order => {
    const api = mountTurn(PENDING_STREAM_TASK_ID)
    await Vue.nextTick()
    await chooseOpen(api.host, true)
    if (order !== 'completion-first') {
      if (order === 'message-first') api.messages.value[0]!.turnId = 'accepted-turn'
      api.activeTaskId.value = 'accepted-turn'
      api.messages.value[0]!.turnId = 'accepted-turn'
      await Vue.nextTick()
      expect(liveButton(api.host).getAttribute('aria-expanded')).toBe('true')
    }
    api.settle()
    await Vue.nextTick()
    expect(historyButton(api.host).getAttribute('aria-expanded')).toBe('true')
  })

  it('keeps a manual reopen when completion lands before the next render', async () => {
    const api = mountTurn()
    await Vue.nextTick()
    await chooseOpen(api.host, false)
    liveButton(api.host).click()
    api.settle()
    await Vue.nextTick()
    expect(historyButton(api.host).getAttribute('aria-expanded')).toBe('true')
  })

  it('isolates the manual choice from another session and another turn', async () => {
    const api = mountTurn()
    await Vue.nextTick()
    await chooseOpen(api.host, true)
    api.settle()
    await Vue.nextTick()
    expect(historyButton(api.host).getAttribute('aria-expanded')).toBe('true')
    api.sessionKey.value = 'session-b'
    await Vue.nextTick()
    expect(historyButton(api.host).getAttribute('aria-expanded')).toBe('false')
    api.sessionKey.value = 'session-a'
    await Vue.nextTick()
    expect(historyButton(api.host).getAttribute('aria-expanded')).toBe('true')
    api.messages.value[api.messages.value.length - 1]!.turnId = 'next-turn'
    await Vue.nextTick()
    expect(historyButton(api.host).getAttribute('aria-expanded')).toBe('false')
  })
})
