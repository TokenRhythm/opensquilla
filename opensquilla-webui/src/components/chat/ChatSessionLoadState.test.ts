// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, ref, type App } from 'vue'
import i18n from '@/i18n'
import zhHans from '@/locales/zh-Hans.json'
import ChatSessionLoadState from './ChatSessionLoadState.vue'

const apps: App<Element>[] = []

async function mountState(state: 'loading' | 'error', onRetry = vi.fn()) {
  const host = document.createElement('div')
  host.className = 'chat-thread'
  host.tabIndex = 0
  document.body.appendChild(host)
  const currentState = ref(state)
  const currentSessionKey = ref('agent:main:webchat:session-a')
  const app = createApp({
    setup: () => () => h(ChatSessionLoadState, {
      key: currentSessionKey.value,
      state: currentState.value,
      onRetry,
    }),
  })
  app.use(i18n)
  app.mount(host)
  apps.push(app)
  await nextTick()
  return { currentSessionKey, currentState, host, onRetry }
}

beforeEach(() => {
  i18n.global.setLocaleMessage('zh-Hans', zhHans)
  i18n.global.locale.value = 'en'
})

afterEach(() => {
  apps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
})

describe('ChatSessionLoadState', () => {
  it('renders the localized initial loading state without nesting a live region', async () => {
    i18n.global.locale.value = 'zh-Hans'
    const { host } = await mountState('loading')
    const status = host.querySelector('[data-testid="chat-session-load-state"]')

    expect(status?.textContent).toContain('正在加载会话…')
    expect(status?.textContent).toContain('正在恢复最近消息和会话状态…')
    expect(status?.getAttribute('data-visual-state')).toBe('loading')
    expect(host.querySelector('.chat-session-load-state__panel--loading')).toBeTruthy()
    expect(status?.hasAttribute('role')).toBe(false)
    expect(status?.getAttribute('aria-busy')).toBe('true')
    expect(status?.hasAttribute('aria-live')).toBe(false)
    expect(status?.hasAttribute('aria-atomic')).toBe(false)
    expect(host.querySelector('button')).toBeNull()
  })

  it('shows a recoverable error card and a stable retrying state', async () => {
    const { currentState, host, onRetry } = await mountState('error')
    const alert = host.querySelector('[data-testid="chat-session-load-state"]')
    const retry = host.querySelector('[data-testid="chat-session-load-retry"]') as HTMLButtonElement

    expect(alert?.textContent).toContain('Conversation temporarily unavailable')
    expect(alert?.textContent).toContain(
      'The connection may have been interrupted, or history is temporarily unavailable.',
    )
    expect(alert?.getAttribute('data-visual-state')).toBe('error')
    expect(alert?.getAttribute('role')).toBe('alert')
    expect(alert?.getAttribute('aria-atomic')).toBe('true')
    expect(alert?.hasAttribute('aria-busy')).toBe(false)
    expect(host.querySelector('.chat-session-load-state__panel--error')).toBeTruthy()
    expect(host.querySelector('.chat-session-load-state__icon')?.getAttribute('aria-hidden')).toBe('true')
    expect(retry.textContent).toContain('Reload')

    retry.click()
    await nextTick()
    expect(onRetry).toHaveBeenCalledOnce()
    expect(document.activeElement).toBe(host)
    expect(alert?.getAttribute('data-visual-state')).toBe('retrying')
    expect(alert?.hasAttribute('role')).toBe(false)
    expect(alert?.getAttribute('aria-busy')).toBe('true')
    expect(alert?.textContent).toContain('Reloading conversation')
    expect(alert?.textContent).toContain('Restoring conversation history…')
    expect(
      (host.querySelector('[data-testid="chat-session-load-retrying"]') as HTMLButtonElement)
        .disabled,
    ).toBe(true)

    currentState.value = 'loading'
    await nextTick()
    expect(alert?.getAttribute('data-visual-state')).toBe('retrying')
    currentState.value = 'error'
    await nextTick()
    expect(alert?.getAttribute('data-visual-state')).toBe('error')
    expect(host.querySelector('[data-testid="chat-session-load-retry"]')).toBeTruthy()
  })

  it('does not carry retry progress into another session', async () => {
    const { currentSessionKey, currentState, host } = await mountState('error')
    const retry = host.querySelector('[data-testid="chat-session-load-retry"]') as HTMLButtonElement

    retry.click()
    currentState.value = 'loading'
    await nextTick()
    expect(
      host.querySelector('[data-testid="chat-session-load-state"]')
        ?.getAttribute('data-visual-state'),
    ).toBe('retrying')

    currentSessionKey.value = 'agent:main:webchat:session-b'
    await nextTick()
    const nextSessionState = host.querySelector('[data-testid="chat-session-load-state"]')
    expect(nextSessionState?.getAttribute('data-visual-state')).toBe('loading')
    expect(nextSessionState?.textContent).toContain('Loading conversation…')
    expect(host.querySelector('[data-testid="chat-session-load-retrying"]')).toBeNull()
  })
})
