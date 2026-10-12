// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, reactive, type App } from 'vue'
import i18n, { loadLocaleMessages, type LocaleCode } from '@/i18n'
import TextPart from './TextPart.vue'

vi.mock('@/platform', () => ({ usePlatform: () => ({ id: 'web', files: {}, capabilities: {} }) }))
vi.mock('@/composables/useToasts', () => ({ useToasts: () => ({ pushToast: vi.fn() }) }))

const apps: App[] = []
afterEach(() => {
  apps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
  i18n.global.locale.value = 'en'
})

describe('missing source citation localization', () => {
  it('updates the existing inline warning in all six locales and retains source navigation', async () => {
    const state = reactive({ html: '<p>Available [1]; missing [2], [3].</p>' })
    const citation = vi.fn()
    const root = document.createElement('div')
    document.body.appendChild(root)
    const app = createApp({ render: () => h(TextPart, {
      part: { type: 'text', key: 'answer', rawText: '', html: state.html },
      sources: [{ sourceId: 1, url: 'https://example.test', title: 'Available source', domain: 'example.test' }],
      onCitation: citation,
    }) })
    apps.push(app)
    app.use(i18n)
    app.mount(root)
    await nextTick()
    const labels: Record<LocaleCode, string> = {
      en: 'Some citations do not map to available sources: [2], [3]',
      'zh-Hans': '部分引用没有对应的可用来源：[2], [3]',
      ja: '一部の引用に対応する参照元がありません：[2], [3]',
      fr: 'Certaines citations ne correspondent à aucune source disponible : [2], [3]',
      de: 'Für einige Quellenverweise sind keine Quellen verfügbar: [2], [3]',
      es: 'Algunas citas no corresponden a fuentes disponibles: [2], [3]',
    }
    for (const locale of Object.keys(labels) as LocaleCode[]) {
      await loadLocaleMessages(locale)
      i18n.global.locale.value = locale
      await nextTick()
      expect(root.querySelector('.msg-ai-citation-warning')?.textContent).toBe(labels[locale])
      expect(root.querySelectorAll('.citation-pill')).toHaveLength(1)
    }
    root.querySelector<HTMLButtonElement>('.citation-pill')!.click()
    expect(citation).toHaveBeenCalledWith(1)
    state.html = '<p>Only the available source [1].</p>'
    await nextTick()
    await nextTick()
    expect(root.querySelector('.msg-ai-citation-warning')).toBeNull()
  })
})
