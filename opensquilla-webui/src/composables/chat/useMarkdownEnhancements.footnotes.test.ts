// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, defineComponent, h, nextTick, ref, type App } from 'vue'
import { useMarkdownEnhancements } from './useMarkdownEnhancements'
vi.mock('vue-i18n', () => ({ useI18n: () => ({ t: (key: string) => key, locale: ref('en') }) }))
vi.mock('@/utils/markdown/mermaidRenderer', () => ({ renderMarkdownDiagrams: vi.fn().mockResolvedValue(undefined), resolvedDiagramThemeId: () => 'light', restoreMermaidSources: vi.fn(), updateMermaidLabels: vi.fn() }))

const apps: App[] = []
const body = '<p>Text <a tabindex="0" data-markdown-footnote-ref="1">[1]</a></p><ol><li data-markdown-footnote="1">Note <a tabindex="0" data-markdown-footnote-back="1">↩</a></li></ol>'

async function mount(source: string, selector?: string) {
  const content = ref(source)
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(defineComponent({
    setup() {
      const root = ref<HTMLElement | null>(null)
      useMarkdownEnhancements(root, { version: () => content.value, selector })
      return () => h('div', { ref: root, innerHTML: content.value })
    },
  }))
  apps.push(app)
  app.mount(el)
  await nextTick()
  return { app, el, content }
}

afterEach(() => {
  apps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
  vi.restoreAllMocks()
})

describe('Markdown footnote surface lifecycle', () => {
  it('keeps repeated note numbers local to each trace text segment', async () => {
    const { el } = await mount(`<div class="msg-ai-text">${body}</div><div class="msg-ai-text">${body}</div>`, '.msg-ai-text')
    const notes = el.querySelectorAll<HTMLElement>('[data-markdown-footnote]')
    const links = el.querySelectorAll<HTMLElement>('[data-markdown-footnote-ref]')
    links[1]!.click()
    expect(document.activeElement).toBe(notes[1])
    el.querySelectorAll<HTMLElement>('[data-markdown-footnote-back]')[1]!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    expect(document.activeElement).toBe(links[1])
  })

  it('binds newly rendered content once and releases removed bodies', async () => {
    const { el, content } = await mount(`<div class="msg-ai-text">${body}</div>`, '.msg-ai-text')
    const staleLink = el.querySelector<HTMLElement>('[data-markdown-footnote-ref]')!
    const staleNote = el.querySelector<HTMLElement>('[data-markdown-footnote]')!
    const staleFocus = vi.spyOn(staleNote, 'focus')
    content.value = `<div class="msg-ai-text"><h3>Changed</h3>${body}</div>`
    await nextTick()
    staleLink.click()
    expect(staleFocus).not.toHaveBeenCalled()
    const target = el.querySelector<HTMLElement>('[data-markdown-footnote]')!
    const focus = vi.spyOn(target, 'focus')
    el.querySelector<HTMLElement>('[data-markdown-footnote-ref]')!.click()
    expect(focus).toHaveBeenCalledOnce()
  })

  it('releases delegated events when the Markdown surface unmounts', async () => {
    const { app, el } = await mount(body)
    const link = el.querySelector<HTMLElement>('[data-markdown-footnote-ref]')!
    const target = el.querySelector<HTMLElement>('[data-markdown-footnote]')!
    const focus = vi.spyOn(target, 'focus')
    app.unmount()
    apps.splice(apps.indexOf(app), 1)
    link.click()
    expect(focus).not.toHaveBeenCalled()
  })
})
