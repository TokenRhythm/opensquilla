// @vitest-environment happy-dom
import { createApp, nextTick } from 'vue'
import { afterEach, describe, expect, it } from 'vitest'

import i18n from '@/i18n'
import { useConfirm } from '@/composables/useConfirm'
import ConfirmModal from './ConfirmModal.vue'

describe('ConfirmModal accessibility', () => {
  let app: ReturnType<typeof createApp> | null = null

  afterEach(() => {
    useConfirm().resolveConfirm(false)
    app?.unmount()
    app = null
    document.body.innerHTML = ''
  })

  it('describes the dialog body and cancels on Escape', async () => {
    const root = document.createElement('div')
    document.body.appendChild(root)
    app = createApp(ConfirmModal)
    app.use(i18n)
    app.mount(root)

    const result = useConfirm().confirm({ title: 'Replace profile?', body: 'Backup first.' })
    await nextTick()
    await nextTick()

    const dialog = document.querySelector<HTMLElement>('[role="dialog"]')
    expect(dialog?.getAttribute('aria-describedby')).toBe('confirm-modal-description')
    expect(document.querySelector('#confirm-modal-description')?.textContent).toBe('Backup first.')

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await expect(result).resolves.toBe(false)
  })

  it('keeps cancellation and confirmation as distinct working actions', async () => {
    const root = document.createElement('div')
    document.body.appendChild(root)
    app = createApp(ConfirmModal)
    app.use(i18n)
    app.mount(root)

    const cancelled = useConfirm().confirm({
      title: 'Discard changes?',
      body: 'Unsaved edits will be lost.',
      primaryLabel: 'Discard changes',
    })
    await nextTick()
    await nextTick()
    const cancel = document.querySelector<HTMLButtonElement>('.modal__footer .btn--ghost')
    cancel?.click()
    await expect(cancelled).resolves.toBe(false)

    const confirmed = useConfirm().confirm({
      title: 'Discard changes?',
      body: 'Unsaved edits will be lost.',
      primaryLabel: 'Discard changes',
    })
    await nextTick()
    await nextTick()
    const primary = document.querySelector<HTMLButtonElement>('.modal__footer .modal__primary')
    expect(primary?.disabled).toBe(false)
    primary?.click()
    await expect(confirmed).resolves.toBe(true)
  })

  it('places Cancel before Trust and open in the project trust prompt', async () => {
    const root = document.createElement('div')
    document.body.appendChild(root)
    app = createApp(ConfirmModal)
    app.use(i18n)
    app.mount(root)

    void useConfirm().confirm({
      title: 'Trust this project directory?',
      body: '/Volumes/workspace/project',
      primaryLabel: 'Trust and open',
    })
    await nextTick()
    await nextTick()

    const labels = Array.from(
      document.querySelectorAll<HTMLButtonElement>('.modal__footer button'),
      button => button.textContent?.trim(),
    )
    expect(labels).toEqual(['Cancel', 'Trust and open'])
  })
})
