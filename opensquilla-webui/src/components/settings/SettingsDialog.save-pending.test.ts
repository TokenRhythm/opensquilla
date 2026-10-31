// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick, reactive, ref, type App } from 'vue'
import { createPinia } from 'pinia'
import i18n, { loadLocaleMessages } from '@/i18n'
import { APP_SETTINGS_KEY, type AppSettings } from '@/modules/appSettings'
import { GATEWAY_ACCESS_KEY } from '@/modules/gatewayAccess'
import { OBSERVABILITY_KEY } from '@/modules/observability'
import { SETUP_WORKFLOW_KEY, type SetupWorkflow } from '@/modules/setupWorkflow'
import SettingsDialog from './SettingsDialog.vue'

let catalogApi: Record<string, any>
let routeState: any
let routerMock: Record<string, any>
let confirmState: ReturnType<typeof ref<boolean>>
let leaveGuard: ((to: { path: string }) => boolean | Promise<boolean>) | null
const confirmAction = vi.fn()
const confirmChoiceAction = vi.fn()

vi.mock('@/composables/setup/useSetupCatalog', () => ({
  SETTINGS_SECTIONS: [
    { id: 'general', label: 'General', icon: 'settings', client: false, group: 'preferences' },
    { id: 'gateway', label: 'Gateway', icon: 'home', client: false, group: null },
    { id: 'provider', label: 'Model Service', icon: 'settings', client: false, group: 'ai' },
    { id: 'capabilities', label: 'Capabilities', icon: 'skills', client: false, group: 'ai' },
  ],
  useSetupCatalog: () => catalogApi,
}))

vi.mock('@/components/setup/SetupCapabilitiesPanel.vue', () => ({
  default: { template: '<div data-testid="capabilities-panel"></div>' },
}))

vi.mock('vue-router', () => ({
  useRoute: () => routeState,
  useRouter: () => routerMock,
}))

vi.mock('@/composables/useConfirm', () => ({
  useConfirm: () => ({ confirm: confirmAction, confirmChoice: confirmChoiceAction, confirmState }),
}))

vi.mock('@/platform', () => {
  const platform = {
    id: 'web',
    capabilities: { isDesktop: false, hasTerminalWorkflow: false },
    gateway: {},
    files: {},
    workbench: { native: {} },
    getOsLocale: vi.fn(async () => 'en'),
    setNativeTheme: vi.fn(async () => undefined),
  }
  return {
    getPlatform: () => platform,
    usePlatform: () => platform,
  }
})

let app: App<Element> | null = null
const tailLogs = vi.fn()

function mockCatalog() {
  const section = ref('general')
  const saveAllPending = ref(true)
  const providerSavePending = ref(false)
  const providerDraftDirty = ref(false)
  const hasUnsavedChanges = ref(true)
  const saveDirtySections = vi.fn()
  const saveProvider = vi.fn(async () => true)
  const discardChanges = vi.fn()
  const setAutoSessionTitles = vi.fn()
  const noop = vi.fn()
  const base: Record<PropertyKey, any> = {
    section,
    setSection: (value: string) => { section.value = value },
    loaded: ref(true),
    providerPanel: ref({ credentialPanel: null, providerSelected: '' }),
    behaviorPanel: ref({
      autoSessionTitles: false,
      autoSessionTitlesDirty: true,
      statusText: 'Automatic titles are off.',
    }),
    privacyPanel: ref({}),
    memoryPanel: ref({ autoCapture: true }),
    recommendationPreview: ref(null), recommendationSaving: ref(false),
    routerRecommendation: ref(undefined), ensembleRecommendation: ref(undefined),
    modelStrategyPanel: ref({}),
    channelsPanel: ref({}),
    capabilitiesPanel: ref({}),
    hasSetupAction: ref(false),
    actionItems: ref([]),
    configSummary: ref([]),
    configPath: ref('/tmp/config.toml'),
    selectInitialSection: noop,
    sectionStatus: () => ({ label: 'Ready', tone: 'is-ok' }),
    sectionDirty: () => true,
    providerDraftDirty,
    dirtySections: ref([{ id: 'general', label: 'General' }]),
    hasUnsavedChanges,
    saveAllPending,
    providerSavePending,
    saveDirtySections,
    saveProvider,
    discardChanges,
    setAutoSessionTitles,
    copyCommand: noop,
    copyConfigPath: noop,
  }
  catalogApi = new Proxy(base, {
    get(target, property: string | symbol) {
      if (!(property in target)) target[property] = vi.fn()
      return target[property]
    },
  })
  return {
    saveAllPending,
    providerSavePending,
    providerDraftDirty,
    hasUnsavedChanges,
    saveDirtySections,
    saveProvider,
    discardChanges,
    setAutoSessionTitles,
  }
}

async function mountDialog(
  gatewayOverrides: Record<string, unknown> = {},
  appSettings?: AppSettings,
  setupWorkflow?: Partial<SetupWorkflow>,
) {
  const el = document.createElement('div')
  document.body.appendChild(el)
  app = createApp(SettingsDialog)
  app.use(i18n)
  app.use(createPinia())
  if (appSettings) app.provide(APP_SETTINGS_KEY, appSettings)
  if (setupWorkflow) app.provide(SETUP_WORKFLOW_KEY, setupWorkflow as SetupWorkflow)
  app.provide(GATEWAY_ACCESS_KEY, {
    availability: 'unavailable',
    requiresCredential: false,
    supportBundleUnavailableReason: 'disconnected',
    subscriptionEpoch: 0,
    loadConnectionEndpoint: () => 'ws://localhost:18790/ws',
    ...gatewayOverrides,
  } as never)
  app.provide(OBSERVABILITY_KEY, { downloadSupportBundle: vi.fn(), tailLogs } as never)
  app.mount(el)
  await nextTick()
  await nextTick()
  return document.body
}

beforeEach(() => {
  tailLogs.mockReset()
  i18n.global.locale.value = 'en'
  confirmState = ref(false)
  confirmAction.mockReset()
  confirmAction.mockResolvedValue(true)
  confirmChoiceAction.mockReset()
  confirmChoiceAction.mockResolvedValue('primary')
  leaveGuard = null
  routeState = reactive({
    params: { section: 'general' },
    hash: '',
    path: '/settings/general',
  })
  routerMock = {
    options: { history: { state: { back: '/sessions' } } },
    replace: vi.fn(async () => undefined),
    push: vi.fn(async () => undefined),
    beforeEach: vi.fn((guard) => {
      leaveGuard = guard
      return vi.fn()
    }),
  }
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: vi.fn(() => ({
      matches: false,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  })
  Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
    configurable: true,
    value: vi.fn(),
  })
  Object.defineProperty(HTMLElement.prototype, 'scrollTo', {
    configurable: true,
    value: vi.fn(),
  })
})

afterEach(() => {
  app?.unmount()
  app = null
  document.body.innerHTML = ''
})

describe('SettingsDialog save-all pending state', () => {
  it('shows the provider warning as a small dot only after status is loaded', async () => {
    mockCatalog()
    catalogApi.sectionDirty = () => false
    catalogApi.sectionStatus = () => ({ label: 'Needs action', tone: 'is-warn' })
    catalogApi.loaded.value = false
    const el = await mountDialog()
    const provider = el.querySelector('#settings-rail-provider')!
    expect(provider.querySelector('.settings-rail__dot')).toBeNull()
    expect(provider.querySelector('.settings-rail__warn')).toBeNull()

    catalogApi.loaded.value = true
    await nextTick()
    expect(provider.querySelector('.settings-rail__dot.is-danger')).not.toBeNull()
    expect(provider.getAttribute('aria-label')).toContain('Needs action')
    expect(provider.querySelector('.settings-rail__warn')).toBeNull()
  })

  it('omits status dots from optional sections and configured providers', async () => {
    mockCatalog()
    catalogApi.sectionDirty = () => false
    catalogApi.sectionStatus = (id: string) => id === 'capabilities'
      ? { label: 'Optional', tone: 'is-muted' }
      : { label: 'Ready', tone: 'is-ok' }
    const el = await mountDialog()
    expect(el.querySelector('#settings-rail-provider .settings-rail__dot')).toBeNull()
    expect(el.querySelector('#settings-rail-provider .settings-rail__warn')).toBeNull()
    expect(el.querySelectorAll('.settings-rail__item:not(#settings-rail-gateway) .settings-rail__dot')).toHaveLength(0)
    expect(el.querySelector('#settings-rail-capabilities')?.getAttribute('aria-label')).toContain('Optional')
    expect(el.querySelector('.settings-rail__warn')).toBeNull()
  })

  it('shows local section feedback instead of a blank loading pane', async () => {
    mockCatalog()
    catalogApi.loaded.value = false
    catalogApi.section.value = 'capabilities'
    routeState.params.section = 'capabilities'

    const el = await mountDialog()
    const loading = el.querySelector<HTMLElement>('.settings-loading')

    expect(loading?.textContent).toContain('Capabilities')
    expect(loading?.getAttribute('role')).toBe('status')
  })

  it('locks settings edits and dirty-bar actions while showing progress', async () => {
    const controls = mockCatalog()
    const el = await mountDialog()

    const body = el.querySelector<HTMLElement>('.settings-body')
    const overlay = el.querySelector<HTMLElement>('.settings-overlay')
    const main = el.querySelector<HTMLElement>('.settings-main')
    const heading = el.querySelector<HTMLElement>('.settings-modal__head')
    const fieldset = el.querySelector<HTMLFieldSetElement>('.settings-panel__interactions')
    const close = el.querySelector<HTMLButtonElement>('.settings-modal__head button')
    const dirtyButtons = el.querySelectorAll<HTMLButtonElement>('.settings-dirtybar button')

    expect(body?.hasAttribute('inert')).toBe(true)
    expect(overlay?.parentElement).toBe(document.body)
    expect(heading?.parentElement).toBe(main)
    expect(main?.firstElementChild).toBe(heading)
    expect(body?.getAttribute('aria-busy')).toBe('true')
    expect(fieldset?.disabled).toBe(true)
    expect(fieldset?.getAttribute('aria-busy')).toBe('true')
    expect(close?.disabled).toBe(true)
    expect(Array.from(dirtyButtons).every(button => button.disabled)).toBe(true)
    expect(dirtyButtons[1]?.getAttribute('aria-busy')).toBe('true')
    expect(el.querySelector('.settings-dirtybar')?.textContent).toContain('Saving changes…')

    dirtyButtons.forEach(button => button.click())
    close?.click()
    expect(controls.saveDirtySections).not.toHaveBeenCalled()
    expect(controls.discardChanges).not.toHaveBeenCalled()
    expect(confirmAction).not.toHaveBeenCalled()
    expect(confirmChoiceAction).not.toHaveBeenCalled()

    controls.saveAllPending.value = false
    await nextTick()

    expect(body?.hasAttribute('inert')).toBe(false)
    expect(fieldset?.disabled).toBe(false)
    expect(close?.disabled).toBe(false)
    expect(Array.from(dirtyButtons).every(button => !button.disabled)).toBe(true)
    dirtyButtons[1]?.click()
    expect(controls.saveDirtySections).toHaveBeenCalledOnce()
  })

  it('preserves the existing draft bar while locking its actions during routing autosave', async () => {
    const controls = mockCatalog()
    controls.saveAllPending.value = false
    catalogApi.modelStrategyRoutingBusy = ref(false)
    const el = await mountDialog()
    const bar = el.querySelector('.settings-dirtybar')!
    const draftText = bar.textContent
    const buttons = Array.from(bar.querySelectorAll<HTMLButtonElement>('button'))

    catalogApi.modelStrategyRoutingBusy.value = true
    await nextTick()

    expect(el.querySelector('.settings-dirtybar')).toBe(bar)
    expect(bar.textContent).toBe(draftText)
    expect(bar.textContent).not.toContain('Saving changes')
    expect(el.querySelector<HTMLFieldSetElement>('.settings-panel__interactions')?.disabled).toBe(true)
    expect(buttons.every(button => button.disabled)).toBe(true)
    buttons.forEach(button => button.click())
    expect(controls.saveDirtySections).not.toHaveBeenCalled()
    expect(controls.discardChanges).not.toHaveBeenCalled()

    catalogApi.modelStrategyRoutingBusy.value = false
    await nextTick()
    expect(el.querySelector('.settings-dirtybar')).toBe(bar)
    expect(bar.textContent).toBe(draftText)
    expect(buttons.every(button => !button.disabled)).toBe(true)
  })

  it('localizes the dirty section name instead of exposing its internal English label', async () => {
    const controls = mockCatalog()
    controls.saveAllPending.value = false
    catalogApi.dirtySections.value = [{ id: 'modelStrategy', label: 'Model Routing' }]
    await loadLocaleMessages('zh-Hans')
    i18n.global.locale.value = 'zh-Hans'

    const el = await mountDialog()
    const dirtyBar = el.querySelector('.settings-dirtybar')

    expect(dirtyBar?.textContent).toContain('模型路由中有未保存的更改')
    expect(dirtyBar?.textContent).toContain('放弃路由更改')
    expect(dirtyBar?.textContent).toContain('保存路由更改')
    expect(dirtyBar?.textContent).not.toContain('Model Routing')
  })
})

describe('SettingsDialog video draft exit guard', () => {
  it('loads video settings on first visit and refreshes only credential status on return', async () => {
    const controls = mockCatalog()
    controls.saveAllPending.value = false
    controls.hasUnsavedChanges.value = false
    catalogApi.dirtySections.value = []
    const readAll = vi.fn().mockResolvedValue({
      video_generation: {
        enabled: false,
        provider: '',
        primary: '',
        duration_seconds: null,
        max_duration_seconds: 8,
        aspect_ratio: '16:9',
        allowed_aspect_ratios: ['16:9', '9:16'],
        resolution: '720p',
        allowed_resolutions: ['720p', '1080p'],
        providers: {
          openrouter: { base_url: 'https://openrouter.ai/api/v1', api_key_env: 'OPENROUTER_API_KEY' },
          gemini: { base_url: 'https://generativelanguage.googleapis.com/v1beta', api_key_env: 'GEMINI_API_KEY' },
        },
      },
    })
    const catalog = vi.fn().mockResolvedValue({})
    const status = vi.fn()
      .mockResolvedValueOnce({ videoGenerationState: { credentialOptions: [] } })
      .mockResolvedValue({ videoGenerationState: { credentialOptions: [{
        providerId: 'openrouter', available: true, source: 'video_env', envKey: 'VIDEO_TEST_KEY',
      }] } })
    const el = await mountDialog({}, { readAll } as unknown as AppSettings, { catalog, status })
    expect(el.querySelector('[data-testid="video-generation-settings"]')).toBeNull()
    expect(catalog).not.toHaveBeenCalled()
    expect(readAll).not.toHaveBeenCalled()
    expect(status).not.toHaveBeenCalled()

    catalogApi.section.value = 'capabilities'
    await vi.waitFor(() => expect(status).toHaveBeenCalledOnce())
    const details = el.querySelector<HTMLDetailsElement>('[data-testid="video-generation-settings"]')!
    const provider = details.querySelector<HTMLSelectElement>('[name="setup_video_provider"]')!
    provider.value = 'openrouter'
    provider.dispatchEvent(new Event('change', { bubbles: true }))
    await nextTick()
    const model = details.querySelector<HTMLInputElement>('[name="setup_provider_video_primary"]')!
    model.value = 'google/veo-3.1-fast'
    model.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()

    catalogApi.section.value = 'general'
    await nextTick()
    expect(details.hidden).toBe(true)
    catalogApi.section.value = 'capabilities'
    await vi.waitFor(() => expect(status).toHaveBeenCalledTimes(2))
    expect(el.querySelector('[data-testid="video-generation-settings"]')).toBe(details)
    expect(model.value).toBe('google/veo-3.1-fast')
    expect(catalog).toHaveBeenCalledOnce()
    expect(readAll).toHaveBeenCalledOnce()
    expect(details.querySelector('.video-settings__credential-card .capability-card__credential-detail')?.textContent)
      .toContain(i18n.global.t('setup.video.videoEnvKeyAvailable', { name: 'VIDEO_TEST_KEY' }))
  })

  async function mountVideoDraft() {
    const controls = mockCatalog()
    controls.saveAllPending.value = false
    controls.hasUnsavedChanges.value = false
    catalogApi.dirtySections.value = []
    catalogApi.section.value = 'capabilities'
    routeState.params.section = 'capabilities'
    const patch = vi.fn().mockResolvedValue({ restartRequired: false })
    const settings = {
      readAll: vi.fn().mockResolvedValue({
        video_generation: {
          enabled: false,
          provider: '',
          primary: '',
          duration_seconds: null,
          max_duration_seconds: 8,
          aspect_ratio: '16:9',
          allowed_aspect_ratios: ['16:9', '9:16'],
          resolution: '720p',
          allowed_resolutions: ['720p', '1080p'],
        },
      }),
      patch,
    } as unknown as AppSettings
    const el = await mountDialog({}, settings)
    await Promise.resolve()
    await nextTick()
    const provider = el.querySelector<HTMLSelectElement>('[name="setup_video_provider"]')!
    provider.value = 'openrouter'
    provider.dispatchEvent(new Event('change', { bubbles: true }))
    await nextTick()
    const model = el.querySelector<HTMLInputElement>('[name="setup_provider_video_primary"]')!
    model.value = 'google/veo-3.1-fast'
    model.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()
    return { el, patch, controls }
  }

  it('includes a video-only draft in the shared save bar and capability rail', async () => {
    const { el, patch, controls } = await mountVideoDraft()
    const bar = el.querySelector('.settings-dirtybar')!
    expect(bar.textContent).toContain(i18n.global.t('settings.rail.capabilities'))
    expect(el.querySelector('#settings-rail-capabilities .settings-rail__dirty')).not.toBeNull()
    expect(el.querySelector('.video-settings__actions')).toBeNull()
    bar.querySelector<HTMLButtonElement>('button.btn--primary')!.click()

    await vi.waitFor(() => expect(el.querySelector('.settings-dirtybar')).toBeNull())
    expect(patch).toHaveBeenCalledExactlyOnceWith([
      { path: 'video_generation.provider', value: 'openrouter' },
      { path: 'video_generation.primary', value: 'google/veo-3.1-fast' },
    ])
    expect(controls.saveDirtySections).not.toHaveBeenCalled()
  })

  it('discards a video-only draft from the shared bar without reloading or writing', async () => {
    const { el, patch, controls } = await mountVideoDraft()
    el.querySelector<HTMLButtonElement>('.settings-dirtybar button')!.click()
    await nextTick()

    expect(el.querySelector('.settings-dirtybar')).toBeNull()
    expect(el.querySelector<HTMLSelectElement>('[name="setup_video_provider"]')?.value).toBe('')
    expect(controls.discardChanges).not.toHaveBeenCalled()
    expect(patch).not.toHaveBeenCalled()
    expect(await leaveGuard!({ path: '/sessions' })).toBe(true)
    expect(confirmChoiceAction).not.toHaveBeenCalled()
  })

  it('counts image and video edits as one capability section and locks both save stages', async () => {
    const { el, patch, controls } = await mountVideoDraft()
    controls.hasUnsavedChanges.value = true
    catalogApi.dirtySections.value = [{ id: 'capabilities', label: 'Capabilities' }]
    let finishCatalogSave!: () => void
    controls.saveDirtySections.mockImplementation(() => new Promise<void>(resolve => {
      finishCatalogSave = () => {
        controls.hasUnsavedChanges.value = false
        catalogApi.dirtySections.value = []
        resolve()
      }
    }))
    let finishVideoSave!: () => void
    patch.mockImplementation(() => new Promise(resolve => {
      finishVideoSave = () => resolve({ restartRequired: false })
    }))
    await nextTick()
    const bar = el.querySelector('.settings-dirtybar')!
    expect(bar.textContent).toContain(i18n.global.t('settings.dialog.unsavedIn', {
      sections: i18n.global.t('settings.rail.capabilities'),
    }))
    const save = bar.querySelector<HTMLButtonElement>('button.btn--primary')!
    save.click()
    await nextTick()
    expect(controls.saveDirtySections).toHaveBeenCalledOnce()
    expect(patch).not.toHaveBeenCalled()
    expect(save.disabled).toBe(true)
    expect(el.querySelector<HTMLFieldSetElement>('.settings-panel__interactions')?.disabled).toBe(true)
    save.click()
    expect(controls.saveDirtySections).toHaveBeenCalledOnce()

    finishCatalogSave()
    await vi.waitFor(() => expect(patch).toHaveBeenCalledOnce())
    expect(save.disabled).toBe(true)
    expect(await leaveGuard!({ path: '/sessions' })).toBe(false)
    finishVideoSave()
    await vi.waitFor(() => expect(el.querySelector('.settings-dirtybar')).toBeNull())
    expect(el.querySelector<HTMLFieldSetElement>('.settings-panel__interactions')?.disabled).toBe(false)
  })

  it('retains the video draft when a shared capability save does not complete', async () => {
    const { el, patch, controls } = await mountVideoDraft()
    controls.hasUnsavedChanges.value = true
    catalogApi.dirtySections.value = [{ id: 'capabilities', label: 'Capabilities' }]
    el.querySelector<HTMLButtonElement>('.settings-dirtybar button.btn--primary')!.click()
    await nextTick()
    await nextTick()

    expect(controls.saveDirtySections).toHaveBeenCalledOnce()
    expect(patch).not.toHaveBeenCalled()
    expect(el.querySelector('.settings-dirtybar')).not.toBeNull()
    await vi.waitFor(() => expect(el.querySelector<HTMLButtonElement>('.settings-dirtybar button.btn--primary')?.disabled).toBe(false))
  })

  it('keeps a failed video save editable and protected by the exit guard', async () => {
    const { el, patch } = await mountVideoDraft()
    catalogApi.section.value = 'general'
    routeState.params.section = 'general'
    await nextTick()
    patch.mockRejectedValueOnce(new Error('Video settings could not be saved'))
    el.querySelector<HTMLButtonElement>('.settings-dirtybar button.btn--primary')!.click()
    await vi.waitFor(() => expect(el.textContent).toContain('Video settings could not be saved'))
    await vi.waitFor(() => expect(catalogApi.section.value).toBe('capabilities'))
    expect(el.querySelector<HTMLDetailsElement>('[data-testid="video-generation-settings"]')?.open).toBe(true)
    expect(el.querySelector<HTMLDetailsElement>('[data-testid="video-generation-settings"]')?.hidden).toBe(false)
    expect(document.activeElement?.textContent).toContain('Video settings could not be saved')
    expect(el.querySelector('.settings-dirtybar')).not.toBeNull()
    expect(el.querySelector<HTMLButtonElement>('.settings-dirtybar button.btn--primary')?.disabled).toBe(false)

    confirmChoiceAction.mockResolvedValueOnce('cancel')
    expect(await leaveGuard!({ path: '/sessions' })).toBe(false)
    expect(confirmChoiceAction).toHaveBeenCalledOnce()
    expect(patch).toHaveBeenCalledOnce()
  })

  it('preserves an unsaved video draft across sections and saves it on exit', async () => {
    const { el, patch } = await mountVideoDraft()
    catalogApi.section.value = 'general'
    await nextTick()
    expect(el.querySelector<HTMLInputElement>('[name="setup_provider_video_primary"]')?.value).toBe('google/veo-3.1-fast')
    expect(el.querySelector<HTMLDetailsElement>('[data-testid="video-generation-settings"]')?.hidden).toBe(true)

    const allowed = await leaveGuard!({ path: '/sessions' })

    expect(allowed).toBe(true)
    expect(confirmChoiceAction).toHaveBeenCalledOnce()
    expect(patch).toHaveBeenCalledExactlyOnceWith([
      { path: 'video_generation.provider', value: 'openrouter' },
      { path: 'video_generation.primary', value: 'google/veo-3.1-fast' },
    ])
  })

  it('lets an operator discard an unsaved video draft on exit without writing', async () => {
    const { patch } = await mountVideoDraft()
    confirmChoiceAction.mockResolvedValueOnce('secondary')

    const allowed = await leaveGuard!({ path: '/sessions' })

    expect(allowed).toBe(true)
    expect(confirmChoiceAction).toHaveBeenCalledOnce()
    expect(patch).not.toHaveBeenCalled()
  })
})

describe('SettingsDialog search navigation', () => {
  it.each([
    ['setup.connection.tokenLabel', '#conn-ws-token'],
    ['setup.connection.wsUrlLabel', '#conn-ws-url'],
  ])('opens collapsed connection details before focusing %s', async (labelKey, selector) => {
    mockCatalog().saveAllPending.value = false
    const el = await mountDialog({ availability: 'available', connectionPhase: 'healthy' })
    const search = el.querySelector<HTMLInputElement>('.settings-search input')!
    search.focus()
    search.value = i18n.global.t('settings.rail.gateway')
    search.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()
    el.querySelector<HTMLButtonElement>('.settings-search__result')!.click()
    await nextTick()
    expect(el.querySelector<HTMLDetailsElement>('#settings-connection-details')?.open).toBe(false)

    search.focus()
    search.value = i18n.global.t(labelKey)
    search.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()
    el.querySelector<HTMLButtonElement>('.settings-search__result')!.click()
    await nextTick()

    const input = el.querySelector<HTMLInputElement>(selector)
    const disclosure = el.querySelector<HTMLDetailsElement>('#settings-connection-details')
    expect(input).not.toBeNull()
    expect(disclosure?.open).toBe(true)
    expect(document.activeElement).toBe(input)
    expect(input?.value).toBe(selector === '#conn-ws-token' ? '' : 'ws://localhost:18790/ws')
    expect(routerMock.replace).toHaveBeenCalledWith({ path: '/settings/gateway' })
  })

  it('focuses the support container when offline download is disabled and catalog is not loaded', async () => {
    mockCatalog().saveAllPending.value = false
    catalogApi.loaded.value = false
    const el = await mountDialog()
    const search = el.querySelector<HTMLInputElement>('.settings-search input')!
    search.focus()
    search.value = i18n.global.t('monitorSupport.downloadBundle')
    search.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()
    el.querySelector<HTMLButtonElement>('.settings-search__result')!.click()
    await nextTick()

    const support = el.querySelector<HTMLElement>('#settings-gateway-support')
    expect(support).not.toBeNull()
    expect(support?.querySelector<HTMLButtonElement>('[data-testid="support-download-bundle"]')?.disabled).toBe(true)
    expect(document.activeElement).toBe(support)
    expect(routerMock.replace).toHaveBeenCalledWith({ path: '/settings/gateway' })
  })

  it('focuses the matching local control without changing its value', async () => {
    mockCatalog().saveAllPending.value = false
    const el = await mountDialog()
    const search = el.querySelector<HTMLInputElement>('.settings-search input')!
    search.focus()
    search.value = i18n.global.t('setup.behavior.autoTitlesLabel')
    search.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()
    el.querySelector<HTMLButtonElement>('.settings-search__result')!.click()
    await nextTick()

    expect(document.activeElement?.getAttribute('aria-label')).toBe(i18n.global.t('setup.behavior.autoTitlesLabel'))
    expect(catalogApi.setAutoSessionTitles).not.toHaveBeenCalled()
  })

  it('finds and focuses the disabled offline log entry without reading logs', async () => {
    mockCatalog().saveAllPending.value = false
    catalogApi.loaded.value = false
    const el = await mountDialog()
    const search = el.querySelector<HTMLInputElement>('.settings-search input')!
    search.focus()
    search.value = i18n.global.t('gatewayLogs.viewLogs')
    search.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()
    el.querySelector<HTMLButtonElement>('.settings-search__result')!.click()
    await nextTick()

    const logs = el.querySelector<HTMLElement>('#settings-gateway-logs')!
    expect(logs).not.toBeNull()
    expect(logs.querySelector<HTMLButtonElement>('[data-testid="support-view-logs"]')?.disabled).toBe(true)
    expect(document.activeElement).toBe(logs)
    expect(tailLogs).not.toHaveBeenCalled()
    expect(routerMock.replace).toHaveBeenCalledWith({ path: '/settings/gateway' })
  })

  it.each([
    ['#logs', 'settings-gateway-logs'],
    ['#support', 'settings-gateway-support'],
  ])('focuses %s on a cold link without reading logs', async (hash, targetId) => {
    mockCatalog().saveAllPending.value = false
    routeState.params.section = 'gateway'
    routeState.path = '/settings/gateway'
    routeState.hash = hash
    const el = await mountDialog()

    expect(document.activeElement).toBe(el.querySelector(`#${targetId}`))
    expect(tailLogs).not.toHaveBeenCalled()
  })

  it('focuses the panel for a section-only match instead of an unrelated action', async () => {
    mockCatalog().saveAllPending.value = false
    const el = await mountDialog()
    const search = el.querySelector<HTMLInputElement>('.settings-search input')!
    search.focus()
    search.value = 'general'
    search.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()
    el.querySelector<HTMLButtonElement>('.settings-search__result')!.click()
    await nextTick()

    expect(document.activeElement).toBe(el.querySelector('.settings-panel'))
  })
})

describe('SettingsDialog rail keyboard navigation', () => {
  async function mountRail(mobile = false) {
    const controls = mockCatalog()
    controls.saveAllPending.value = false
    controls.hasUnsavedChanges.value = false
    catalogApi.loaded.value = false
    Object.defineProperty(window, 'matchMedia', {
      configurable: true,
      value: vi.fn((query: string) => ({
        matches: mobile && query === '(max-width: 768px)',
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      })),
    })
    const el = await mountDialog()
    return { el, controls }
  }

  async function pressKey(element: HTMLElement, key: string) {
    element.focus()
    const event = new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true })
    element.dispatchEvent(event)
    await nextTick()
    return event
  }

  it.each([
    { mobile: false, forward: 'ArrowDown', backward: 'ArrowUp', ignored: 'ArrowRight' },
  ])('uses the visible rail orientation and one tab stop (mobile=$mobile)', async ({ mobile, forward, backward, ignored }) => {
    const { el } = await mountRail(mobile)
    const rail = el.querySelector<HTMLElement>('.settings-rail')!
    const tabs = Array.from(rail.querySelectorAll<HTMLButtonElement>('[role="tab"]'))
    expect(rail.getAttribute('aria-orientation')).toBe(mobile ? 'horizontal' : 'vertical')
    expect(tabs.map(tab => tab.tabIndex)).toEqual([0, -1, -1, -1])

    expect((await pressKey(tabs[0], ignored)).defaultPrevented).toBe(false)
    expect(catalogApi.section.value).toBe('general')
    expect((await pressKey(tabs[0], forward)).defaultPrevented).toBe(true)
    expect(document.activeElement).toBe(tabs[1])
    expect(catalogApi.section.value).toBe('gateway')
    expect(tabs[1].getAttribute('aria-selected')).toBe('true')
    expect(tabs.map(tab => tab.tabIndex)).toEqual([-1, 0, -1, -1])
    expect(routerMock.replace).toHaveBeenLastCalledWith({ path: '/settings/gateway' })

    await pressKey(tabs[1], 'End')
    expect(document.activeElement).toBe(tabs[3])
    expect(catalogApi.section.value).toBe('capabilities')
    await pressKey(tabs[3], forward)
    expect(document.activeElement).toBe(tabs[0])
    await pressKey(tabs[0], backward)
    expect(document.activeElement).toBe(tabs[3])
    await pressKey(tabs[3], 'Home')
    expect(document.activeElement).toBe(tabs[0])
    expect(tabs.map(tab => tab.tabIndex)).toEqual([0, -1, -1, -1])
  })

  it('does not navigate by keyboard while settings writes are pending', async () => {
    const { el, controls } = await mountRail()
    controls.saveAllPending.value = true
    await nextTick()
    const selected = el.querySelector<HTMLButtonElement>('[role="tab"][aria-selected="true"]')!
    await pressKey(selected, 'End')
    expect(catalogApi.section.value).toBe('general')
    expect(routerMock.replace).not.toHaveBeenCalled()
  })

  it('uses a mobile category list and keeps the detail form mounted on return', async () => {
    const { el, controls } = await mountRail(true)
    controls.hasUnsavedChanges.value = false
    await nextTick()
    const panel = el.querySelector<HTMLElement>('.settings-panel')!
    const form = panel.querySelector('.settings-panel__interactions')!
    panel.scrollTop = 120
    catalogApi.behaviorPanel.value.autoSessionTitles = true
    el.querySelector<HTMLButtonElement>('.settings-modal__back')!.click()
    await nextTick()
    const rail = el.querySelector<HTMLElement>('.settings-rail')!
    const categories = Array.from(rail.querySelectorAll<HTMLButtonElement>('.settings-rail__item'))
    expect(rail.getAttribute('role')).toBeNull()
    expect(categories.every(item => item.tabIndex === 0)).toBe(true)
    expect(panel.hasAttribute('inert')).toBe(true)
    expect(document.activeElement).toBe(categories[0])
    await pressKey(categories[0], 'ArrowDown')
    expect(document.activeElement).toBe(categories[1])
    expect(catalogApi.section.value).toBe('general')
    categories[0].click()
    await nextTick()
    expect(panel.hasAttribute('inert')).toBe(false)
    expect(rail.hasAttribute('inert')).toBe(true)
    expect(panel.querySelector('.settings-panel__interactions')).toBe(form)
    expect(panel.scrollTop).toBe(120)
    expect(catalogApi.behaviorPanel.value.autoSessionTitles).toBe(true)
    expect(document.activeElement).toBe(panel)
  })

  it('opens generic mobile Settings on categories, while a deep link opens its detail', async () => {
    mockCatalog().saveAllPending.value = false
    catalogApi.loaded.value = false
    routeState.params.section = undefined
    routeState.path = '/settings'
    Object.defineProperty(window, 'matchMedia', {
      configurable: true,
      value: vi.fn(() => ({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() })),
    })
    const el = await mountDialog()
    expect(el.querySelector('.settings-panel')?.hasAttribute('inert')).toBe(true)
    expect(el.querySelector('.settings-rail')?.hasAttribute('inert')).toBe(false)
    routeState.params.section = 'gateway'
    routeState.hash = '#connection'
    await nextTick()
    await nextTick()
    expect(el.querySelector('.settings-panel')?.hasAttribute('inert')).toBe(false)
    expect(el.querySelector('.settings-rail')?.hasAttribute('inert')).toBe(true)
    expect(document.activeElement?.id).toBe('settings-gateway-connection')
  })

  it('excludes hidden categories from the mobile detail focus loop', async () => {
    const { el, controls } = await mountRail(true)
    controls.hasUnsavedChanges.value = false
    await nextTick()
    const back = el.querySelector<HTMLButtonElement>('.settings-modal__back')!
    back.focus()
    back.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', shiftKey: true, bubbles: true, cancelable: true }))
    expect(document.activeElement?.closest('.settings-panel')).not.toBeNull()
    expect(document.activeElement?.closest('.settings-rail')).toBeNull()
    expect(document.activeElement?.closest('[inert]')).toBeNull()
  })
})

describe('SettingsDialog keyboard focus loop', () => {
  async function mountHealthyGateway() {
    const controls = mockCatalog()
    controls.saveAllPending.value = false
    controls.hasUnsavedChanges.value = false
    catalogApi.dirtySections.value = []
    const el = await mountDialog({ availability: 'available', connectionPhase: 'healthy' })
    const search = el.querySelector<HTMLInputElement>('.settings-search input')!
    search.focus()
    search.value = i18n.global.t('settings.rail.gateway')
    search.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()
    el.querySelector<HTMLButtonElement>('.settings-search__result')!.click()
    await nextTick()
    return el
  }

  function pressTab(element: HTMLElement, shiftKey = false) {
    element.focus()
    expect(document.activeElement).toBe(element)
    const event = new KeyboardEvent('keydown', { key: 'Tab', shiftKey, bubbles: true, cancelable: true })
    element.dispatchEvent(event)
    expect(event.defaultPrevented).toBe(true)
  }

  it.each([false, true])('wraps both ends of healthy Gateway with connection details open=%s', async (open) => {
    const el = await mountHealthyGateway()
    const disclosure = el.querySelector<HTMLDetailsElement>('#settings-connection-details')!
    expect(disclosure.open).toBe(false)
    if (open) {
      disclosure.open = true
      disclosure.dispatchEvent(new Event('toggle'))
      await nextTick()
    }
    const first = el.querySelector<HTMLButtonElement>('#settings-rail-gateway')!
    const last = disclosure.querySelector<HTMLElement>(open ? '.conn-actions button:last-child' : 'summary')!
    expect(last).not.toBeNull()

    pressTab(last)
    expect(document.activeElement).toBe(first)
    pressTab(first, true)
    expect(document.activeElement).toBe(last)
  })

  it('keeps the first summary subtree reachable while excluding nested content under closed ancestors', async () => {
    const el = await mountHealthyGateway()
    const disclosure = document.createElement('details')
    disclosure.innerHTML = `
      <summary>More options <button type="button">Summary action</button></summary>
      <details open><summary>Hidden inner summary</summary><button type="button">Hidden inner action</button></details>
      <summary tabindex="0">Hidden later summary</summary>
    `
    el.querySelector('.settings-modal')!.appendChild(disclosure)
    const first = el.querySelector<HTMLButtonElement>('#settings-rail-gateway')!
    const last = disclosure.querySelector<HTMLButtonElement>('summary button')!

    pressTab(first, true)
    expect(document.activeElement).toBe(last)
    pressTab(last)
    expect(document.activeElement).toBe(first)
  })
})

describe('SettingsDialog exit protection', () => {
  it.each(['docked', 'collapsed'])('restores the visible account trigger after its %s menu item unmounts', async (layout) => {
    const controls = mockCatalog()
    controls.saveAllPending.value = false
    controls.hasUnsavedChanges.value = false
    catalogApi.dirtySections.value = []
    const sidebar = document.createElement('div')
    sidebar.className = 'sidebar-foot'
    sidebar.innerHTML = '<button data-testid="sidebar-account-trigger">Account</button>'
    const dockedTrigger = sidebar.querySelector<HTMLButtonElement>('button')!
    const collapsedTrigger = document.createElement('button')
    collapsedTrigger.dataset.testid = 'sidebar-account-trigger'
    const opener = document.createElement('button')
    opener.dataset.testid = 'sidebar-toggle-collapsed'
    opener.style.display = 'none'
    const menuItem = document.createElement('button')
    menuItem.textContent = 'Settings'
    document.body.append(sidebar, collapsedTrigger, opener, menuItem)
    if (layout === 'collapsed') sidebar.setAttribute('inert', '')
    else collapsedTrigger.hidden = true
    menuItem.focus()
    await mountDialog()
    menuItem.remove()

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await nextTick()

    expect(document.activeElement).toBe(layout === 'collapsed' ? collapsedTrigger : dockedTrigger)
  })

  it.each(['inert', 'hidden', 'cold'])('restores the sidebar opener when the Settings entry is %s', async (state) => {
    const controls = mockCatalog()
    controls.saveAllPending.value = false
    controls.hasUnsavedChanges.value = false
    catalogApi.dirtySections.value = []
    const sidebar = document.createElement('div')
    sidebar.className = 'sidebar-foot'
    sidebar.innerHTML = '<button data-icon="settings">Settings</button>'
    const invoker = sidebar.querySelector<HTMLButtonElement>('button')!
    const opener = document.createElement('button')
    opener.dataset.testid = 'sidebar-toggle-collapsed'
    document.body.append(sidebar, opener)
    if (state !== 'cold') invoker.focus()
    const el = await mountDialog()
    if (state === 'hidden') sidebar.style.display = 'none'
    else sidebar.setAttribute('inert', '')
    el.querySelector<HTMLButtonElement>('.settings-modal__head button')?.click()
    await nextTick()
    expect(document.activeElement).toBe(opener)
  })

  it('does not leave a keyboard focus ring on Settings after a pointer close', async () => {
    const controls = mockCatalog()
    controls.saveAllPending.value = false
    controls.hasUnsavedChanges.value = false
    catalogApi.dirtySections.value = []
    const invoker = document.createElement('button')
    invoker.dataset.icon = 'settings'
    invoker.className = 'sidebar-fn-item'
    document.body.appendChild(invoker)
    invoker.focus()

    const el = await mountDialog()
    const close = el.querySelector<HTMLButtonElement>('.settings-modal__head button')!
    close.dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 1 }))
    await nextTick()

    expect(document.activeElement).not.toBe(invoker)
  })

  it('restores Settings focus for keyboard-style close activation', async () => {
    const controls = mockCatalog()
    controls.saveAllPending.value = false
    controls.hasUnsavedChanges.value = false
    catalogApi.dirtySections.value = []
    const invoker = document.createElement('button')
    invoker.dataset.icon = 'settings'
    invoker.className = 'sidebar-fn-item'
    document.body.appendChild(invoker)
    invoker.focus()

    const el = await mountDialog()
    el.querySelector<HTMLButtonElement>('.settings-modal__head button')?.click()
    await nextTick()

    expect(document.activeElement).toBe(invoker)
  })

  it('guards provider-only drafts on close and external navigation without adding the dirty bar', async () => {
    const controls = mockCatalog()
    controls.saveAllPending.value = false
    controls.hasUnsavedChanges.value = false
    controls.providerDraftDirty.value = true
    catalogApi.dirtySections.value = []
    confirmChoiceAction.mockResolvedValue('cancel')

    const el = await mountDialog()

    expect(el.querySelector('.settings-dirtybar')).toBeNull()
    expect(leaveGuard).toBeTypeOf('function')

    const internalResult = await leaveGuard!({ path: '/settings/modelStrategy' })
    expect(internalResult).toBe(true)
    expect(confirmChoiceAction).not.toHaveBeenCalled()

    const externalResult = await leaveGuard!({ path: '/sessions' })
    expect(externalResult).toBe(false)
    expect(confirmChoiceAction).toHaveBeenCalledOnce()

    confirmChoiceAction.mockClear()
    el.querySelector<HTMLButtonElement>('.settings-modal__head button')?.click()
    await nextTick()
    await Promise.resolve()

    expect(confirmChoiceAction).toHaveBeenCalledOnce()
    expect(el.querySelector('.settings-modal')).toBeTruthy()
    expect(routerMock.push).not.toHaveBeenCalled()
  })

  it('saves unsaved settings before closing when Save and close is chosen', async () => {
    const controls = mockCatalog()
    controls.saveAllPending.value = false
    controls.saveDirtySections.mockImplementation(() => {
      controls.hasUnsavedChanges.value = false
    })
    catalogApi.dirtySections.value = []
    confirmChoiceAction.mockResolvedValue('primary')

    const el = await mountDialog()
    el.querySelector<HTMLButtonElement>('.settings-modal__head button')?.click()
    await nextTick()
    await Promise.resolve()
    await nextTick()

    expect(confirmChoiceAction).toHaveBeenCalledWith(expect.objectContaining({
      primaryLabel: 'Save and close',
      secondaryLabel: 'Discard changes',
      showCancel: false,
    }))
    expect(controls.saveDirtySections).toHaveBeenCalledOnce()
    expect(el.querySelector('.settings-modal')).toBeTruthy()
  })

  it('uses the provider save path before closing a provider-only draft', async () => {
    const controls = mockCatalog()
    controls.saveAllPending.value = false
    controls.hasUnsavedChanges.value = false
    controls.providerDraftDirty.value = true
    controls.saveProvider.mockImplementation(async () => {
      controls.providerDraftDirty.value = false
      return true
    })
    catalogApi.dirtySections.value = []
    confirmChoiceAction.mockResolvedValue('primary')

    const el = await mountDialog()
    el.querySelector<HTMLButtonElement>('.settings-modal__head button')?.click()
    await nextTick()
    await Promise.resolve()
    await nextTick()

    expect(controls.saveProvider).toHaveBeenCalledOnce()
    expect(controls.saveDirtySections).not.toHaveBeenCalled()
  })

  it('blocks external exit while a provider save is pending', async () => {
    const controls = mockCatalog()
    controls.saveAllPending.value = false
    controls.hasUnsavedChanges.value = false
    controls.providerSavePending.value = true
    catalogApi.dirtySections.value = []

    const el = await mountDialog()
    const close = el.querySelector<HTMLButtonElement>('.settings-modal__head button')

    expect(close?.disabled).toBe(true)
    expect(await leaveGuard!({ path: '/sessions' })).toBe(false)
    expect(confirmAction).not.toHaveBeenCalled()
    expect(confirmChoiceAction).not.toHaveBeenCalled()
    expect(await leaveGuard!({ path: '/settings/provider' })).toBe(true)

    const unloadEvent = new Event('beforeunload', { cancelable: true })
    window.dispatchEvent(unloadEvent)
    expect(unloadEvent.defaultPrevented).toBe(true)
  })

  it('registers beforeunload only while a draft or save can be lost', async () => {
    const controls = mockCatalog()
    controls.saveAllPending.value = false
    controls.hasUnsavedChanges.value = false
    catalogApi.dirtySections.value = []
    await mountDialog()

    const cleanEvent = new Event('beforeunload', { cancelable: true })
    window.dispatchEvent(cleanEvent)
    expect(cleanEvent.defaultPrevented).toBe(false)

    controls.providerDraftDirty.value = true
    await nextTick()
    const dirtyEvent = new Event('beforeunload', { cancelable: true })
    window.dispatchEvent(dirtyEvent)
    expect(dirtyEvent.defaultPrevented).toBe(true)

    controls.providerDraftDirty.value = false
    await nextTick()
    const clearedEvent = new Event('beforeunload', { cancelable: true })
    window.dispatchEvent(clearedEvent)
    expect(clearedEvent.defaultPrevented).toBe(false)
  })
})
