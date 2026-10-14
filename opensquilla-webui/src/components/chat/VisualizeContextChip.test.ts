// @vitest-environment happy-dom
import { createApp, h, nextTick, ref, type App } from 'vue'
import { afterEach, expect, it, vi } from 'vitest'
import i18n from '@/i18n'
import VisualizeContextChip from './VisualizeContextChip.vue'
import ChatComposer from './ChatComposer.vue'

const apps: App[] = []
afterEach(() => { apps.splice(0).forEach(app => app.unmount()); document.body.replaceChildren() })

it('shows, inspects and removes context without replacing the user question or hiding a collapsed composer', async () => {
  const context = ref({ id: 1, sessionKey: 'A', documentId: 'doc_one', revisionId: 'rev_one', name: 'choices.html', summary: 'Current selection', data: { selected: ['x', 'y'] } })
  const text = ref('Keep my existing question'), remove = vi.fn()
  const host = document.createElement('div'); document.body.append(host)
  const app = createApp({ render: () => h(ChatComposer, {
    modelValue: text.value, 'onUpdate:modelValue': (next: string) => { text.value = next },
    attachments: [], busySendMode: 'queue', hasSendContent: true, hasContext: true,
    isStreaming: false, canStop: false, isNewLanding: false, placeholder: 'Send', sendButtonTitle: 'Send',
    runMode: 'safe', allowedRunModes: ['safe'], runModeLocked: false, runModeLockMessage: '',
    sessionRoutingMode: 'off', sessionRoutingBusy: false, voiceBusy: false, voiceRecording: false, voiceReady: true,
    collapsed: true,
  }, { context: () => h(VisualizeContextChip, { snapshot: context.value, onRemove: remove }) }) })
  apps.push(app); app.use(i18n); app.mount(host); await nextTick()
  expect(host.querySelector('.chat-composer--collapsed')).toBeNull()
  expect(host.querySelector('textarea')?.value).toBe('Keep my existing question')
  const chip = host.querySelector('[data-testid="visualize-context-chip"]')!
  const details = chip.querySelector<HTMLButtonElement>('button')!
  expect(details.getAttribute('aria-expanded')).toBe('false')
  details.click(); await nextTick()
  expect(chip.querySelector('pre')?.textContent).toContain('"selected"')
  expect(details.getAttribute('aria-expanded')).toBe('true')
  chip.querySelectorAll('button')[1]!.click()
  expect(remove).toHaveBeenCalledOnce()
  expect(text.value).toBe('Keep my existing question')
  context.value = { ...context.value, id: 2, summary: 'New click snapshot' }
  await nextTick()
  expect(chip.querySelector('pre')).toBeNull()
})
