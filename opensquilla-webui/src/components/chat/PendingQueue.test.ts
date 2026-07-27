// @vitest-environment happy-dom

import { afterEach, describe, expect, it } from 'vitest'
import { createApp, nextTick } from 'vue'
import i18n from '@/i18n'
import PendingQueue from './PendingQueue.vue'

afterEach(() => {
  document.body.innerHTML = ''
  i18n.global.locale.value = 'en'
})

async function mountQueue(listeners: Record<string, () => void> = {}) {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(PendingQueue, {
    items: [{ text: 'Follow the latest instruction' }],
    maxPending: 5,
    ...listeners,
  })
  app.use(i18n)
  app.mount(el)
  await nextTick()
  return { app, el }
}

describe('PendingQueue', () => {
  it('offers steer, remove, and quiet overflow actions on each queued message', async () => {
    let steered = 0
    let removed = 0
    const { app, el } = await mountQueue({
      onSteer: () => { steered += 1 },
      onRemove: () => { removed += 1 },
    })

    const steer = [...el.querySelectorAll<HTMLButtonElement>('button')]
      .find(button => button.textContent?.includes('Steer'))
    steer?.click()
    el.querySelector<HTMLButtonElement>('[aria-label="Remove pending message 1"]')?.click()

    expect(steered).toBe(1)
    expect(removed).toBe(1)
    expect(el.querySelector('.chat-pending-card')).not.toBeNull()
    app.unmount()
  })

  it('keeps edit and clear-all inside the overflow menu', async () => {
    let edited = 0
    let cleared = 0
    const { app, el } = await mountQueue({
      onEdit: () => { edited += 1 },
      onClear: () => { cleared += 1 },
    })

    el.querySelector<HTMLButtonElement>('[aria-label="More"]')?.click()
    await nextTick()
    expect(el.querySelector('[role="menu"]')).not.toBeNull()

    const buttons = [...el.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')]
    buttons.find(button => button.textContent?.includes('Edit message'))?.click()
    expect(edited).toBe(1)

    el.querySelector<HTMLButtonElement>('[aria-label="More"]')?.click()
    await nextTick()
    ;[...el.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')]
      .find(button => button.textContent?.includes('Clear queue'))
      ?.click()
    expect(cleared).toBe(1)
    app.unmount()
  })
})
