// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick, reactive, ref, type App } from 'vue'
import i18n from '@/i18n'
import SettingsDialog from './SettingsDialog.vue'

let catalogApi: Record<string, any>
let routeState: any
let routerMock: Record<string, any>
let confirmState: ReturnType<typeof ref<boolean>>
const confirmAction = vi.fn()

vi.mock('@/composables/setup/useSetupCatalog', () => ({
  SETTINGS_SECTIONS: [
    { id: 'behavior', label: 'Behavior', icon: 'chat', group: 'preferences' },
  ],
  useSetupCatalog: () => catalogApi,
}))

vi.mock('vue-router', () => ({
  useRoute: () => routeState,
  useRouter: () => routerMock,
}))

vi.mock('@/composables/useConfirm', () => ({
  useConfirm: () => ({ confirm: confirmAction, confirmState }),
}))

vi.mock('@/platform', () => ({
  usePlatform: () => ({
    capabilities: { isDesktop: false, hasTerminalWorkflow: false },
  }),
}))

let app: App<Element> | null = null

function mockCatalog() {
  const section = ref('behavior')
  const saveAllPending = ref(true)
  const saveDirtySections = vi.fn()
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
    dirtySections: ref([{ id: 'behavior', label: 'Behavior' }]),
    hasUnsavedChanges: ref(true),
    saveAllPending,
    saveDirtySections,
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
  return { saveAllPending, saveDirtySections, discardChanges, setAutoSessionTitles }
}

async function mountDialog() {
  const el = document.createElement('div')
  document.body.appendChild(el)
  app = createApp(SettingsDialog)
  app.use(i18n)
  app.mount(el)
  await nextTick()
  await nextTick()
  return el
}

beforeEach(() => {
  i18n.global.locale.value = 'en'
  confirmState = ref(false)
  confirmAction.mockReset()
  routeState = reactive({
    params: { section: 'behavior' },
    hash: '',
    path: '/settings/behavior',
  })
  routerMock = {
    options: { history: { state: { back: '/sessions' } } },
    replace: vi.fn(async () => undefined),
    push: vi.fn(async () => undefined),
    beforeEach: vi.fn(() => vi.fn()),
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
  it('locks settings edits and dirty-bar actions while showing progress', async () => {
    const controls = mockCatalog()
    const el = await mountDialog()

    const body = el.querySelector<HTMLElement>('.settings-body')
    const fieldset = el.querySelector<HTMLFieldSetElement>('.settings-panel__interactions')
    const close = el.querySelector<HTMLButtonElement>('.settings-modal__head button')
    const dirtyButtons = el.querySelectorAll<HTMLButtonElement>('.settings-dirtybar button')

    expect(body?.hasAttribute('inert')).toBe(true)
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

    controls.saveAllPending.value = false
    await nextTick()

    expect(body?.hasAttribute('inert')).toBe(false)
    expect(fieldset?.disabled).toBe(false)
    expect(close?.disabled).toBe(false)
    expect(Array.from(dirtyButtons).every(button => !button.disabled)).toBe(true)
    dirtyButtons[1]?.click()
    expect(controls.saveDirtySections).toHaveBeenCalledOnce()
  })
})
