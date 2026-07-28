// @vitest-environment happy-dom

import { afterEach, describe, expect, it } from 'vitest'
import { createApp, nextTick } from 'vue'
import i18n from '@/i18n'
import PendingQueue from './PendingQueue.vue'
import type { Attachment } from '@/types/chat'

afterEach(() => {
  document.body.innerHTML = ''
  i18n.global.locale.value = 'en'
})

async function mountQueue(
  listeners: Record<string, () => void> = {},
  items: Array<{
    text: string
    deliveryState?: 'steering' | 'retryable'
    attachments?: Attachment[]
    hiddenControl?: boolean
    displayTextOverride?: string
  }> = [
    { text: 'Follow the latest instruction' },
  ],
  props: { imageBlockedMessage?: string } = {},
) {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(PendingQueue, {
    items,
    maxPending: 5,
    ...listeners,
    ...props,
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

  it('marks a steering item busy and disables every destructive or duplicate action', async () => {
    let steered = 0
    let removed = 0
    const { app, el } = await mountQueue({
      onSteer: () => { steered += 1 },
      onRemove: () => { removed += 1 },
    }, [{ text: 'Already steering', deliveryState: 'steering' }])

    expect(el.querySelector('.chat-pending-card')?.getAttribute('aria-busy')).toBe('true')
    const actions = [...el.querySelectorAll<HTMLButtonElement>('.chat-pending-actions button')]
    expect(actions).toHaveLength(3)
    expect(actions.every(button => button.disabled)).toBe(true)

    actions.forEach(button => button.click())
    await nextTick()
    expect(steered).toBe(0)
    expect(removed).toBe(0)
    expect(el.querySelector('[role="menu"]')).toBeNull()
    app.unmount()
  })

  it('keeps a retryable item available for an explicit retry', async () => {
    let steered = 0
    let edited = 0
    const { app, el } = await mountQueue({
      onSteer: () => { steered += 1 },
      onEdit: () => { edited += 1 },
    }, [{ text: 'Retry this steer', deliveryState: 'retryable' }])

    expect(el.querySelector('.chat-pending-card')?.hasAttribute('aria-busy')).toBe(false)
    const retry = [...el.querySelectorAll<HTMLButtonElement>('button')]
      .find(button => button.textContent?.includes('Retry'))
    expect(retry?.disabled).toBe(false)
    expect(retry?.title).toBe('Retry')
    retry?.click()
    expect(steered).toBe(1)

    el.querySelector<HTMLButtonElement>('[aria-label="More"]')?.click()
    await nextTick()
    const edit = [...el.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')]
      .find(button => button.textContent?.includes('Edit message'))
    expect(edit?.disabled).toBe(true)
    edit?.click()
    expect(edited).toBe(0)
    app.unmount()
  })

  it('makes a failed hidden confirmation explicitly retryable or removable', async () => {
    let retried = 0
    let removed = 0
    const { app, el } = await mountQueue({
      onSteer: () => { retried += 1 },
      onRemove: () => { removed += 1 },
    }, [{
      text: 'provider-only marker',
      displayTextOverride: 'Confirmed',
      hiddenControl: true,
      deliveryState: 'retryable',
    }])

    expect(el.querySelector('.chat-pending-text')?.textContent).toContain('Confirmed')
    const retry = [...el.querySelectorAll<HTMLButtonElement>('button')]
      .find(button => button.textContent?.includes('Retry'))
    retry?.click()
    el.querySelector<HTMLButtonElement>('[aria-label="Remove pending message 1"]')?.click()

    expect(retried).toBe(1)
    expect(removed).toBe(1)
    expect(el.querySelector('[aria-label="More"]')).toBeNull()
    app.unmount()
  })

  it('allows only one queued delivery lease at a time', async () => {
    const { app, el } = await mountQueue({}, [
      { text: 'In flight', deliveryState: 'steering' },
      { text: 'Must wait' },
    ])

    const steerButtons = [...el.querySelectorAll<HTMLButtonElement>(
      '.chat-pending-action--steer',
    )]
    expect(steerButtons).toHaveLength(2)
    expect(steerButtons.every(button => button.disabled)).toBe(true)
    app.unmount()
  })

  it('explains why a queued item with a failed attachment cannot steer', async () => {
    const failed: Attachment = {
      kind: 'failed',
      local_id: 7,
      name: 'failed.pdf',
      mime: 'application/pdf',
      error: 'upload failed',
    }
    const { app, el } = await mountQueue({}, [{
      text: 'Keep this attachment',
      attachments: [failed],
    }])

    const steer = el.querySelector<HTMLButtonElement>('.chat-pending-action--steer')
    expect(steer?.disabled).toBe(true)
    expect(steer?.title).toContain('failed attachment')
    const describedBy = steer?.getAttribute('aria-describedby')
    expect(describedBy).toBeTruthy()
    expect(el.querySelector(`#${describedBy}`)?.textContent)
      .toContain('retry or remove the failed attachment')
    expect(el.querySelector('.chat-pending-attachment-status')?.textContent)
      .toContain('retry or remove the failed attachment')
    expect(el.querySelector('.chat-pending-card')?.getAttribute('aria-describedby'))
      .toBe(describedBy)
    expect(el.querySelector('.chat-pending-attachments')?.textContent)
      .toContain('Needs attention')
    app.unmount()
  })

  it('explains why current routing blocks a queued image', async () => {
    const image: Attachment = {
      kind: 'staged',
      local_id: 8,
      name: 'diagram.png',
      mime: 'image/png',
      file_uuid: 'image-8',
    }
    const blockedMessage = 'Ensemble mode does not support image attachments.'
    const { app, el } = await mountQueue({}, [{
      text: 'Review this image',
      attachments: [image],
    }], { imageBlockedMessage: blockedMessage })

    const steer = el.querySelector<HTMLButtonElement>('.chat-pending-action--steer')
    expect(steer?.disabled).toBe(true)
    expect(steer?.title).toBe(blockedMessage)
    const describedBy = steer?.getAttribute('aria-describedby')
    expect(describedBy).toBeTruthy()
    expect(el.querySelector(`#${describedBy}`)?.textContent).toContain(blockedMessage)
    expect(el.querySelector('.chat-pending-attachment-status')?.textContent)
      .toContain(blockedMessage)
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
