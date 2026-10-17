// @vitest-environment happy-dom
import { createApp, defineComponent, h, nextTick, ref } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createI18n } from 'vue-i18n'
import type { ChatStreamTimelineItem } from '@/types/chat'
import RunTrace from './RunTrace.vue'

const owners = vi.hoisted(() => ({ instances: new Map<HTMLElement, { dispose: ReturnType<typeof vi.fn> }>() }))
vi.mock('@/utils/markdown/mermaidRenderer', () => ({
  resolvedDiagramThemeId: () => 'light',
  restoreMermaidSources: vi.fn(),
  renderMarkdownDiagrams: async (root: HTMLElement, labels: { copyCode: string }, options: { onRendered: (owner: unknown) => void }) => {
    for (const pre of root.querySelectorAll('pre:has(code.language-mermaid)')) {
      const element = document.createElement('div')
      element.className = 'mermaid-diagram'
      const button = document.createElement('button')
      button.textContent = labels.copyCode
      element.append(button)
      const owner = { element, dispose: vi.fn(() => owners.instances.delete(element)) }
      owners.instances.set(element, owner)
      pre.replaceWith(element)
      options.onRendered(owner)
    }
  },
  updateMermaidLabels: (root: HTMLElement, labels: { copyCode: string }) => {
    for (const element of root.querySelectorAll<HTMLElement>('.mermaid-diagram')) {
      if (owners.instances.has(element)) element.querySelector('button')!.textContent = labels.copyCode
    }
  },
}))

afterEach(() => { document.body.innerHTML = ''; owners.instances.clear(); vi.unstubAllGlobals() })
const settle = async () => { await nextTick(); await Promise.resolve(); await nextTick() }
const textItem = (key: string, html: string): ChatStreamTimelineItem => ({ type: 'text', key, html })

describe('RunTrace Markdown ownership', () => {
  it('keeps a retained narration owner through sibling appends and refreshes only the changed body', async () => {
    vi.stubGlobal('IntersectionObserver', undefined)
    const messages = ref<ChatStreamTimelineItem[]>([
      textItem('first', '<pre><code class="language-mermaid">graph TD; A-->B</code></pre>'),
    ])
    const i18n = createI18n({ legacy: false, locale: 'en', missingWarn: false, fallbackWarn: false, messages: {
      en: { chat: { mermaid: { copyCode: 'Copy source' } } },
      zh: { chat: { mermaid: { copyCode: '复制源码' } } },
    } })
    const host = document.createElement('div')
    document.body.append(host)
    const app = createApp(defineComponent(() => () => h(RunTrace, {
      items: messages.value, presentation: 'activity',
      isToolGroupOpen: () => undefined, isToolItemOpen: () => undefined,
    }))).use(i18n)
    app.mount(host)
    try {
      await settle()
      const first = host.querySelector<HTMLElement>('.mermaid-diagram')!
      const firstOwner = owners.instances.get(first)!
      first.dataset.manualZoom = '2'
      messages.value = [...messages.value, textItem('second', '<p>New process message</p>')]
      await settle()
      expect(host.querySelector('.mermaid-diagram')).toBe(first)
      expect(firstOwner.dispose).not.toHaveBeenCalled()
      expect(first.dataset.manualZoom).toBe('2')
      i18n.global.locale.value = 'zh'
      await settle()
      expect(first.querySelector('button')?.textContent).toBe('复制源码')

      messages.value = [textItem('first', '<p>Updated body</p>'), messages.value[1]!]
      await settle()
      expect(firstOwner.dispose).toHaveBeenCalledOnce()
      expect(host.querySelector('.mermaid-diagram')).toBeNull()
      expect(host.textContent).toContain('Updated body')
      expect(host.textContent).toContain('New process message')
    } finally { app.unmount() }
    expect(owners.instances.size).toBe(0)
  })
})
