// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, reactive, type App } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import i18n from '@/i18n'
import AssistantChatPanel from './AssistantChatPanel.vue'
import { SESSION_LIFECYCLE_KEY } from '@/modules/sessionLifecycle'
import { SESSION_READ_LIFECYCLE_FACTORY_KEY, type SessionReadLease, type SessionReadMessage } from '@/modules/sessionReadLifecycle'
import { TURN_COMMANDS_KEY } from '@/modules/turnCommands'
import { CONVERSATION_EVENTS_KEY, type ConversationEvent } from '@/modules/conversationEvents'
import type { ConversationEventSourceHandlers } from '@/modules/conversationEventHub'
import { HISTORY_CONTENT_READER_KEY } from '@/modules/historyContent'
import { createHistoryContentReader } from '@/adapters/gateway/historyContentV4'
import { GATEWAY_ACCESS_KEY, type GatewayAccess } from '@/modules/gatewayAccess'

const mounted: App[] = []
afterEach(() => { mounted.splice(0).forEach(app => app.unmount()); document.body.innerHTML = ''; localStorage.clear() })
const flush = async () => { for (let i = 0; i < 20; i++) await nextTick() }

async function mountPanel(messages: readonly SessionReadMessage[] = []) {
  const parent = reactive({ key: `parent-${crypto.randomUUID()}`, workspace: 'workspace' })
  const gateway = reactive({ availability: 'available', isAvailable: true, connectionHealth: 'healthy', connectionPhase: 'healthy', deliveryIdentity: crypto.randomUUID(), subscriptionEpoch: 1 })
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/chat', component: { render: () => null } }] })
  await router.push('/chat')
  const send = vi.fn(async () => ({ ok: true }))
  const cancel = vi.fn(async () => ({ aborted: true }))
  const create = vi.fn(async () => ({ key: 'child', sessionId: 'child-id' }))
  const close = vi.fn(async () => {})
  const history = { messages }
  let handlers: ConversationEventSourceHandlers<ConversationEvent> | undefined
  let sequence = 0
  const contentRead = vi.fn(async (_endpoint: string, _options?: { signal?: AbortSignal }) => ({
    metadata: { status: 200 }, stream: () => new Response('canonical full answer').body,
  }))
  const lease = {
    live: Promise.resolve({ activity: 'idle', snapshot: null }), metadata: Promise.resolve({ workspaceId: 'workspace' }), criticalRequestsQueued: Promise.resolve(),
    retryMetadata: vi.fn(async () => ({ workspaceId: 'workspace', epoch: 1, pendingUserInputs: [], runStatus: 'idle' })),
    reconcile: vi.fn(async () => ({ activity: 'idle', snapshot: null })),
    history: { latest: vi.fn(async () => history) }, close,
  } as unknown as SessionReadLease
  const el = document.createElement('div'); document.body.appendChild(el)
  const app = createApp({ render: () => h(AssistantChatPanel, { parentSessionKey: parent.key, workspaceId: parent.workspace }) })
  app.use(i18n); app.use(router)
  app.provide(GATEWAY_ACCESS_KEY, gateway as unknown as GatewayAccess)
  app.provide(SESSION_LIFECYCLE_KEY, { create, fork: vi.fn(), rename: vi.fn(), remove: vi.fn() })
  app.provide(SESSION_READ_LIFECYCLE_FACTORY_KEY, { create: () => ({ open: () => lease, current: () => lease }) })
  app.provide(TURN_COMMANDS_KEY, { send, cancel, steer: vi.fn(), supports: () => true })
  app.provide(CONVERSATION_EVENTS_KEY, { subscribe: value => { handlers = value; return vi.fn() } })
  app.provide(HISTORY_CONTENT_READER_KEY, createHistoryContentReader({ requestBinary: contentRead }))
  app.mount(el); mounted.push(app); await flush()
  const emit = (semanticKind: string, text?: string) => handlers?.onEvent?.({ kind: 'conversation', event: {
    kind: 'known', semanticKind, sessionKey: 'child', taskId: 'task', turnId: 'turn',
    streamGeneration: 'generation', streamSeq: ++sequence, connectionSeq: null, generationEpoch: null,
    meta: {}, payload: { key: 'child', epoch: 1, assistant_message_id: 'answer', text },
  } } as ConversationEvent)
  return { el, parent, gateway, router, create, send, cancel, close, history, contentRead, emit }
}

describe('AssistantChatPanel', () => {
  it.each(['ready', 'missing-reference', 'preparing', 'oversized'])('labels %s previews and opens their durable conversation', async reason => {
    i18n.global.locale.value = 'en'
    const f = await mountPanel([{ id: 'answer', messageId: 'answer', role: 'assistant', text: 'preview body', previewComplete: false,
      contentAvailability: reason === 'preparing' ? 'preparing' : 'ready',
      contentRef: reason === 'missing-reference' ? undefined : { version: 1, sessionKey: 'child', sessionId: 'physical-child', messageId: 'answer', view: 'display', revision: 'r1', byteLength: reason === 'oversized' ? 1024 * 1024 : 20000 },
    }] as unknown as SessionReadMessage[])
    const input = f.el.querySelector('textarea')!
    input.value = 'hello'; input.dispatchEvent(new Event('input')); await flush()
    f.el.querySelector('form')!.dispatchEvent(new Event('submit', { cancelable: true })); await flush()
    expect(f.contentRead).not.toHaveBeenCalled()
    const preview = f.el.querySelector('[data-testid="assistant-chat-preview"]')!
    expect(preview.textContent).toContain('Showing a limited preview.')
    ;(preview.querySelector('button') as HTMLButtonElement).click()
    await vi.waitFor(() => expect(f.router.currentRoute.value.query.session).toBe('child'))
  })

  it.each([true, false])('uses the existing content reader after commit and labels failed reads (success=%s)', async success => {
    i18n.global.locale.value = 'en'
    const f = await mountPanel()
    const input = f.el.querySelector('textarea')!
    input.value = 'hello'; input.dispatchEvent(new Event('input')); await flush()
    f.el.querySelector('form')!.dispatchEvent(new Event('submit', { cancelable: true })); await flush()
    f.emit('text-delta', 'preview body with the complete streamed suffix')
    f.emit('turn-completed')
    f.history.messages = [{ id: 'answer', messageId: 'answer', role: 'assistant', text: 'preview body', previewComplete: false,
      contentRef: { version: 1, sessionKey: 'child', sessionId: 'physical-child', messageId: 'answer', view: 'display', revision: 'r1', byteLength: 20000 },
    }] as unknown as SessionReadMessage[]
    let finish!: (value: Awaited<ReturnType<typeof f.contentRead>>) => void
    f.contentRead.mockImplementation(async () => new Promise(resolve => { finish = resolve }))
    input.value = 'next draft'; input.dispatchEvent(new Event('input')); await flush()
    f.emit('turn-committed')
    await vi.waitFor(() => expect(f.contentRead).toHaveBeenCalledOnce())
    expect(f.contentRead.mock.calls[0]![0]).toContain('messageId=answer&revision=r1&view=display&export=1')
    expect((f.el.querySelector('button[type="submit"]') as HTMLButtonElement).disabled).toBe(true)
    finish({ metadata: { status: success ? 200 : 503 }, stream: () => new Response('canonical full answer').body })
    await vi.waitFor(() => expect((f.el.querySelector('button[type="submit"]') as HTMLButtonElement).disabled).toBe(false))
    expect(input.value).toBe('next draft')
    if (success) {
      expect(f.el.textContent).toContain('canonical full answer')
      expect(f.el.querySelector('[data-testid="assistant-chat-preview"]')).toBeNull()
    } else {
      expect(f.el.querySelector('[data-testid="assistant-chat-preview"]')?.textContent).toContain('Showing a limited preview.')
      expect(f.el.textContent).not.toContain('complete streamed suffix')
    }
  })

  it('creates only on first send, sends in place, stops the child and offers full conversation', async () => {
    i18n.global.locale.value = 'en'
    const f = await mountPanel()
    expect(f.create).not.toHaveBeenCalled()
    const input = f.el.querySelector('textarea')!
    input.value = 'side question'; input.dispatchEvent(new Event('input'))
    await flush()
    f.el.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
    await flush()
    expect(f.send).toHaveBeenCalledOnce()
    expect(f.router.currentRoute.value.query).toEqual({})
    expect(f.el.textContent).toContain('side question')
    const buttons = () => [...f.el.querySelectorAll('button')]
    buttons().find(button => button.textContent?.trim() === 'Stop')!.click()
    await flush()
    expect(f.cancel).toHaveBeenCalledWith({ sessionKey: 'child', source: 'assistant-chat' }, expect.any(Object))
    buttons().find(button => button.textContent?.trim() === 'Open full conversation')!.click()
    await flush()
    await vi.waitFor(() => expect(f.router.currentRoute.value.query.session).toBe('child'))
  })

  it('retires reads and gates sending on disconnection and parent replacement', async () => {
    i18n.global.locale.value = 'en'
    const f = await mountPanel()
    const input = f.el.querySelector('textarea')!
    input.value = 'hello'; input.dispatchEvent(new Event('input')); await flush()
    f.el.querySelector('form')!.dispatchEvent(new Event('submit', { cancelable: true })); await flush()
    f.gateway.isAvailable = false; await flush()
    expect(f.close).toHaveBeenCalledOnce()
    expect(f.el.querySelector('textarea')!.disabled).toBe(true)
    f.parent.key = 'replacement'; f.gateway.isAvailable = true; await flush()
    expect(f.el.textContent).not.toContain('hello')
    expect(f.create).toHaveBeenCalledOnce()
  })
})
