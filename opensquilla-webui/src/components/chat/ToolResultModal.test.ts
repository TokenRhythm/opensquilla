// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, defineComponent, h, nextTick, ref } from 'vue'
import i18n from '@/i18n'
import type { ToolResultContext } from '@/types/chat'
import { copyTextWithFallback } from '@/utils/browser'
import ToolResultModal from './ToolResultModal.vue'

vi.mock('@/utils/browser', () => ({
  copyTextWithFallback: vi.fn().mockResolvedValue(undefined),
}))

async function mountToolResultModal(options: {
  title?: string
  content?: string
  context?: ToolResultContext
} = {}) {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const closeCount = ref(0)
  const isOpen = ref(true)
  const title = options.title ?? 'read_file · Result'
  const content = options.content ?? 'key: value'
  const Host = defineComponent({
    setup() {
      return () => h(ToolResultModal, {
        open: isOpen.value,
        title,
        content,
        context: options.context,
        onClose: () => {
          closeCount.value += 1
          isOpen.value = false
        },
      })
    },
  })
  const app = createApp(Host)
  app.use(i18n)
  app.mount(el)
  await nextTick()
  return { app, el, closeCount, isOpen }
}

beforeEach(() => {
  i18n.global.locale.value = 'en'
  document.body.innerHTML = ''
  vi.mocked(copyTextWithFallback).mockClear()
})

describe('ToolResultModal', () => {
  it('turns a read_file result into a file-aware code viewer', async () => {
    const content = [
      '---',
      'priority_bands:',
      '  high: 1.0',
    ].join('\n')
    const { app, el } = await mountToolResultModal({
      content,
      context: {
        toolName: 'read_file',
        section: 'result',
        inputRaw: JSON.stringify({ path: '/workspace/HEARTBEAT.yml' }),
      },
    })

    expect(el.querySelector('.tool-sheet__operation')?.textContent).toContain('read_file · Result')
    expect(el.querySelector('.tool-sheet__title')?.textContent).toBe('HEARTBEAT.yml')
    expect(el.querySelector('.tool-sheet__path')?.textContent).toBe('/workspace/HEARTBEAT.yml')
    expect(el.querySelector('.tool-sheet__meta')?.textContent).toContain('YAML · 3 lines')
    expect(el.querySelector('.tool-sheet__line-numbers')?.textContent?.trim()).toBe('1\n2\n3')
    const codeRegion = el.querySelector<HTMLElement>('.tool-sheet__code')
    expect(codeRegion?.classList.contains('tool-sheet__code--wrap')).toBe(false)
    expect(codeRegion?.getAttribute('role')).toBe('region')
    expect(codeRegion?.tabIndex).toBe(0)
    expect(codeRegion?.getAttribute('aria-label')).toContain('HEARTBEAT.yml · YAML · 3 lines')
    codeRegion?.focus()
    expect(document.activeElement).toBe(codeRegion)

    const wrapButton = el.querySelector<HTMLButtonElement>('button[aria-pressed]')
    expect(wrapButton?.textContent).toContain('Wrap lines')
    wrapButton?.click()
    await nextTick()

    expect(el.querySelector('.tool-sheet__code')?.classList.contains('tool-sheet__code--wrap')).toBe(true)
    expect(el.querySelector('.tool-sheet__line-numbers')).toBeNull()
    expect(wrapButton?.textContent).toContain('Preserve line width')
    app.unmount()
  })

  it('keeps the JSON tree scroll region keyboard focusable', async () => {
    const { app, el } = await mountToolResultModal({ content: '{"status":"ok"}' })
    const treeRegion = el.querySelector<HTMLElement>('.tool-sheet__tree')

    expect(treeRegion?.getAttribute('role')).toBe('region')
    expect(treeRegion?.tabIndex).toBe(0)
    treeRegion?.focus()
    expect(document.activeElement).toBe(treeRegion)
    app.unmount()
  })

  it('does not label read_file errors as the target file language', async () => {
    const { app, el } = await mountToolResultModal({
      content: 'ENOENT: no such file or directory',
      context: {
        toolName: 'read_file',
        section: 'error',
        inputRaw: JSON.stringify({ path: '/workspace/missing.json' }),
      },
    })

    expect(el.querySelector('.tool-sheet__title')?.textContent).toBe('missing.json')
    expect(el.querySelector('.tool-sheet__meta')?.textContent).toContain('Text · 1 lines')
    expect(el.querySelector('.tool-sheet__pre .hljs')).toBeNull()
    app.unmount()
  })

  it('renders file change details as a diff', async () => {
    const { app, el } = await mountToolResultModal({
      content: '--- before\n+++ after\n@@\n-old\n+new',
      context: {
        toolName: 'edit_file',
        section: 'input',
        format: 'diff',
      },
    })

    expect(el.querySelector('.tool-sheet__meta')?.textContent).toContain('Diff · 5 lines')
    expect(el.querySelector('.tool-sheet__pre .language-diff')).not.toBeNull()
    app.unmount()
  })

  it('copies the complete raw content and updates the button state', async () => {
    const content = 'first\nsecond'
    const { app, el } = await mountToolResultModal({ content })
    const copyButton = el.querySelector<HTMLButtonElement>('button[title="Copy"]')

    expect(copyButton).not.toBeNull()
    copyButton?.click()
    await Promise.resolve()
    await nextTick()

    expect(copyTextWithFallback).toHaveBeenCalledWith(content)
    expect(copyButton?.title).toBe('Copied')
    app.unmount()
  })

  it('keeps raw tool content inert and closes on Escape', async () => {
    const { app, el, closeCount } = await mountToolResultModal({
      content: 'markup: <img src=x onerror="window.__pwned = true">',
    })

    expect(el.querySelector('.tool-sheet__pre code')?.textContent).toContain('<img src=x')
    expect(el.querySelector('.tool-sheet__pre img')).toBeNull()

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await nextTick()

    expect(closeCount.value).toBe(1)
    expect(el.querySelector('.tool-sheet')).toBeNull()
    app.unmount()
  })
})
