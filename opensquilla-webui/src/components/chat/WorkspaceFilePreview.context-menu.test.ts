// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, type App } from 'vue'
import i18n from '@/i18n'
import { WORKSPACE_FILES_KEY, type WorkspaceFile } from '@/modules/workspaceFiles'
import WorkspaceFilePreview from './WorkspaceFilePreview.vue'

vi.mock('@/platform', () => ({ usePlatform: () => ({ id: 'web', files: {} }) }))

const apps: App[] = []
const file: WorkspaceFile = { requestedPath: 'note.md', path: 'note.md', name: 'note.md', mime: 'text/markdown',
  size: 32, kind: 'text', workspaceBinding: 'synthetic-binding' }

async function mount() {
  const root = document.createElement('div')
  document.body.append(root)
  const app = createApp({ render: () => h(WorkspaceFilePreview, { file, sessionKey: 'synthetic', scope: 'synthetic' }) })
  app.use(i18n)
  app.provide(WORKSPACE_FILES_KEY, { resolve: vi.fn(), read: vi.fn().mockResolvedValue(new Blob(['Selected preview text\nSecond line'])) })
  app.mount(root)
  apps.push(app)
  await vi.waitFor(() => expect(document.querySelector('[role="dialog"] pre')?.textContent).toContain('Selected preview text'))
  return { panel: document.querySelector<HTMLElement>('[role="dialog"]')!, body: document.querySelector<HTMLPreElement>('[role="dialog"] pre')! }
}

function selectContents(element: Element) {
  const range = document.createRange()
  range.selectNodeContents(element)
  const selection = document.getSelection()!
  selection.removeAllRanges()
  selection.addRange(range)
}

function contextEvent(kind: 'mouse' | 'key' | 'shift-f10' = 'mouse') {
  return kind === 'mouse' ? new MouseEvent('contextmenu', { bubbles: true, cancelable: true })
    : new KeyboardEvent('keydown', { key: kind === 'key' ? 'ContextMenu' : 'F10', shiftKey: kind === 'shift-f10', bubbles: true, cancelable: true })
}

afterEach(() => {
  document.getSelection()?.removeAllRanges()
  apps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
})

describe('workspace fallback preview context menu', () => {
  it.each(['mouse', 'key', 'shift-f10'] as const)('preserves the native %s menu and selected text inside the preview', async kind => {
    const { body } = await mount()
    body.focus()
    selectContents(body.querySelector('code')!)
    const event = contextEvent(kind)
    body.dispatchEvent(event)
    await nextTick()
    expect(event.defaultPrevented).toBe(false)
    expect(document.querySelector('[role="menu"]')).toBeNull()
    expect(document.getSelection()?.toString()).toBe('Selected preview text\nSecond line')
    expect(document.activeElement).toBe(body)
  })

  it.each(['empty', 'outside', 'cross-boundary'] as const)('retains the file menu for an %s preview selection', async kind => {
    const { panel, body } = await mount()
    if (kind === 'outside') selectContents(panel.querySelector('strong')!)
    if (kind === 'cross-boundary') {
      const range = document.createRange()
      range.setStart(panel.querySelector('strong')!.firstChild!, 0)
      range.setEnd(body.querySelector('code')!.firstChild!, 8)
      document.getSelection()!.addRange(range)
    }
    const event = contextEvent()
    body.dispatchEvent(event)
    await nextTick()
    expect(event.defaultPrevented).toBe(true)
    expect(document.querySelector('[role="menu"]')?.textContent).toContain('Copy relative path')
  })

  it.each(['header', 'trigger'] as const)('keeps the %s file actions available while body text is selected', async target => {
    const { panel, body } = await mount()
    selectContents(body)
    if (target === 'header') panel.querySelector('header')!.dispatchEvent(contextEvent())
    else panel.querySelector<HTMLButtonElement>('.workspace-file-preview__actions button')!.click()
    await nextTick()
    expect(document.querySelector('[role="menu"]')?.textContent).toContain('Copy relative path')
  })

  it.each(['input', 'textarea', 'contenteditable'] as const)('does not replace the %s editing menu with file actions', async kind => {
    const { panel } = await mount()
    const control = document.createElement(kind === 'contenteditable' ? 'div' : kind)
    if (kind === 'contenteditable') control.contentEditable = 'true'
    panel.append(control)
    const event = contextEvent()
    control.dispatchEvent(event)
    await nextTick()
    expect(event.defaultPrevented).toBe(false)
    expect(document.querySelector('[role="menu"]')).toBeNull()
  })
})
