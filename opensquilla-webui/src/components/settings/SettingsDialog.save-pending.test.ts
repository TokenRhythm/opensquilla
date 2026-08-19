// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick, reactive, ref, type App } from 'vue'
import { createPinia } from 'pinia'
import i18n, { loadLocaleMessages } from '@/i18n'
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
    { id: 'capabilities', label: 'Capabilities', icon: 'skills', client: false, group: 'ai' },
  ],
  useSetupCatalog: () => catalogApi,
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
    modelStrategyPanel: ref({}),
    presetPanel: ref(null),
    channelsPanel: ref({}),
    capabilitiesPanel: ref({}),
    hasSetupAction: ref(false),
    actionItems: ref([]),
    fixCommands: ref([]),
    handoffCommands: ref([]),
    recipeCommands: ref([]),
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

async function mountDialog() {
  const el = document.createElement('div')
  document.body.appendChild(el)
  app = createApp(SettingsDialog)
  app.use(i18n)
  app.use(createPinia())
  app.mount(el)
  await nextTick()
  await nextTick()
  return document.body
}

beforeEach(() => {
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

describe('SettingsDialog exit protection', () => {
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
