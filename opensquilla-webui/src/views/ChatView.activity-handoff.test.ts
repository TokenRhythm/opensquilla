// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import * as Vue from 'vue'
import { compile } from '@vue/compiler-dom'
import { createPinia } from 'pinia'
import i18n from '@/i18n'
import ActivityDisclosure from '@/components/chat/ActivityDisclosure.vue'
import AssistantMessage from '@/components/chat/AssistantMessage.vue'
import { useChatStream } from '@/composables/chat/useChatStream'
import { useChatRenderedMessages } from '@/composables/chat/useChatRenderedMessages'
import { useChatToolDisclosureScope } from '@/composables/chat/useChatToolDisclosureScope'
import { useToolDetailPreference } from '@/composables/useToolDetailPreference'
import { clearAssistantActivityExpansionState } from '@/utils/chat/activityDisclosureState'
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

function mountTurn(initialTaskId = 'accepted-turn') {
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
        liveActivityContinuityKey: disclosure.activityContinuityKey,
        liveActivityExecuting: false, executionDockRun: null, liveActivityStepCount: 1,
        liveActivityFailureCount: 0, liveActivityPhaseLabel: 'Working', streamTurnElapsed: '',
        liveActivityElapsedLabel: '', streamActivityStale: false,
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
    stream.appendToolCall({ id: 'tool-a', name: 'custom_tool' })
    stream.appendToolResult({ id: 'tool-a', name: 'custom_tool', result: 'Tool detail' })
    stream.reconcileFinalText('Ready.')
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
