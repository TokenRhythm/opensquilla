// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, ref, type App } from 'vue'
import i18n from '@/i18n'
import { ARTIFACT_WORKBENCH_KEY } from '@/modules/artifactWorkbench'
import { useNativeSurfaceOcclusionState } from '@/composables/useDialogA11y'
import ImageCopyActions from './ImageCopyActions.vue'

const apps: App[] = []
const artifact = { id: 'vector', name: 'vector.svg', mime: 'image/svg+xml' }
const xml = '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="8"/>'

async function mount() {
  const unrelated = ref(0)
  const content = { fetchArtifact: vi.fn(async () => ({ ok: true, blob: new Blob([xml], { type: 'image/svg+xml' }) })) }
  const host = document.createElement('div')
  document.body.append(host)
  const app = createApp({ render: () => h('div', { 'data-refresh': unrelated.value }, [
    h(ImageCopyActions, { source: { kind: 'artifact', artifact }, sessionKey: 'fixture-session' }),
  ]) })
  app.use(i18n)
  app.provide(ARTIFACT_WORKBENCH_KEY, { content } as never)
  app.mount(host)
  apps.push(app)
  await nextTick()
  return { host, content, unrelated, app }
}

beforeEach(() => { i18n.global.locale.value = 'en' })
afterEach(() => {
  apps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('ImageCopyActions', () => {
  it('supports the SVG menu keyboard lifecycle across unrelated parent renders', async () => {
    const { host, unrelated } = await mount()
    const more = host.querySelector<HTMLButtonElement>('[data-testid="image-copy-more"]')!
    more.focus()
    more.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }))
    await nextTick()
    const menu = document.querySelector('[role="menu"]')!
    expect(document.activeElement).toBe(menu.querySelector('[role="menuitem"]'))
    expect(useNativeSurfaceOcclusionState().value).toBe(true)
    unrelated.value++
    await nextTick()
    expect(document.querySelector('[role="menu"]')).toBe(menu)
    menu.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await nextTick()
    expect(document.querySelector('[role="menu"]')).toBeNull()
    expect(document.activeElement).toBe(more)
    expect(useNativeSurfaceOcclusionState().value).toBe(false)
  })

  it('announces an unsupported clipboard without fetching or downloading', async () => {
    vi.stubGlobal('ClipboardItem', undefined)
    const { host, content } = await mount()
    host.querySelector<HTMLButtonElement>('[data-testid="copy-image"]')!.click()
    await vi.waitFor(() => expect(host.querySelector('.image-copy-actions')?.getAttribute('data-state')).toBe('error'))
    expect(host.querySelector('[role="status"]')?.textContent).toBe(i18n.global.t('imageClipboard.failed'))
    expect(content.fetchArtifact).not.toHaveBeenCalled()
  })
})
