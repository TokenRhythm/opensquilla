// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, type App } from 'vue'
import i18n from '@/i18n'
import zhHans from '@/locales/zh-Hans.json'
import type { ChatSessionRecoveryState } from '@/utils/chat/sessionLoadState'
import ChatSessionRecoveryStatus from './ChatSessionRecoveryStatus.vue'

const apps: App<Element>[] = []

async function mountState(state: ChatSessionRecoveryState, onRetry = vi.fn()) {
  const host = document.createElement('div')
  host.className = 'chat-thread'
  host.tabIndex = 0
  document.body.appendChild(host)
  const app = createApp({
    setup: () => () => h(ChatSessionRecoveryStatus, {
      state,
      onRetry,
    }),
  })
  app.use(i18n)
  app.mount(host)
  apps.push(app)
  await nextTick()
  return { host, onRetry }
}

beforeEach(() => {
  i18n.global.setLocaleMessage('zh-Hans', zhHans)
  i18n.global.locale.value = 'en'
})

afterEach(() => {
  apps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
})

describe('ChatSessionRecoveryStatus', () => {
  it('renders compact localized history progress as a live status', async () => {
    i18n.global.locale.value = 'zh-Hans'
    const { host } = await mountState('history-loading')
    const status = host.querySelector('[data-testid="chat-session-recovery-status"]')

    expect(status?.textContent).toContain('正在恢复会话历史…')
    expect(status?.getAttribute('data-recovery-state')).toBe('history-loading')
    expect(status?.getAttribute('role')).toBe('status')
    expect(status?.getAttribute('aria-live')).toBe('polite')
    expect(status?.getAttribute('aria-atomic')).toBe('true')
    expect(host.querySelector('.chat-session-recovery-status__spinner')).toBeTruthy()
    expect(host.querySelector('button')).toBeNull()
  })

  it('shows a non-blocking history error and emits retry without local latching', async () => {
    const { host, onRetry } = await mountState('history-error')
    const alert = host.querySelector('[data-testid="chat-session-recovery-status"]')
    const retry = host.querySelector('[data-testid="chat-session-recovery-retry"]') as HTMLButtonElement

    expect(alert?.textContent).toContain('Conversation history temporarily unavailable')
    expect(alert?.textContent).toContain(
      'The connection may have been interrupted, or history is temporarily unavailable.',
    )
    expect(alert?.getAttribute('data-recovery-state')).toBe('history-error')
    expect(alert?.getAttribute('role')).toBe('alert')
    expect(alert?.getAttribute('aria-atomic')).toBe('true')
    expect(retry.textContent).toContain('Reload history')

    retry.click()
    await nextTick()
    expect(onRetry).toHaveBeenCalledOnce()
    expect(document.activeElement).toBe(host)
    expect(host.querySelector('[data-testid="chat-session-recovery-retry"]')).toBeTruthy()
  })

  it('renders live degradation independently from history', async () => {
    const { host, onRetry } = await mountState('live-degraded')
    const status = host.querySelector('[data-testid="chat-session-recovery-status"]')
    const retry = host.querySelector('[data-testid="chat-session-recovery-retry"]') as HTMLButtonElement

    expect(status?.textContent).toContain('Live updates are temporarily unavailable')
    expect(status?.textContent).toContain('History remains available.')
    expect(status?.getAttribute('data-recovery-state')).toBe('live-degraded')
    expect(retry.textContent).toContain('Reconnect')
    retry.click()
    expect(onRetry).toHaveBeenCalledOnce()
  })
})
