// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick } from 'vue'
import i18n from '@/i18n'
import type { ChatRenderedMessage } from '@/types/chat'
import UserMessage from './UserMessage.vue'

const message = {
  id: 'message-1',
  role: 'user',
  displayRole: 'user',
  roleLabel: 'You',
  text: 'attached',
  timeStr: '',
  showHeader: false,
  attachments: [{
    kind: 'file',
    displayId: 'attachment-1',
    renderKey: 'attachment-1',
    name: 'report.pdf',
    mime: 'application/pdf',
  }],
} satisfies ChatRenderedMessage

beforeEach(() => {
  i18n.global.locale.value = 'en'
  document.body.innerHTML = ''
  vi.restoreAllMocks()
})

describe('UserMessage attachment download', () => {
  it('uses a busy deduped button and leaves share selection untouched', async () => {
    let resolveDownload: ((ok: boolean) => void) | undefined
    const downloadAttachment = vi.fn(() => new Promise<boolean>((resolve) => {
      resolveDownload = resolve
    }))
    const toggleShare = vi.fn()
    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp(UserMessage, {
      message,
      shareMode: true,
      shareSelected: false,
      shareMessageId: 'message-1',
      stripTimePrefix: (value: string) => value,
      copyMessage: async () => true,
      downloadAttachment,
      onToggleShare: toggleShare,
    })
    app.use(i18n)
    app.mount(host)
    await nextTick()

    const chip = host.querySelector<HTMLButtonElement>('.msg-file-chip')
    chip?.click()
    chip?.click()
    await nextTick()

    expect(downloadAttachment).toHaveBeenCalledOnce()
    expect(toggleShare).not.toHaveBeenCalled()
    expect(chip?.disabled).toBe(true)
    expect(chip?.getAttribute('aria-busy')).toBe('true')

    resolveDownload?.(false)
    await Promise.resolve()
    await nextTick()
    expect(chip?.disabled).toBe(false)
    expect(chip?.classList.contains('msg-file-chip--failed')).toBe(true)

    chip?.click()
    expect(downloadAttachment).toHaveBeenCalledTimes(2)
    expect(toggleShare).not.toHaveBeenCalled()
    app.unmount()
  })
})
