// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, ref, type App } from 'vue'
import i18n from '@/i18n'
import zhHans from '@/locales/zh-Hans.json'
import type { DeliverySnapshot } from '@/modules/delivery'
import ChatDeliveryRecoveryStatus from './ChatDeliveryRecoveryStatus.vue'

const apps: App<Element>[] = []
afterEach(() => {
  apps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
  i18n.global.locale.value = 'en'
})

it('stays absent for healthy sends and offers a quiet check only while delivery is unresolved', async () => {
  const entries = ref<DeliverySnapshot[]>([])
  const onCheck = vi.fn()
  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp({ setup: () => () => h(ChatDeliveryRecoveryStatus, {
    entries: entries.value, checkingIds: [], onCheck,
  }) })
  app.use(i18n); app.mount(host); apps.push(app)
  expect(host.children).toHaveLength(0)

  entries.value = [{ id: 'original', sessionKey: 'session', phase: 'unknown',
    isCurrentIdentity: true, stopPending: false, waitReason: 'budget', preview: 'Original message' }]
  await nextTick()
  expect(host.textContent).toContain('Automatic checks have paused')
  expect(host.querySelector('[role="alert"]')).toBeNull()
  expect(onCheck).not.toHaveBeenCalled()
  host.querySelector<HTMLButtonElement>('button')!.click()
  expect(onCheck).toHaveBeenCalledExactlyOnceWith('original')

  entries.value = []
  await nextTick()
  expect(host.children).toHaveLength(0)
})

it('renders a separate check for each current conversation request and reflects pending actions', async () => {
  i18n.global.setLocaleMessage('zh-Hans', zhHans)
  i18n.global.locale.value = 'zh-Hans'
  const entries = ref<DeliverySnapshot[]>([
    { id: 'first', sessionKey: 'session', phase: 'unknown', isCurrentIdentity: true, stopPending: false, waitReason: 'budget', preview: '第一条消息' },
    { id: 'second', sessionKey: 'session', phase: 'unknown', isCurrentIdentity: true, stopPending: false, waitReason: 'permission', preview: '第二条消息' },
  ])
  const checkingIds = ref<string[]>([])
  const onCheck = vi.fn((id: string) => { checkingIds.value = [id] })
  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp({ setup: () => () => h(ChatDeliveryRecoveryStatus, { entries: entries.value, checkingIds: checkingIds.value, onCheck }) })
  app.use(i18n); app.mount(host); apps.push(app)
  await nextTick()
  expect(host.textContent).toContain('消息状态待确认')
  expect(host.textContent).toContain('第一条消息')
  expect(host.textContent).toContain('第二条消息')
  const buttons = [...host.querySelectorAll<HTMLButtonElement>('[data-testid="chat-delivery-recovery-check"]')]
  expect(buttons).toHaveLength(2)
  expect(buttons[0]?.textContent).toContain('检查发送状态')
  buttons[0]!.click()
  await nextTick()
  expect(onCheck).toHaveBeenCalledExactlyOnceWith('first')
  expect(buttons[0]!.disabled).toBe(true)
  expect(buttons[0]!.getAttribute('aria-busy')).toBe('true')
  expect(buttons[1]!.disabled).toBe(false)
  entries.value = []
  await nextTick()
  expect(host.querySelector('[data-testid="chat-delivery-recovery"]')).toBeNull()
  expect(host.textContent).not.toContain('第一条消息')
})
