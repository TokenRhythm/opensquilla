// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick } from 'vue'
import i18n from '@/i18n'
import ChatStallNotice from './ChatStallNotice.vue'

async function mountNotice(props: {
  seconds: number
  canInterrupt?: boolean
  onWait?: () => void
  onInterrupt?: () => void
}) {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(ChatStallNotice, props)
  app.use(i18n)
  app.mount(el)
  await nextTick()
  return { app, el }
}

beforeEach(() => {
  i18n.global.locale.value = 'en'
  document.body.innerHTML = ''
})

describe('ChatStallNotice', () => {
  it('renders as a polite live status region with both actions', async () => {
    const { app, el } = await mountNotice({ seconds: 92 })

    const notice = el.querySelector('[data-testid="chat-stall-notice"]')
    expect(notice).toBeTruthy()
    expect(notice?.getAttribute('role')).toBe('status')
    expect(notice?.getAttribute('aria-live')).toBe('polite')

    const wait = el.querySelector<HTMLButtonElement>('[data-testid="chat-stall-wait"]')
    const interrupt = el.querySelector<HTMLButtonElement>('[data-testid="chat-stall-interrupt"]')
    expect(wait?.tagName).toBe('BUTTON')
    expect(interrupt?.tagName).toBe('BUTTON')
    // Buttons carry visible text labels (accessible names).
    expect(wait?.textContent?.trim()).toBeTruthy()
    expect(interrupt?.textContent?.trim()).toBeTruthy()
    app.unmount()
  })

  it('emits wait and interrupt from the two buttons', async () => {
    const onWait = vi.fn()
    const onInterrupt = vi.fn()
    const { app, el } = await mountNotice({ seconds: 90, onWait, onInterrupt })

    el.querySelector<HTMLButtonElement>('[data-testid="chat-stall-wait"]')
      ?.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    el.querySelector<HTMLButtonElement>('[data-testid="chat-stall-interrupt"]')
      ?.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await nextTick()

    expect(onWait).toHaveBeenCalledTimes(1)
    expect(onInterrupt).toHaveBeenCalledTimes(1)
    app.unmount()
  })

  it('keeps the neutral wait action but hides interrupt when policy is read-only', async () => {
    const { app, el } = await mountNotice({ seconds: 90, canInterrupt: false })

    expect(el.querySelector('[data-testid="chat-stall-notice"]')).toBeTruthy()
    expect(el.querySelector('[data-testid="chat-stall-wait"]')).toBeTruthy()
    expect(el.querySelector('[data-testid="chat-stall-interrupt"]')).toBeNull()
    app.unmount()
  })
})
