// @vitest-environment happy-dom
import { createApp, h, nextTick, reactive, type Component } from 'vue'
import { createI18n } from 'vue-i18n'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createV4ArtifactPreviews } from '@/adapters/gateway/artifactPreviewsV4'
import { ARTIFACT_WORKBENCH_KEY, type ArtifactWorkbench } from '@/modules/artifactWorkbench'
import { httpBinaryResponse, httpTransportTestDouble, type TestHttpBinaryResponse } from '@/testing/httpTransport.test-helper'
import ArtifactPreviewPanel from './ArtifactPreviewPanel.vue'
import en from '@/locales/en.json'

// This component test consumes the already-sanitized placeholder contract.
// The actual parser/sanitizer and diagram engines have their own browser tests.
vi.mock('@/utils/markdown/renderCore', () => ({ renderMarkdownCore: (source: string) => source }))
vi.mock('@/utils/markdown/mermaidRenderer', () => ({ renderMarkdownDiagrams: vi.fn().mockResolvedValue(undefined), resolvedDiagramThemeId: () => 'light', restoreMermaidSources: vi.fn(), updateMermaidLabels: vi.fn() }))

async function settle() {
  for (let index = 0; index < 12; index += 1) { await Promise.resolve(); await nextTick() }
}
const image = (name: string) => `<span data-markdown-image="/api/v1/artifacts/${name}" data-markdown-image-alt="${name}">${name}</span>`
afterEach(() => { vi.restoreAllMocks(); document.body.innerHTML = '' })

describe('stable Markdown image publication', () => {
  it('shows ready images without rebuilding surrounding text, code controls, diagrams, focus or selection', async () => {
    let finishFirst!: (response: TestHttpBinaryResponse) => void
    let finishSecond!: (response: TestHttpBinaryResponse) => void
    const first = new Promise<TestHttpBinaryResponse>(resolve => { finishFirst = resolve })
    const second = new Promise<TestHttpBinaryResponse>(resolve => { finishSecond = resolve })
    const source = '<p>Keep this selected text</p><pre><code>const value = 1</code></pre>'
      + '<pre data-diagram-language="mermaid"><code>graph TD; A-->B;</code></pre>' + image('first') + image('second')
    const requestBinary = vi.fn(async (url: string) => url === '/api/v1/artifacts/notes'
      ? httpBinaryResponse(source, { contentType: 'text/markdown' })
      : url.endsWith('/first') ? first : second)
    let sequence = 0
    vi.spyOn(URL, 'createObjectURL').mockImplementation(() => `blob:ready-${++sequence}`)
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
    const host = document.createElement('div')
    document.body.append(host)
    const props = reactive({ artifact: { id: 'notes', name: 'notes.md', mime: 'text/markdown' }, sessionKey: 'session-a', showHeader: false })
    const app = createApp({ render: () => h(ArtifactPreviewPanel as Component, props) })
    app.use(createI18n({ legacy: false, locale: 'en', messages: { en } }))
    app.provide(ARTIFACT_WORKBENCH_KEY, {
      previews: createV4ArtifactPreviews(httpTransportTestDouble({ requestBinary }), { baseOrigin: () => 'http://localhost' }),
    } as ArtifactWorkbench)
    app.mount(host)
    try {
      await settle()
      const paragraph = host.querySelector('p')!
      const code = host.querySelector('pre > code')!
      const button = host.querySelector<HTMLButtonElement>('.code-copy-btn')!
      const diagram = host.querySelector<HTMLElement>('pre[data-diagram-language]')!
      const diagramViewport = document.createElement('div')
      diagramViewport.className = 'chat-diagram-viewport'
      diagram.append(diagramViewport)
      button.focus()
      const range = document.createRange()
      range.selectNodeContents(paragraph)
      const selection = window.getSelection()!
      selection.removeAllRanges()
      selection.addRange(range)

      finishFirst(httpBinaryResponse('first', { contentType: 'image/png' }))
      await settle()
      const firstImage = host.querySelector<HTMLImageElement>('[data-markdown-image$="/first"] img')!
      expect(firstImage?.getAttribute('src')).toBe('blob:ready-1')
      expect(host.querySelector('[data-markdown-image$="/second"] img')).toBeNull()
      expect(host.querySelector('p')).toBe(paragraph)
      expect(host.querySelector('pre > code')).toBe(code)
      expect(host.querySelector('.code-copy-btn')).toBe(button)
      expect(document.activeElement).toBe(button)
      expect(selection.anchorNode).toBe(paragraph)
      expect(selection.toString()).toBe('Keep this selected text')
      expect(diagram.querySelector('.chat-diagram-viewport')).toBe(diagramViewport)

      finishSecond(httpBinaryResponse('second', { contentType: 'image/png' }))
      await settle()
      expect(host.querySelector('[data-markdown-image$="/first"] img')).toBe(firstImage)
      expect(host.querySelectorAll('[data-markdown-image] img')).toHaveLength(2)
      expect(document.activeElement).toBe(button)
      expect(diagram.querySelector('.chat-diagram-viewport')).toBe(diagramViewport)
    } finally {
      app.unmount()
      host.remove()
    }
    expect(URL.revokeObjectURL).toHaveBeenCalledTimes(2)
  })
})
