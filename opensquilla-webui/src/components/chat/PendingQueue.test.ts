// @vitest-environment happy-dom

import { afterEach, describe, expect, it } from 'vitest'
import { createApp, nextTick } from 'vue'
import i18n, { loadLocaleMessages } from '@/i18n'
import PendingQueue from './PendingQueue.vue'
import type { Attachment, PendingSteerAttempt } from '@/types/chat'

afterEach(() => {
  document.body.innerHTML = ''
  i18n.global.locale.value = 'en'
})

async function mountQueue(
  listeners: Record<string, () => void> = {},
  items: Array<{
    text: string
    deliveryState?: 'steering' | 'retryable'
    steerAttempt?: PendingSteerAttempt
    attachments?: Attachment[]
    hiddenControl?: boolean
    displayTextOverride?: string
  }> = [
    { text: 'Follow the latest instruction' },
  ],
  props: {
    imageBlockedMessage?: string
    steerAvailable?: boolean
    steerUnavailableMessage?: string
  } = {},
) {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(PendingQueue, {
    items,
    maxPending: 5,
    steerAvailable: true,
    ...listeners,
    ...props,
  })
  app.use(i18n)
  app.mount(el)
  await nextTick()
  return { app, el }
}

describe('PendingQueue', () => {
  const steerRequest = {
    key: 'agent:main:webchat:test',
    message: 'Make it longer',
    expected_turn_id: 'turn-current',
    client_request_id: 'request-steer',
    client_message_id: 'client-steer',
    surface_id: 'webui',
    _source: { runMode: 'safe' as const },
  }

  it('keeps the original steer affordance visible but disabled when capability is unavailable', async () => {
    const reason = 'Steer unavailable: the active task identity has not synchronized yet.'
    const { app, el } = await mountQueue({}, undefined, {
      steerAvailable: false,
      steerUnavailableMessage: reason,
    })

    const steer = el.querySelector<HTMLButtonElement>('.chat-pending-action--steer')
    expect(steer?.textContent).toContain('Steer')
    expect(steer?.disabled).toBe(true)
    expect(steer?.title).toBe(reason)
    expect(steer?.getAttribute('aria-describedby')).toBeNull()
    expect(el.querySelector('.chat-pending-steer-status')).toBeNull()
    expect(el.querySelector('[aria-label="Remove pending message 1"]')).not.toBeNull()
    app.unmount()
  })

  it('does not show an unavailable reason when same-turn steering is available', async () => {
    const { app, el } = await mountQueue({}, undefined, {
      steerAvailable: true,
      steerUnavailableMessage: 'This stale reason must stay hidden.',
    })

    const steer = el.querySelector<HTMLButtonElement>('.chat-pending-action--steer')
    expect(steer?.disabled).toBe(false)
    expect(steer?.title).not.toContain('stale reason')
    expect(el.querySelector('.chat-pending-steer-status')).toBeNull()
    expect(steer?.getAttribute('aria-describedby')).toBeNull()
    app.unmount()
  })

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

  it('derives submitting UI only from the canonical steer attempt phase', async () => {
    const { app, el } = await mountQueue({}, [{
      text: steerRequest.message,
      steerAttempt: { phase: 'submitting', request: steerRequest },
    }])

    expect(el.querySelector('.chat-pending-card')?.getAttribute('aria-busy')).toBe('true')
    expect(el.querySelector('.chat-pending')?.getAttribute('aria-label')).toBe('Pending 1/6')
    expect(el.querySelector('.chat-pending-action--steer')?.textContent)
      .toContain('Submitting guidance…')
    const actions = [...el.querySelectorAll<HTMLButtonElement>('.chat-pending-actions button')]
    expect(actions.every(button => button.disabled)).toBe(true)
    app.unmount()
  })

  it.each([
    {
      locale: 'en' as const,
      action: 'Delivery status unknown · Retry confirmation',
      remove: 'Discard local retry for pending message 1; this does not mean the server did not receive it',
    },
    {
      locale: 'zh-Hans' as const,
      action: '发送状态未知 · 重试确认',
      remove: '放弃待发送消息 1 在本设备上的重试；这不代表服务端未接收',
    },
  ])('explains acceptance-unknown retry and local discard in $locale', async ({
    locale,
    action,
    remove,
  }) => {
    await loadLocaleMessages(locale)
    i18n.global.locale.value = locale
    const { app, el } = await mountQueue({}, [{
      text: steerRequest.message,
      steerAttempt: { phase: 'acceptance_unknown', request: steerRequest },
    }], { steerAvailable: false })

    const retry = el.querySelector<HTMLButtonElement>('.chat-pending-action--steer')
    expect(retry?.textContent).toContain(action)
    expect(retry?.disabled).toBe(false)
    expect(el.querySelector<HTMLButtonElement>(`[aria-label="${remove}"]`)).not.toBeNull()
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

  it('keeps hidden control input removable without exposing same-turn retry', async () => {
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
    expect(retry).toBeUndefined()
    el.querySelector<HTMLButtonElement>('[aria-label="Remove pending message 1"]')?.click()

    expect(retried).toBe(0)
    expect(removed).toBe(1)
    expect(el.querySelector('[aria-label="More"]')).toBeNull()
    app.unmount()
  })

  it.each(['/status', '!pwd'])(
    'keeps the original affordance disabled for queued control input %s',
    async (text) => {
      let steered = 0
      const { app, el } = await mountQueue({
        onSteer: () => { steered += 1 },
      }, [{ text }])

      const steer = el.querySelector<HTMLButtonElement>('.chat-pending-action--steer')
      expect(steer?.textContent).toContain('Steer')
      expect(steer?.disabled).toBe(true)
      expect(steered).toBe(0)
      app.unmount()
    },
  )

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
    expect(steerButtons[0]?.title).toContain('already being applied')
    expect(steerButtons[1]?.title).toContain('another queued message is being delivered')
    app.unmount()
  })

  it('prioritizes the attachment blocker over a task capability reason', async () => {
    const documentAttachment: Attachment = {
      kind: 'staged',
      local_id: 9,
      name: 'requirements.pdf',
      mime: 'application/pdf',
      file_uuid: 'document-9',
    }
    const capabilityReason = 'Steer unavailable: the active task identity has not synchronized yet.'
    const { app, el } = await mountQueue({}, [{
      text: 'Review this document',
      attachments: [documentAttachment],
    }], {
      steerAvailable: false,
      steerUnavailableMessage: capabilityReason,
    })

    const steer = el.querySelector<HTMLButtonElement>('.chat-pending-action--steer')
    expect(steer?.disabled).toBe(true)
    expect(steer?.title).toContain('messages with attachments')
    expect(steer?.title).not.toBe(capabilityReason)
    app.unmount()
  })

  it('keeps the original affordance disabled for a queued item with an attachment', async () => {
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
    expect(el.querySelector('.chat-pending-attachment-status')?.textContent)
      .toContain('retry or remove the failed attachment')
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
