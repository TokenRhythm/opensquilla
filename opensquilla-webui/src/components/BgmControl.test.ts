// @vitest-environment happy-dom
import type { App } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'

type Presentation = 'full' | 'pause-only'

const mountedApps: App<Element>[] = []

afterEach(() => {
  for (const app of mountedApps.splice(0)) app.unmount()
  document.body.innerHTML = ''
  vi.doUnmock('@/composables/useBgm')
  vi.clearAllMocks()
})

async function mountControl(options: {
  playing: boolean
  presentation?: Presentation
}) {
  vi.resetModules()
  const {
    createApp,
    defineComponent,
    h,
    nextTick,
    ref,
  } = await import('vue')

  const playing = ref(options.playing)
  const initBgm = vi.fn(async () => undefined)
  const toggle = vi.fn(async () => {
    playing.value = !playing.value
  })
  const bgm = {
    tracks: ref([{ id: 'stream', title: 'Test stream', src: 'https://example.test/audio' }]),
    playing,
    currentTrackId: ref('stream'),
    currentTitle: ref('Test stream'),
    volume: ref(0.5),
    localTrackTitle: ref(''),
    initBgm,
    toggle,
    selectTrack: vi.fn(async () => undefined),
    setVolume: vi.fn(),
    playLocalFile: vi.fn(async () => undefined),
  }

  vi.doMock('@/composables/useBgm', () => ({
    BGM_LOCAL_TRACK_ID: '__local__',
    useBgm: () => bgm,
  }))

  const i18n = (await import('@/i18n')).default
  i18n.global.locale.value = 'en'
  const BgmControl = (await import('./BgmControl.vue')).default
  const presentation = ref<Presentation | undefined>(options.presentation)
  const Harness = defineComponent({
    setup() {
      return () => h(BgmControl, presentation.value
        ? { presentation: presentation.value }
        : {})
    },
  })
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(Harness)
  app.use(i18n)
  app.mount(el)
  mountedApps.push(app)
  await nextTick()

  return {
    bgm,
    el,
    nextTick,
    presentation,
  }
}

function testId(el: Element, id: string): HTMLElement | null {
  return el.querySelector<HTMLElement>(`[data-testid="${id}"]`)
}

describe('BgmControl responsive presentation', () => {
  it('keeps the default full split control while paused', async () => {
    const { bgm, el, nextTick } = await mountControl({ playing: false })

    const toggle = testId(el, 'bgm-toggle')
    expect(toggle?.getAttribute('aria-label')).toBe('Play background music')
    expect(toggle?.getAttribute('aria-pressed')).toBe('false')
    expect(testId(el, 'bgm-menu-trigger')).toBeTruthy()

    testId(el, 'bgm-menu-trigger')?.click()
    await nextTick()
    expect(el.querySelector('[role="menu"]')).toBeTruthy()
    expect(testId(el, 'bgm-track-stream')).toBeTruthy()
    expect(bgm.initBgm).toHaveBeenCalledTimes(1)
  })

  it('keeps play/pause and the picker together in full presentation while playing', async () => {
    const { el } = await mountControl({ playing: true, presentation: 'full' })

    expect(testId(el, 'bgm-toggle')?.getAttribute('aria-label'))
      .toBe('Pause background music')
    expect(testId(el, 'bgm-toggle')?.getAttribute('aria-pressed')).toBe('true')
    expect(testId(el, 'bgm-menu-trigger')).toBeTruthy()
    expect(el.querySelector('.bgm-menu-wrap')?.getAttribute('data-presentation')).toBe('full')
  })

  it('stays mounted to initialize persisted state but renders no paused compact control', async () => {
    const { bgm, el } = await mountControl({ playing: false, presentation: 'pause-only' })

    expect(bgm.initBgm).toHaveBeenCalledTimes(1)
    expect(testId(el, 'bgm-toggle')).toBeNull()
    expect(testId(el, 'bgm-menu-trigger')).toBeNull()
    expect(el.querySelector('.bgm-menu-wrap')).toBeNull()
  })

  it('renders one direct pause action in pause-only presentation and removes it after use', async () => {
    const { bgm, el, nextTick } = await mountControl({
      playing: true,
      presentation: 'pause-only',
    })

    const toggle = testId(el, 'bgm-toggle')
    expect(toggle?.getAttribute('aria-label')).toBe('Pause background music')
    expect(el.querySelectorAll('[data-testid="bgm-toggle"]')).toHaveLength(1)
    expect(testId(el, 'bgm-menu-trigger')).toBeNull()
    expect(el.querySelector('[role="menu"]')).toBeNull()
    expect(el.querySelector('.bgm-menu-wrap')?.getAttribute('data-presentation'))
      .toBe('pause-only')

    toggle?.click()
    await nextTick()
    expect(bgm.toggle).toHaveBeenCalledTimes(1)
    expect(testId(el, 'bgm-toggle')).toBeNull()
  })

  it('does not restart playback from a stale pause-only button', async () => {
    const { bgm, el, nextTick } = await mountControl({
      playing: true,
      presentation: 'pause-only',
    })
    const staleButton = testId(el, 'bgm-toggle')

    bgm.playing.value = false
    staleButton?.click()

    expect(bgm.toggle).not.toHaveBeenCalled()
    await nextTick()
    expect(testId(el, 'bgm-toggle')).toBeNull()
  })

  it('closes the full picker and maps focus to the retained pause action', async () => {
    const { el, nextTick, presentation } = await mountControl({
      playing: true,
      presentation: 'full',
    })
    const menuTrigger = testId(el, 'bgm-menu-trigger') as HTMLButtonElement
    menuTrigger.focus()
    menuTrigger.click()
    await nextTick()
    expect(el.querySelector('[role="menu"]')).toBeTruthy()

    presentation.value = 'pause-only'
    await nextTick()
    await nextTick()

    const pause = testId(el, 'bgm-toggle')
    expect(el.querySelector('[role="menu"]')).toBeNull()
    expect(testId(el, 'bgm-menu-trigger')).toBeNull()
    expect(document.activeElement).toBe(pause)
  })

  it('closes the picker on Escape and restores the caret trigger', async () => {
    const { el, nextTick } = await mountControl({ playing: false })
    const trigger = testId(el, 'bgm-menu-trigger') as HTMLButtonElement
    trigger.focus()
    trigger.click()
    await nextTick()
    const track = testId(el, 'bgm-track-stream') as HTMLButtonElement
    track.focus()
    expect(el.querySelector('[data-chat-topbar-popover="bgm"]')).toBeTruthy()

    const escape = new KeyboardEvent('keydown', {
      key: 'Escape',
      bubbles: true,
      cancelable: true,
    })
    document.dispatchEvent(escape)
    await nextTick()

    expect(escape.defaultPrevented).toBe(true)
    expect(el.querySelector('[data-chat-topbar-popover="bgm"]')).toBeNull()
    expect(document.activeElement).toBe(trigger)
  })

  it('keeps outside focus when an outside click dismisses the picker', async () => {
    const { el, nextTick } = await mountControl({ playing: false })
    testId(el, 'bgm-menu-trigger')?.click()
    await nextTick()
    const outside = document.createElement('button')
    document.body.appendChild(outside)
    outside.focus()
    outside.click()
    await nextTick()

    expect(el.querySelector('[data-chat-topbar-popover="bgm"]')).toBeNull()
    expect(document.activeElement).toBe(outside)
  })
})
