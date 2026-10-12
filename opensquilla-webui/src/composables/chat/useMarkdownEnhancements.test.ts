// @vitest-environment happy-dom
import { createApp, defineComponent, h, nextTick, ref } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useMarkdownEnhancements } from './useMarkdownEnhancements'

const calls = vi.hoisted(() => ({ copy: vi.fn(), images: vi.fn(), resetImages: vi.fn(), disposeImages: vi.fn(), render: vi.fn(), restore: vi.fn(), labels: vi.fn() }))
const locale = ref('en')
vi.mock('vue-i18n', () => ({ useI18n: () => ({ locale, t: (key: string) => `${locale.value}:${key}` }) }))
vi.mock('@/utils/chat/codeBlockChrome', () => ({ decorateCodeBlocks: calls.copy }))
vi.mock('./useMarkdownImageLightbox', () => ({ useMarkdownImageLightbox: () => ({ decorate: calls.images, reset: calls.resetImages, dispose: calls.disposeImages }) }))
vi.mock('@/utils/markdown/mermaidRenderer', () => ({ renderMermaidBlocks: calls.render, resolvedDiagramThemeId: () => document.documentElement.getAttribute('data-theme') || 'light', restoreMermaidSources: calls.restore, updateMermaidLabels: calls.labels }))

let cleanup: (() => void) | undefined
beforeEach(() => {
  Object.values(calls).forEach(call => call.mockReset())
  calls.render.mockResolvedValue(undefined)
  locale.value = 'en'
  document.documentElement.setAttribute('data-theme', 'light')
})
afterEach(() => { cleanup?.(); cleanup = undefined; document.body.replaceChildren() })

function surface(incremental: boolean) {
  const blocks = ref<string[]>([])
  const resetEpoch = ref(0)
  let owner!: ReturnType<typeof useMarkdownEnhancements>
  const app = createApp(defineComponent({
    setup() {
      const root = ref<HTMLElement | null>(null)
      owner = useMarkdownEnhancements(root, { version: () => blocks.value.length, incremental, resetVersion: () => resetEpoch.value })
      return () => h('div', { ref: root }, [
        ...blocks.value.map((text, index) => h('div', { key: `${resetEpoch.value}:${index}`, class: 'streaming-rich-block', innerHTML: text })),
        h('span', { class: 'streaming-tail' }, 'unfinished'),
      ])
    },
  }))
  const mount = document.createElement('div')
  document.body.appendChild(mount)
  app.mount(mount)
  cleanup = () => app.unmount()
  return { blocks, resetEpoch, owner }
}

describe('mounted Markdown enhancement owner', () => {
  it('settles unexpected background failures without an unhandled rejection', async () => {
    calls.render.mockRejectedValueOnce(new Error('DOM unavailable'))
    const { blocks, owner } = surface(true)
    blocks.value.push('<pre><code>original source</code></pre>')
    await nextTick()
    await expect(owner.ready.value).resolves.toBeUndefined()
    expect(document.body.textContent).toContain('original source')
  })
  it('only scans each committed block once across 800 incremental appends', async () => {
    const { blocks, owner } = surface(true)
    await nextTick()
    for (let index = 0; index < 800; index++) {
      blocks.value.push(`<pre><code>block ${index}</code></pre>`)
      await nextTick()
    }
    await owner.ready.value
    expect(calls.copy).toHaveBeenCalledTimes(800)
    expect(calls.images).toHaveBeenCalledTimes(800)
    expect(calls.render).toHaveBeenCalledTimes(800)
    expect(new Set(calls.copy.mock.calls.map(call => call[0])).size).toBe(800)
    expect(calls.copy.mock.calls.every(call => call[0].classList.contains('streaming-rich-block'))).toBe(true)
  })
  it('invalidates old work on retract and disposal, including late callbacks', async () => {
    const submitted: { signal: AbortSignal, onRendered: (diagram: { element: HTMLElement, dispose: () => void }) => void, finish: () => void }[] = []
    calls.render.mockImplementation((_root, _labels, options) => new Promise<void>(resolve => submitted.push({ ...options, finish: resolve })))
    const { blocks, resetEpoch, owner } = surface(true)
    blocks.value.push('<pre><code>before</code></pre>')
    await nextTick()
    resetEpoch.value++
    blocks.value = ['<pre><code>after</code></pre>']
    await nextTick()
    expect(submitted[0].signal.aborted).toBe(true)
    const dispose = vi.fn()
    submitted[0].onRendered({ element: document.createElement('div'), dispose })
    expect(dispose).toHaveBeenCalledOnce()
    cleanup?.()
    cleanup = undefined
    expect(submitted[submitted.length - 1]?.signal.aborted).toBe(true)
    submitted.forEach(job => job.finish())
    await owner.ready.value
    expect(calls.disposeImages).toHaveBeenCalledOnce()
  })
  it('refreshes on theme change and updates labels on locale change', async () => {
    const { blocks } = surface(true)
    blocks.value.push('<pre><code>committed</code></pre>')
    await nextTick()
    document.documentElement.setAttribute('data-theme', 'dark')
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(calls.restore).toHaveBeenCalledOnce()
    expect(calls.copy).toHaveBeenCalledTimes(2)
    locale.value = 'de'
    await nextTick()
    expect(calls.labels).toHaveBeenCalledOnce()
    expect(calls.labels.mock.calls[0][1].copyCode).toBe('de:chat.mermaid.copyCode')
  })
})
