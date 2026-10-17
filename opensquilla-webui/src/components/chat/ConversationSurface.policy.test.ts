// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, reactive, type App } from 'vue'
import { createPinia } from 'pinia'
import i18n from '@/i18n'
import ConversationSurface from './ConversationSurface.vue'
import ChatMessageList from './ChatMessageList.vue'
import ChatComposer from './ChatComposer.vue'
import RouterFxStrip from './RouterFxStrip.vue'
import { MAIN_CONVERSATION_POLICY, SIDE_CONVERSATION_POLICY, type ConversationPolicy } from '@/modules/conversationPolicy'
import type { ChatRenderedMessage } from '@/types/chat'
import type { ChatPart } from '@/types/parts'
import { GATEWAY_ACCESS_KEY } from '@/modules/gatewayAccess'
import { ARTIFACT_WORKBENCH_KEY } from '@/modules/artifactWorkbench'

const apps: App[] = []
const flush = async () => { for (let index = 0; index < 8; index++) await nextTick() }
afterEach(() => { apps.splice(0).forEach(app => app.unmount()); document.body.innerHTML = ''; localStorage.clear() })

const user = (): ChatRenderedMessage => ({
  role: 'user', displayRole: 'user', roleLabel: 'You', messageId: 'question', turnId: 'turn',
  turnKey: 'turn', text: 'Inspect the page', timeStr: '', showHeader: false,
})
const assistant = (overrides: Partial<ChatRenderedMessage> = {}): ChatRenderedMessage => ({
  role: 'assistant', displayRole: 'assistant', roleLabel: 'Assistant', messageId: 'answer',
  turnKey: 'turn', turnId: 'turn', text: 'The page is ready.', timeStr: '', showHeader: false,
  turnOutcome: { turnId: 'turn', status: 'completed' },
  meta: { model: 'test/model', modelShort: 'model', input: 120, output: 40, hasTokens: true,
    cachedTokens: 0, reasoningTokens: 0, costUsd: 0.125, hasSaved: false, savedLabel: '' },
  ...overrides,
})

async function mountSurface(policy: Readonly<ConversationPolicy>, messages: ChatRenderedMessage[]) {
  i18n.global.locale.value = 'en'
  const state = reactive({ messages, running: false, draft: 'next question' })
  const actions = { send: vi.fn(), stop: vi.fn(), fork: vi.fn(), edit: vi.fn(), retry: vi.fn(), resolve: vi.fn(), open: vi.fn() }
  const host = document.createElement('div'); document.body.appendChild(host)
  const app = createApp({ render: () => h(ConversationSurface, { policy }, { default: () => [
    h(ChatMessageList, {
      messages: state.messages, sessionKey: 'session', shareMode: false, selectedMessageIds: new Set<string>(),
      stripTimePrefix: (value: string) => value, renderMarkdown: (value: string) => value,
      fmtTok: String, subagentSummary: (value: string) => value, subagentBody: (value: string) => value,
      toolCallGroups: () => [], isToolGroupOpen: () => false, isToolItemOpen: () => false,
      toolGroupStatusText: () => '', toolStatusText: () => '', toolSecondaryText: () => '',
      copyMessage: async () => true, downloadAttachment: async () => true,
      isStreaming: state.running, workbenchEnabled: true,
      onForkConversation: actions.fork, onEditMessage: actions.edit, onRegenerateMessage: actions.retry,
      onResolveInterrupt: actions.resolve, onOpenArtifact: actions.open,
    }, { 'router-strip': ({ message }: { message: ChatRenderedMessage }) => h(RouterFxStrip, { message }) }),
    h(ChatComposer, {
      modelValue: state.draft, attachments: [], busySendMode: 'queue', hasSendContent: true,
      isStreaming: state.running, canStop: state.running, isNewLanding: false,
      placeholder: 'Send a message', sendButtonTitle: 'Send', runMode: 'full', allowedRunModes: ['safe', 'full'],
      runModeLocked: false, runModeLockMessage: '', sessionRoutingMode: 'squilla_router', sessionRoutingBusy: false,
      goalModeAvailable: true, planModeAvailable: true,
      voiceBusy: false, voiceRecording: false, voiceReady: false, onSend: actions.send, onStop: actions.stop,
    }),
  ] }) }).use(i18n).use(createPinia())
  app.provide(GATEWAY_ACCESS_KEY, { isAvailable: true, isLocalOwner: true } as never)
  app.provide(ARTIFACT_WORKBENCH_KEY, { resources: {}, content: {}, artifacts: {}, documents: {}, previews: {}, subscribeDocumentChanges: () => ({ close() {} }) } as never)
  apps.push(app); app.mount(host); await flush()
  return { host, state, actions }
}

// Leaf policy coverage only. Real routed main/workbench host parity is tested
// through the unmodified App in e2e/conversation-host-parity.spec.ts.
describe('ConversationSurface leaf policy behavior', () => {
  it.each([MAIN_CONVERSATION_POLICY, SIDE_CONVERSATION_POLICY])('shows copy, usage and timestamp only after the current $kind message settles', async policy => {
    const surface = await mountSurface(policy, [user(), assistant({ isStreaming: true, ts: 1_792_138_000_000 })])
    surface.state.running = true; await flush()
    expect(surface.host.querySelector('.msg-ai-text')?.textContent).toContain('The page is ready.')
    expect(surface.host.querySelector('.msg-ai-footer')).toBeNull()
    expect(surface.host.querySelector('.msg-ai-actions')).toBeNull()
    expect(surface.host.querySelector('.msg-meta__more-btn')).toBeNull()
    expect(surface.host.querySelector('.msg-ai .msg-time')).toBeNull()

    surface.state.messages = [user(), assistant({ isStreaming: false, ts: 1_792_138_000_000 })]
    // A later turn may already be running. Receipt visibility belongs to the
    // individual completed row, not to the entire session's busy state.
    await flush()
    expect(surface.host.querySelector('.msg-ai-actions')).not.toBeNull()
    expect(surface.host.querySelector('.msg-ai .msg-time')?.getAttribute('datetime')).toBeTruthy()
    surface.host.querySelector<HTMLButtonElement>('.msg-meta__more-btn')!.click(); await flush()
    expect(surface.host.querySelector('.msg-meta-popover')?.textContent).toContain('$0.125')
    expect(surface.host.querySelector('.msg-meta-popover')?.textContent).toContain('↑120 ↓40')
  })

  it('retains the same answer, usage popover, resource open and routing controls under both policies', async () => {
    const row = assistant({ artifacts: [{ id: 'html', name: 'page.html', mime: 'text/html', download_url: '/html' }] })
    const main = await mountSurface(MAIN_CONVERSATION_POLICY, [user(), row])
    const side = await mountSurface(SIDE_CONVERSATION_POLICY, [user(), row])
    for (const surface of [main, side]) {
      expect(surface.host.querySelector('.msg-ai-text')?.textContent).toContain('The page is ready.')
      surface.host.querySelector<HTMLButtonElement>('.msg-meta__more-btn')!.click(); await flush()
      expect(surface.host.querySelector('.msg-meta-popover')?.textContent).toContain('$0.125')
      expect(surface.host.querySelector('.msg-meta-popover')?.textContent).toContain('↑120 ↓40')
      surface.host.querySelector<HTMLButtonElement>('.msg-artifact-body')!.click(); await flush()
      expect(surface.actions.open).toHaveBeenCalledWith(expect.objectContaining({ id: 'html' }))
      surface.host.querySelector<HTMLButtonElement>('.chat-model-routing-btn')!.click(); await flush()
      expect(document.querySelector('[role="dialog"]')?.textContent).toContain('Models & routing')
      document.querySelector<HTMLButtonElement>('.routing-heading .routing-icon-button')?.click(); await flush()
      surface.host.querySelector<HTMLButtonElement>('.chat-send-btn')!.click()
      expect(surface.actions.send).toHaveBeenCalledOnce()
      surface.state.running = true; await flush()
      surface.host.querySelector<HTMLButtonElement>('.chat-stop-btn')!.click()
      expect(surface.actions.stop).toHaveBeenCalledOnce()
      expect(surface.host.querySelector('.assistant-chat-panel__status')).toBeNull()
    }
    expect(main.host.querySelector('.msg-meta-popover')?.textContent).toBe(side.host.querySelector('.msg-meta-popover')?.textContent)
  })

  it('keeps approval actions enabled while the side policy removes fork, edit, retry, plan and goal controls', async () => {
    const approval: Extract<ChatPart, { type: 'interrupt' }> = {
      type: 'interrupt', key: 'approval', interruptKind: 'approval',
      approval: { approvalId: 'approve-1', toolName: 'execute_code', command: 'echo hello' },
    } as Extract<ChatPart, { type: 'interrupt' }>
    const main = await mountSurface(MAIN_CONVERSATION_POLICY, [user(), assistant({ parts: [approval] })])
    const side = await mountSurface(SIDE_CONVERSATION_POLICY, [user(), assistant({ parts: [approval] })])
    const labels = (host: HTMLElement) => [...host.querySelectorAll('button')].map(button => `${button.title} ${button.getAttribute('aria-label')} ${button.textContent}`)
    expect(labels(main.host).some(label => /Fork/i.test(label))).toBe(true)
    expect(labels(side.host).some(label => /Fork/i.test(label))).toBe(false)
    expect(main.host.querySelector(`.msg-user button[title="${i18n.global.t('chat.edit')}"]`)).not.toBeNull()
    expect(side.host.querySelector(`.msg-user button[title="${i18n.global.t('chat.edit')}"]`)).toBeNull()
    for (const surface of [main, side]) {
      const approve = [...surface.host.querySelectorAll<HTMLButtonElement>('button')].find(button => /Allow once/i.test(button.textContent || ''))
      expect(approve).toBeDefined(); approve!.click(); await flush()
      expect(surface.actions.resolve).toHaveBeenCalledWith('approve-1', 'allow-once')
      surface.host.querySelector<HTMLButtonElement>('.chat-plus-btn')!.click(); await flush()
      const menu = document.querySelector('.composer-add-menu')
      expect(menu?.textContent?.includes('Goal')).toBe(surface === main)
      expect(menu?.textContent?.includes('Plan')).toBe(surface === main)
      surface.host.querySelector<HTMLButtonElement>('.chat-plus-btn')!.click(); await flush()
    }
  })

  it('also denies the safe usage-barrier retry path for side instead of leaving a dead Retry button', async () => {
    const error: ChatRenderedMessage = { role: 'error', displayRole: 'error', roleLabel: 'Error',
      text: 'Usage accounting temporarily unavailable.', timeStr: '', showHeader: true, turnId: 'turn', errorCode: 'usage_accounting_busy',
      turnOutcome: { turnId: 'turn', status: 'failed', usageCallIndex: 1, noPriorProviderDispatch: true, replaySafe: true, userMessageId: 'question' } }
    const main = await mountSurface(MAIN_CONVERSATION_POLICY, [user(), error])
    const side = await mountSurface(SIDE_CONVERSATION_POLICY, [user(), error])
    const retry = (host: HTMLElement) => [...host.querySelectorAll<HTMLButtonElement>('button')].find(button => button.textContent?.trim() === 'Retry')
    expect(retry(main.host)).toBeDefined()
    expect(retry(side.host)).toBeUndefined()
    retry(main.host)!.click(); expect(main.actions.retry).toHaveBeenCalledOnce()
  })
})
