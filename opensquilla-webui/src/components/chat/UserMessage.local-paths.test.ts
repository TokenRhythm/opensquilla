// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick, type App } from 'vue'
import i18n from '@/i18n'
import type { ChatRenderedMessage } from '@/types/chat'
import UserMessage from './UserMessage.vue'

const apps: App[] = []
const paths = ['C:\\Users\\测试\\report.pdf', '/tmp/report.pdf']
afterEach(() => {
  apps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
})

async function render(text: string, refs?: string[], image = false) {
  i18n.global.locale.value = 'en'
  const message: ChatRenderedMessage = { id: 'user-paths', role: 'user', displayRole: 'user', roleLabel: 'You',
    text, timeStr: '', showHeader: false, localPathReferences: refs,
    ...(image ? { attachments: [{ kind: 'inline' as const, displayId: 'image', renderKey: 'image',
      name: 'screenshot.png', mime: 'image/png', data: 'aW1hZ2U=' }] } : {}),
  }
  const host = document.createElement('div')
  document.body.appendChild(host)
  const copyMessage = vi.fn(async () => true)
  const onEdit = vi.fn()
  const app = createApp(UserMessage, { message, shareMode: false, shareSelected: false,
    shareMessageId: message.id, stripTimePrefix: (value: string) => value,
    copyMessage, onEdit, downloadAttachment: async () => true })
  app.use(i18n)
  app.mount(host)
  apps.push(app)
  await nextTick()
  return { host, message, copyMessage, onEdit }
}

describe('sent local references', () => {
  it('renders filenames separately from the body and retains distinct full-path titles', async () => {
    const { host } = await render(`Compare\n${paths.join('\n')}`, paths, true)
    expect(host.querySelector('.msg-user-bubble')?.textContent?.trim()).toBe('Compare')
    const chips = [...host.querySelectorAll<HTMLElement>('.msg-local-path')]
    expect(chips.map(chip => chip.textContent?.trim())).toEqual(['report.pdf', 'report.pdf'])
    expect(chips.map(chip => chip.title)).toEqual(paths)
    expect(host.querySelector('.msg-thumb')?.getAttribute('alt')).toBe('screenshot.png')
  })

  it('does not show an empty text bubble for a references-only message', async () => {
    const { host } = await render(paths.join('\n'), paths)
    expect(host.querySelectorAll('.msg-local-path')).toHaveLength(2)
    expect(host.querySelector('.msg-user-bubble')).toBeNull()
  })

  it('preserves complete canonical text for copy and edit', async () => {
    const { host, message, copyMessage, onEdit } = await render(paths.join('\n'), paths)
    host.querySelector<HTMLButtonElement>('.msg-user-actions button')!.click()
    host.querySelector<HTMLButtonElement>('button[aria-label="Edit"]')!.click()
    expect(copyMessage).toHaveBeenCalledWith(message)
    expect(onEdit).toHaveBeenCalledWith(message)
    expect(message.text).toBe(paths.join('\n'))
  })

  it.each([undefined, ['/different/file.pdf']])('leaves unmarked or mismatching text unchanged', async refs => {
    const { host } = await render(paths.join('\n'), refs)
    expect(host.querySelectorAll('.msg-local-path')).toHaveLength(0)
    expect(host.querySelector('.msg-user-bubble')?.textContent?.trim()).toBe(paths.join('\n'))
  })
})
