// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, ref, type App } from 'vue'
import i18n from '@/i18n'
import { ARTIFACT_WORKBENCH_KEY } from '@/modules/artifactWorkbench'
import { useNativeSurfaceOcclusionState } from '@/composables/useDialogA11y'
import * as imageClipboard from '@/utils/imageClipboard'
import { getIconSvg } from '@/utils/icons'
import ImageCopyActions from './ImageCopyActions.vue'

const apps: App[] = []
const artifact = { id: 'vector', name: 'vector.svg', mime: 'image/svg+xml' }
const xml = '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="8"/>'

function iconMarkup(name: 'copy' | 'check') {
  const element = document.createElement('span')
  element.innerHTML = getIconSvg(name, 16)
  return element.innerHTML
}

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
  vi.useRealTimers()
})

describe('ImageCopyActions', () => {
  it('shows a temporary check after success without changing action labels or announcing success', async () => {
    vi.useFakeTimers()
    let finishWrite!: () => void
    vi.spyOn(imageClipboard, 'writePreparedClipboard').mockImplementation(() => new Promise<void>(resolve => {
      finishWrite = resolve
    }))
    const { host } = await mount()
    const button = host.querySelector<HTMLButtonElement>('[data-testid="copy-image"]')!
    const title = button.title
    const label = button.getAttribute('aria-label')
    expect(button.querySelector('.icon')?.innerHTML).toBe(iconMarkup('copy'))
    button.click()
    await nextTick()
    expect(button.disabled).toBe(true)
    expect(button.querySelector('.spinner')).not.toBeNull()
    expect(button.querySelector('.image-copy-actions__success')).toBeNull()

    finishWrite()
    await vi.advanceTimersByTimeAsync(0)
    expect(button.disabled).toBe(false)
    expect(button.querySelector('.icon')?.innerHTML).toBe(iconMarkup('check'))
    expect(host.querySelector('[role="status"]')?.textContent).toBe('')
    expect(host.querySelector('.image-copy-actions')?.getAttribute('data-state')).toBe('ok')
    expect(button.title).toBe(title)
    expect(button.getAttribute('aria-label')).toBe(label)
    await vi.advanceTimersByTimeAsync(1199)
    expect(button.querySelector('.icon')?.innerHTML).toBe(iconMarkup('check'))
    await vi.advanceTimersByTimeAsync(1)
    expect(button.querySelector('.icon')?.innerHTML).toBe(iconMarkup('copy'))
    expect(button.querySelector('.image-copy-actions__success')).toBeNull()
  })

  it('supports the SVG menu keyboard lifecycle across unrelated parent renders', async () => {
    vi.spyOn(imageClipboard, 'writePreparedClipboard').mockResolvedValue(undefined)
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
    more.click()
    await nextTick()
    document.querySelector<HTMLButtonElement>('[data-testid="copy-svg-source"]')!.click()
    await nextTick()
    await vi.waitFor(() => {
      expect(host.querySelector('[data-testid="copy-image"]')?.getAttribute('aria-busy')).toBe('false')
      expect(host.querySelector('[data-testid="copy-image"] .icon')?.innerHTML).toBe(iconMarkup('copy'))
    })
    expect(imageClipboard.writePreparedClipboard).toHaveBeenCalledWith('svg-source', expect.any(Function))
    expect(document.querySelector('[role="menu"]')).toBeNull()
    expect(host.querySelector('[role="status"]')?.textContent).toBe('')
  })

  it('announces an unsupported clipboard without fetching or downloading', async () => {
    vi.stubGlobal('ClipboardItem', undefined)
    const { host, content } = await mount()
    host.querySelector<HTMLButtonElement>('[data-testid="copy-image"]')!.click()
    await vi.waitFor(() => expect(host.querySelector('.image-copy-actions')?.getAttribute('data-state')).toBe('error'))
    expect(host.querySelector('[role="status"]')?.textContent).toBe(i18n.global.t('imageClipboard.failed'))
    expect(content.fetchArtifact).not.toHaveBeenCalled()
    expect(host.querySelector('[data-testid="copy-image"] .icon')?.innerHTML).toBe(iconMarkup('copy'))
  })
})
