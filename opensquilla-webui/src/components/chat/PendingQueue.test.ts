// @vitest-environment happy-dom

import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, defineComponent, h, nextTick, reactive, ref } from 'vue'
import i18n, { loadLocaleMessages } from '@/i18n'
import PendingQueue from './PendingQueue.vue'
import type { Attachment, PendingSteerAttempt } from '@/types/chat'

afterEach(() => {
  document.body.innerHTML = ''
  i18n.global.locale.value = 'en'
})

async function mountQueue(
  listeners: Partial<{
    onClear: () => void
    onEdit: (pendingUiId: string) => void
    onRemove: (pendingUiId: string) => void
    onReorder: (fromIndex: number, toIndex: number) => void
    onReorderEnd: () => void
    onReorderStart: (index: number) => void
    onSteer: (pendingUiId: string) => void
  }> = {},
  items: Array<{
    pendingUiId?: string
    text: string
    pendingInputId?: string
    pendingPersistenceState?: 'saving' | 'staged' | 'local_only' | 'retryable' | 'cancelling'
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
    durableSteerAvailable?: boolean
    steerUnavailableMessage?: string
  } = {},
) {
  const el = document.createElement('div')
  document.body.appendChild(el)
  items.forEach((item, index) => {
    item.pendingUiId ||= `pending-ui-${index}`
  })
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

  it('keeps the original steer affordance disabled and visibly explains queue-only delivery', async () => {
    const reason = 'Steer unavailable: the active task identity has not synchronized yet.'
    const { app, el } = await mountQueue({}, [
      { text: 'Follow the latest instruction' },
      { text: 'Use the concise version' },
    ], {
      steerAvailable: false,
      steerUnavailableMessage: reason,
    })

    const steer = el.querySelector<HTMLButtonElement>('.chat-pending-action--steer')
    expect(steer?.textContent).toContain('Steer')
    expect(steer?.disabled).toBe(true)
    expect(steer?.title).toBe(reason)
    expect(steer?.getAttribute('aria-describedby')).toBeNull()
    const status = el.querySelector<HTMLElement>('.chat-pending-steer-status')
    expect(el.querySelectorAll('.chat-pending-steer-status')).toHaveLength(1)
    expect(status?.getAttribute('role')).toBe('status')
    expect(status?.getAttribute('aria-live')).toBe('polite')
    expect(status?.textContent).toContain(reason)
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

  it('keeps a durable queued item disabled until the gateway supports atomic steer', async () => {
    const { app, el } = await mountQueue({}, [{
      text: 'Durably queued guidance',
      pendingInputId: 'pending-durable',
      pendingPersistenceState: 'staged',
    }], {
      steerAvailable: true,
      durableSteerAvailable: false,
    })

    const steer = el.querySelector<HTMLButtonElement>('.chat-pending-action--steer')
    expect(steer?.disabled).toBe(true)
    expect(steer?.title).toContain('gateway does not support')
    app.unmount()
  })

  it('offers steer, remove, and quiet overflow actions on each queued message', async () => {
    const steered: string[] = []
    const removed: string[] = []
    const { app, el } = await mountQueue({
      onSteer: (pendingUiId: string) => { steered.push(pendingUiId) },
      onRemove: (pendingUiId: string) => { removed.push(pendingUiId) },
    })

    const steer = [...el.querySelectorAll<HTMLButtonElement>('button')]
      .find(button => button.textContent?.includes('Steer'))
    steer?.click()
    el.querySelector<HTMLButtonElement>('[aria-label="Remove pending message 1"]')?.click()

    expect(steered).toEqual(['pending-ui-0'])
    expect(removed).toEqual(['pending-ui-0'])
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
    expect(el.querySelector('.chat-pending-card')?.getAttribute('data-delivery-state')).toBe('busy')
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
    }], {
      steerAvailable: false,
      steerUnavailableMessage: 'New messages will queue after the current response.',
    })

    const retry = el.querySelector<HTMLButtonElement>('.chat-pending-action--steer')
    expect(el.querySelector('.chat-pending-card')?.getAttribute('data-delivery-state'))
      .toBe('attention')
    expect(retry?.textContent).toContain(action)
    expect(retry?.disabled).toBe(false)
    expect(el.querySelector('.chat-pending-steer-status')).toBeNull()
    expect(el.querySelector<HTMLButtonElement>(`[aria-label="${remove}"]`)).not.toBeNull()
    app.unmount()
  })

  it('keeps a rejected steer retry available without showing queue-only status', async () => {
    const { app, el } = await mountQueue({}, [{
      text: steerRequest.message,
      steerAttempt: { phase: 'retryable_rejected', request: steerRequest },
    }], {
      steerAvailable: false,
      steerUnavailableMessage: 'New messages will queue after the current response.',
    })

    const retry = el.querySelector<HTMLButtonElement>('.chat-pending-action--steer')
    expect(retry?.textContent).toContain('Not sent · Retry')
    expect(retry?.disabled).toBe(false)
    expect(el.querySelector('.chat-pending-steer-status')).toBeNull()
    app.unmount()
  })

  it('does not show queue-only status beside steer confirmation retries', async () => {
    const { app, el } = await mountQueue({}, [
      { text: 'Ordinary queued follow-up' },
      {
        text: steerRequest.message,
        steerAttempt: { phase: 'acceptance_unknown', request: steerRequest },
      },
    ], {
      steerAvailable: false,
      steerUnavailableMessage: 'New messages will queue after the current response.',
    })

    const steerButtons = [...el.querySelectorAll<HTMLButtonElement>(
      '.chat-pending-action--steer',
    )]
    expect(steerButtons[0]?.disabled).toBe(true)
    expect(steerButtons[1]?.disabled).toBe(false)
    expect(el.querySelector('.chat-pending-steer-status')).toBeNull()
    app.unmount()
  })

  it('does not show queue-only status beside a rejected steer retry', async () => {
    const { app, el } = await mountQueue({}, [
      { text: 'Ordinary queued follow-up' },
      {
        text: 'Rejected steer retry',
        steerAttempt: { phase: 'retryable_rejected', request: steerRequest },
      },
    ], {
      steerAvailable: false,
      steerUnavailableMessage: 'New messages will queue after the current response.',
    })

    const steerButtons = [...el.querySelectorAll<HTMLButtonElement>(
      '.chat-pending-action--steer',
    )]
    expect(steerButtons[0]?.disabled).toBe(true)
    expect(steerButtons[1]?.disabled).toBe(false)
    expect(el.querySelector('.chat-pending-steer-status')).toBeNull()
    app.unmount()
  })

  it('keeps a retryable item available for an explicit retry', async () => {
    let steered = 0
    let edited = 0
    const { app, el } = await mountQueue({
      onSteer: () => { steered += 1 },
      onEdit: () => { edited += 1 },
    }, [{ text: 'Retry this steer', deliveryState: 'retryable' }], {
      steerAvailable: false,
      steerUnavailableMessage: 'New messages will queue after the current response.',
    })

    expect(el.querySelector('.chat-pending-card')?.hasAttribute('aria-busy')).toBe(false)
    const retry = [...el.querySelectorAll<HTMLButtonElement>('button')]
      .find(button => button.textContent?.includes('Retry'))
    expect(retry?.disabled).toBe(false)
    expect(retry?.title).toBe('Retry')
    expect(el.querySelector('.chat-pending-steer-status')).toBeNull()
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

  it('activates pointer sorting only after a 750 ms hold and reorders past a midpoint', async () => {
    vi.useFakeTimers()
    const starts: number[] = []
    const moves: Array<[number, number]> = []
    let ended = 0
    const elementFromPoint = vi.spyOn(document, 'elementFromPoint')
    const { app, el } = await mountQueue({
      onReorderStart: (index: number) => starts.push(index),
      onReorder: (fromIndex: number, toIndex: number) => moves.push([fromIndex, toIndex]),
      onReorderEnd: () => { ended += 1 },
    }, [
      { text: 'First queued message' },
      { text: 'Second queued message' },
      { text: 'Third queued message' },
    ])

    try {
      const cards = [...el.querySelectorAll<HTMLElement>('.chat-pending-card')]
      expect(cards[0]?.classList.contains('is-reorderable')).toBe(true)
      cards[0]?.dispatchEvent(new MouseEvent('pointerdown', {
        bubbles: true,
        button: 0,
        clientX: 20,
        clientY: 20,
      }))
      await nextTick()
      expect(cards[0]?.classList.contains('is-reorder-arming')).toBe(true)
      await vi.advanceTimersByTimeAsync(749)
      expect(starts).toEqual([])
      expect(cards[0]?.classList.contains('is-reordering')).toBe(false)

      await vi.advanceTimersByTimeAsync(1)
      await nextTick()
      expect(starts).toEqual([0])
      expect(cards[0]?.classList.contains('is-reordering')).toBe(true)
      expect([...cards[0]!.querySelectorAll<HTMLButtonElement>('button')]
        .every(button => button.disabled)).toBe(true)

      Object.defineProperty(cards[1], 'getBoundingClientRect', {
        configurable: true,
        value: () => ({ top: 50, height: 50 }),
      })
      elementFromPoint.mockReturnValue(cards[1]!)
      document.dispatchEvent(new MouseEvent('pointermove', {
        bubbles: true,
        clientX: 20,
        clientY: 90,
      }))
      expect(moves).toEqual([[0, 1]])

      document.dispatchEvent(new MouseEvent('pointerup', { bubbles: true }))
      await nextTick()
      expect(ended).toBe(1)
      expect(cards[0]?.classList.contains('is-reordering')).toBe(false)
    } finally {
      app.unmount()
      elementFromPoint.mockRestore()
      vi.useRealTimers()
    }
  })

  it('cancels a pending long press when the pointer moves before activation', async () => {
    vi.useFakeTimers()
    let started = 0
    const { app, el } = await mountQueue({
      onReorderStart: () => { started += 1 },
    }, [
      { text: 'First queued message' },
      { text: 'Second queued message' },
    ])

    try {
      el.querySelector<HTMLElement>('.chat-pending-card')?.dispatchEvent(new MouseEvent(
        'pointerdown',
        { bubbles: true, button: 0, clientX: 10, clientY: 10 },
      ))
      document.dispatchEvent(new MouseEvent('pointermove', {
        bubbles: true,
        clientX: 25,
        clientY: 10,
      }))
      await vi.advanceTimersByTimeAsync(750)
      expect(started).toBe(0)
    } finally {
      app.unmount()
      vi.useRealTimers()
    }
  })

  it('supports keyboard reordering and disables sorting around a delivery lease', async () => {
    const moves: Array<[number, number]> = []
    let starts = 0
    let ends = 0
    const { app, el } = await mountQueue({
      onReorderStart: () => { starts += 1 },
      onReorder: (fromIndex: number, toIndex: number) => moves.push([fromIndex, toIndex]),
      onReorderEnd: () => { ends += 1 },
    }, [
      { text: 'First queued message' },
      { text: 'Second queued message' },
    ])

    const cards = [...el.querySelectorAll<HTMLElement>('.chat-pending-card')]
    expect(cards.every(card => card.tabIndex === 0)).toBe(true)
    cards[1]?.dispatchEvent(new KeyboardEvent('keydown', {
      bubbles: true,
      altKey: true,
      key: 'ArrowUp',
    }))
    expect(starts).toBe(1)
    expect(moves).toEqual([[1, 0]])
    expect(ends).toBe(1)
    app.unmount()

    const locked = await mountQueue({}, [
      { text: 'In flight', deliveryState: 'steering' },
      { text: 'Must wait' },
    ])
    expect([...locked.el.querySelectorAll<HTMLElement>('.chat-pending-card')]
      .every(card => card.getAttribute('tabindex') === null)).toBe(true)
    locked.app.unmount()
  })

  it('preserves bubble identity when a middle item is removed', async () => {
    const items = reactive([
      { text: 'First queued message' },
      { text: 'Delete this middle message' },
      { text: 'Last queued message' },
    ])
    const { app, el } = await mountQueue({}, items)
    const before = [...el.querySelectorAll<HTMLElement>('.chat-pending-card')]

    items.splice(1, 1)
    await nextTick()

    const after = [...el.querySelectorAll<HTMLElement>('.chat-pending-card')]
      .filter(card => !card.classList.contains('chat-pending-list-leave-active'))
    expect(after.map(card => card.querySelector('.chat-pending-text')?.textContent?.trim()))
      .toEqual(['First queued message', 'Last queued message'])
    expect(after[0]).toBe(before[0])
    expect(after[1]).toBe(before[2])
    app.unmount()
  })

  it('keeps menu focus and action identity when a peer removes an earlier item', async () => {
    const items = ref([
      { pendingUiId: 'pending-peer-a', text: 'Peer A' },
      { pendingUiId: 'pending-peer-b', text: 'Peer B' },
    ])
    const edited: string[] = []
    const el = document.createElement('div')
    document.body.appendChild(el)
    const Host = defineComponent(() => () => h(PendingQueue, {
      items: items.value,
      maxPending: 5,
      steerAvailable: true,
      onEdit: (pendingUiId: string) => edited.push(pendingUiId),
    }))
    const app = createApp(Host)
    app.use(i18n)
    app.mount(el)
    await nextTick()

    const secondMore = el.querySelectorAll<HTMLButtonElement>('[aria-label="More"]')[1]
    secondMore?.click()
    await nextTick()
    const edit = [...el.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')]
      .find(button => button.textContent?.includes('Edit message'))
    edit?.focus()
    expect(document.activeElement).toBe(edit)

    items.value.splice(0, 1)
    await nextTick()

    const survivingCard = el.querySelector<HTMLElement>('[data-pending-ui-id="pending-peer-b"]')
    expect(survivingCard?.querySelector('[role="menu"]')).not.toBeNull()
    expect(document.activeElement).toBe(edit)
    edit?.click()
    expect(edited).toEqual(['pending-peer-b'])
    app.unmount()
  })
})
