// @vitest-environment happy-dom
import { afterEach, describe, expect, it } from 'vitest'
import { createApp, defineComponent, h, nextTick, ref, type App } from 'vue'
import { createI18n } from 'vue-i18n'
import { useMarkdownImageLightbox } from './useMarkdownImageLightbox'
import { hasOpenDialogLayer, useDialogA11y, useNativeSurfaceOcclusionState } from '@/composables/useDialogA11y'

const apps: App[] = []
const raster = 'data:image/png;base64,iVBORw0KGgo='

function mountImages(html = '<img src="https://example.com/a.png" alt="chart">', upperDialog = false) {
  let lightbox!: ReturnType<typeof useMarkdownImageLightbox>
  let occluded!: ReturnType<typeof useNativeSurfaceOcclusionState>
  const upperOpen = ref(false)
  const i18n = createI18n({
    legacy: false,
    locale: 'en',
    messages: {
      en: { chat: { previewOf: 'Preview: {title}', closePreview: 'Close preview' } },
      zh: { chat: { previewOf: '预览：{title}', closePreview: '关闭预览' } },
    },
  })
  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp(defineComponent({
    setup() {
      lightbox = useMarkdownImageLightbox()
      occluded = useNativeSurfaceOcclusionState()
      const upperRoot = ref<HTMLElement | null>(null)
      if (upperDialog) useDialogA11y(upperRoot, upperOpen, () => { upperOpen.value = false })
      return () => h('div', [
        h('div', { class: 'image-owner', innerHTML: html }),
        upperOpen.value
          ? h('section', { ref: upperRoot, 'aria-label': 'Upper dialog' }, [h('button', 'Upper close')])
          : null,
      ])
    },
  }))
  app.use(i18n)
  app.mount(host)
  apps.push(app)
  const root = host.querySelector<HTMLElement>('.image-owner')!
  lightbox.decorate(root)
  return { app, root, lightbox, occluded, upperOpen, i18n }
}

async function flushDialog() {
  await nextTick()
  await nextTick()
  await nextTick()
}

function key(key: string, target: EventTarget = document, shiftKey = false) {
  const event = new KeyboardEvent('keydown', { key, shiftKey, bubbles: true, cancelable: true })
  target.dispatchEvent(event)
  return event
}

afterEach(() => {
  for (const app of apps.splice(0)) app.unmount()
  document.body.innerHTML = ''
})

describe('owned Markdown image preview', () => {
  it('opens public URLs from the keyboard and uses the shared focus, Escape and native layer lifecycle', async () => {
    const state = mountImages()
    const image = state.root.querySelector('img')!
    expect(image.getAttribute('role')).toBe('button')
    expect(image.getAttribute('tabindex')).toBe('0')
    expect(image.getAttribute('aria-haspopup')).toBe('dialog')
    image.focus()
    expect(key('Enter', image).defaultPrevented).toBe(true)
    await flushDialog()

    const dialog = document.querySelector('.md-img-lightbox')!
    const close = dialog.querySelector<HTMLButtonElement>('button')!
    expect(dialog.getAttribute('role')).toBe('dialog')
    expect(dialog.getAttribute('aria-modal')).toBe('true')
    expect(dialog.getAttribute('aria-label')).toBe('Preview: chart')
    expect(dialog.querySelector('img')?.getAttribute('src')).toBe('https://example.com/a.png')
    expect(document.activeElement).toBe(close)
    expect(hasOpenDialogLayer()).toBe(true)
    expect(state.occluded.value).toBe(true)

    expect(key('Tab').defaultPrevented).toBe(true)
    expect(document.activeElement).toBe(close)
    expect(key('Tab', document, true).defaultPrevented).toBe(true)
    expect(document.activeElement).toBe(close)
    key('Escape')
    await flushDialog()
    expect(document.querySelector('.md-img-lightbox')).toBeNull()
    expect(document.activeElement).toBe(image)
    expect(hasOpenDialogLayer()).toBe(false)
    expect(state.occluded.value).toBe(false)

    expect(key(' ', image).defaultPrevented).toBe(true)
    await flushDialog()
    expect(document.querySelector('.md-img-lightbox')).not.toBeNull()
    document.querySelector<HTMLButtonElement>('.md-img-lightbox__close')!.click()
    await flushDialog()
    expect(document.activeElement).toBe(image)
  })

  it('respects an upper shared dialog instead of closing the lower image preview on Escape', async () => {
    const state = mountImages(undefined, true)
    state.root.querySelector('img')!.click()
    await flushDialog()
    state.upperOpen.value = true
    await flushDialog()
    expect(document.activeElement?.textContent).toBe('Upper close')
    key('Escape')
    await flushDialog()
    expect(state.upperOpen.value).toBe(false)
    expect(document.querySelector('.md-img-lightbox')).not.toBeNull()
    expect(state.occluded.value).toBe(true)
    key('Escape')
    await flushDialog()
    expect(document.querySelector('.md-img-lightbox')).toBeNull()
    expect(state.occluded.value).toBe(false)
  })

  it('preserves small raster data URLs and leaves rejected sources inert', async () => {
    const state = mountImages(`<img src="${raster}" alt="inline"><img src="file:///C:/private.png"><img src="data:image/svg+xml;base64,PHN2Zz4=">`)
    const images = state.root.querySelectorAll('img')
    expect(images[0]!.getAttribute('role')).toBe('button')
    expect(images[1]!.getAttribute('role')).toBeNull()
    expect(images[2]!.getAttribute('role')).toBeNull()
    images[0]!.click()
    await flushDialog()
    expect(document.querySelector('.md-img-lightbox img')?.getAttribute('src')).toBe(raster)
  })

  it('updates trigger and open dialog labels on locale changes without replacing the source', async () => {
    const state = mountImages()
    const image = state.root.querySelector('img')!
    image.click()
    await flushDialog()
    state.i18n.global.locale.value = 'zh'
    await flushDialog()
    expect(image.getAttribute('aria-label')).toBe('预览：chart')
    expect(document.querySelector('.md-img-lightbox')?.getAttribute('aria-label')).toBe('预览：chart')
    expect(document.querySelector('.md-img-lightbox__close')?.getAttribute('aria-label')).toBe('关闭预览')
    expect(document.querySelector('.md-img-lightbox img')?.getAttribute('src')).toBe('https://example.com/a.png')
  })

  it('keeps incremental controls idempotent and replaces an open preview within the same owner', async () => {
    const state = mountImages()
    const first = state.root.querySelector('img')!
    state.lightbox.decorate(state.root)
    first.click()
    await flushDialog()
    const second = document.createElement('img')
    second.src = 'https://example.com/b.png'
    second.alt = 'second'
    state.root.appendChild(second)
    state.lightbox.decorate(state.root)
    second.click()
    await flushDialog()
    expect(document.querySelectorAll('.md-img-lightbox')).toHaveLength(1)
    expect(document.querySelector('.md-img-lightbox img')?.getAttribute('src')).toBe('https://example.com/b.png')
    key('Escape')
    await flushDialog()
    expect(document.activeElement).toBe(second)
  })

  it('closes only its own preview on reset and removes its image listeners and affordances', async () => {
    const first = mountImages()
    const second = mountImages('<img src="https://example.com/b.png" alt="second">')
    const image = first.root.querySelector('img')!
    image.click()
    await flushDialog()
    second.lightbox.reset()
    await flushDialog()
    expect(document.querySelector('.md-img-lightbox')).not.toBeNull()
    first.lightbox.reset()
    await flushDialog()
    expect(document.querySelector('.md-img-lightbox')).toBeNull()
    expect(image.getAttribute('role')).toBeNull()
    expect(image.getAttribute('tabindex')).toBeNull()
    expect(image.classList.contains('md-img')).toBe(false)
    image.click()
    await flushDialog()
    expect(document.querySelector('.md-img-lightbox')).toBeNull()
    first.lightbox.decorate(first.root)
    image.click()
    await flushDialog()
    expect(document.querySelector('.md-img-lightbox')).not.toBeNull()
  })

  it('releases open and pending previews when the owner unmounts', async () => {
    const state = mountImages()
    state.root.querySelector('img')!.click()
    await flushDialog()
    expect(state.occluded.value).toBe(true)
    state.app.unmount()
    apps.splice(apps.indexOf(state.app), 1)
    await flushDialog()
    expect(document.querySelector('.md-img-lightbox')).toBeNull()
    expect(hasOpenDialogLayer()).toBe(false)
    expect(state.occluded.value).toBe(false)

    const pending = mountImages()
    pending.root.querySelector('img')!.click()
    pending.app.unmount()
    apps.splice(apps.indexOf(pending.app), 1)
    await flushDialog()
    expect(document.querySelector('.md-img-lightbox')).toBeNull()
    expect(hasOpenDialogLayer()).toBe(false)
  })

  it('closes a preview whose source image is removed by the next decoration pass', async () => {
    const state = mountImages()
    const image = state.root.querySelector('img')!
    image.click()
    await flushDialog()
    image.remove()
    state.lightbox.decorate(state.root)
    await flushDialog()
    expect(document.querySelector('.md-img-lightbox')).toBeNull()
    expect(state.occluded.value).toBe(false)
  })
})
