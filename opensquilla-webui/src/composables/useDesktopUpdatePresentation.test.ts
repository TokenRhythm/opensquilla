// @vitest-environment happy-dom
import { computed, createApp, defineComponent, h, nextTick, ref } from 'vue'
import { describe, expect, it } from 'vitest'
import i18n, { loadLocaleMessages } from '@/i18n'
import type { DesktopUpdateState, DesktopUpdateStatus } from '@/platform'
import {
  desktopUpdateSeverity,
  useDesktopUpdatePresentation,
} from './useDesktopUpdatePresentation'

const BASE_STATE: DesktopUpdateState = {
  status: 'available',
  currentVersion: '1.0.0',
  latestVersion: '2.0.0',
  progress: null,
  checkedAt: null,
  error: null,
  errorCode: null,
  snoozedUntil: null,
  canCheck: true,
  canNativeInstall: true,
  installMode: 'native',
  releaseUrl: null,
  source: null,
  fallbackUsed: false,
}

describe('desktopUpdateSeverity', () => {
  it('keeps normal lifecycle states quiet and reserves danger for errors', () => {
    const expected: Record<DesktopUpdateStatus, string> = {
      idle: 'normal',
      checking: 'normal',
      available: 'info',
      downloading: 'info',
      downloaded: 'info',
      'not-available': 'normal',
      error: 'danger',
      applying: 'normal',
    }

    for (const [status, severity] of Object.entries(expected)) {
      expect(desktopUpdateSeverity(status as DesktopUpdateStatus)).toBe(severity)
    }
  })
})

describe('useDesktopUpdatePresentation', () => {
  it('reactively presents native, manual, downloading, and error states', async () => {
    i18n.global.locale.value = 'en'
    const state = ref<DesktopUpdateState>({ ...BASE_STATE })
    const actionBusy = ref(false)
    const source = {
      state,
      actionBusy,
      latestVersion: computed(() => state.value.latestVersion || state.value.currentVersion || ''),
      localizedError: computed(() => (
        state.value.errorCode ? 'Safe localized update error' : state.value.error || 'Fallback error'
      )),
    }
    let presentation!: ReturnType<typeof useDesktopUpdatePresentation>
    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp(defineComponent({
      setup() {
        presentation = useDesktopUpdatePresentation(source)
        return () => h('div')
      },
    }))
    app.use(i18n)
    app.mount(host)

    expect(presentation.summary.value).toBe('Update 2.0.0')
    expect(presentation.indicatorLabel).toBe(presentation.summary)
    expect(presentation.title.value).toBe('OpenSquilla 2.0.0 is available')
    expect(presentation.iconName.value).toBe('download')
    expect(presentation.severity.value).toBe('info')
    expect(presentation.manualInstall.value).toBe(false)
    expect(presentation.canInstall.value).toBe(true)
    expect(presentation.busy.value).toBe(false)

    state.value = { ...state.value, status: 'downloading', progress: 42.4 }
    await nextTick()
    expect(presentation.summary.value).toBe('Downloading 42%')
    expect(presentation.title.value).toBe('Downloading update')
    expect(presentation.iconName.value).toBe('refresh')
    expect(presentation.busy.value).toBe(true)

    state.value = {
      ...state.value,
      status: 'downloaded',
      progress: null,
      installMode: 'manual',
      canNativeInstall: false,
    }
    await nextTick()
    expect(presentation.summary.value).toBe('Installer verified')
    expect(presentation.title.value).toBe('Verified installer ready')
    expect(presentation.description.value).toContain('2.0.0')
    expect(presentation.iconName.value).toBe('check')
    expect(presentation.manualInstall.value).toBe(true)

    state.value = {
      ...state.value,
      status: 'error',
      errorCode: 'integrity_failed',
      error: 'raw transport detail',
      installMode: 'unsupported',
    }
    await nextTick()
    expect(presentation.summary.value).toBe('Update issue')
    expect(presentation.title.value).toBe('Update check failed')
    expect(presentation.description.value).toBe('Safe localized update error')
    expect(presentation.description.value).not.toContain('raw transport detail')
    expect(presentation.iconName.value).toBe('info')
    expect(presentation.severity.value).toBe('danger')
    expect(presentation.canInstall.value).toBe(false)

    app.unmount()
    host.remove()
  })

  it('recomputes localized presentation when the active locale changes', async () => {
    i18n.global.locale.value = 'en'
    const state = ref<DesktopUpdateState>({ ...BASE_STATE })
    const source = {
      state,
      actionBusy: ref(false),
      latestVersion: computed(() => '2.0.0'),
      localizedError: computed(() => ''),
    }
    let presentation!: ReturnType<typeof useDesktopUpdatePresentation>
    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp(defineComponent({
      setup() {
        presentation = useDesktopUpdatePresentation(source)
        return () => h('div')
      },
    }))
    app.use(i18n)
    app.mount(host)

    expect(presentation.summary.value).toBe('Update 2.0.0')
    await loadLocaleMessages('zh-Hans')
    i18n.global.locale.value = 'zh-Hans'
    await nextTick()
    expect(presentation.summary.value).toBe('更新 2.0.0')

    app.unmount()
    host.remove()
    i18n.global.locale.value = 'en'
  })
})
