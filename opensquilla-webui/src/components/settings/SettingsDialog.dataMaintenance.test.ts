// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, defineComponent, nextTick, ref, type App } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import i18n from '@/i18n'
import SettingsDialog from './SettingsDialog.vue'

const mounted: App[] = []
const mocks = vi.hoisted(() => ({
  activeCatalog: null as Record<string, unknown> | null,
}))

vi.mock('@/composables/setup/useSetupCatalog', async () => {
  const { SETTINGS_SECTIONS } = await import('@/composables/setup/settingsSections')
  return {
    SETTINGS_SECTIONS,
    useSetupCatalog: () => mocks.activeCatalog,
  }
})
vi.mock('@/components/settings/SettingsAdvancedPanel.vue', () => ({
  default: {
    emits: ['open-agent-configuration', 'open-data-maintenance'],
    template: '<button type="button" data-testid="activate-data-maintenance" @click="$emit(\'open-data-maintenance\')">Open data maintenance</button>',
  },
}))
vi.mock('@/stores/rpc', () => ({
  useRpcStore: () => ({
    waitForConnection: vi.fn(async () => {}),
    supportsMethod: vi.fn(() => false),
    call: vi.fn(),
    isConnected: true,
    isConnecting: false,
  }),
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
vi.mock('@/composables/useConfirm', async () => {
  const { ref: createRef } = await import('vue')
  return {
    useConfirm: () => ({ confirm: vi.fn(async () => true), confirmState: createRef(null) }),
  }
})

async function settle(): Promise<void> {
  for (let i = 0; i < 8; i++) await Promise.resolve()
  await new Promise(resolve => setTimeout(resolve, 10))
}

async function mountDialog(path: '/settings/advanced' | '/settings/dataMigration') {
  document.body.innerHTML = ''

  const section = ref(path.endsWith('/dataMigration') ? 'dataMigration' : 'advanced')
  const noOp = vi.fn()
  const catalog = new Proxy<Record<string, unknown>>({
    section,
    setSection: (next: string) => { section.value = next },
    loaded: ref(true),
    providerPanel: ref({ runtimeProviders: [] }),
    behaviorPanel: ref({}),
    privacyPanel: ref({}),
    modelStrategyPanel: ref({}),
    presetPanel: ref({}),
    channelsPanel: ref({}),
    capabilitiesPanel: ref({}),
    hasSetupAction: ref(false),
    actionItems: ref([]),
    fixCommands: ref([]),
    handoffCommands: ref([]),
    recipeCommands: ref([]),
    configSummary: ref([]),
    configPath: ref(''),
    dirtySections: ref([]),
    hasUnsavedChanges: ref(false),
    saveAllPending: ref(false),
    sectionStatus: () => ({ label: 'Ready', tone: 'is-ok' }),
    sectionDirty: () => false,
  }, {
    get(target, key) {
      return Reflect.has(target, key) ? Reflect.get(target, key) : noOp
    },
  })

  mocks.activeCatalog = catalog

  const Empty = defineComponent({ template: '<div />' })
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/settings/:section?', component: Empty },
      { path: '/sessions', component: Empty },
      { path: '/chat', component: Empty },
    ],
  })
  await router.push(path)
  await router.isReady()

  i18n.global.locale.value = 'en'
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(SettingsDialog)
  app.use(router)
  app.use(i18n)
  app.mount(el)
  mounted.push(app)
  await settle()
  await nextTick()
  return { el: document.body, router }
}

afterEach(() => {
  while (mounted.length) mounted.pop()!.unmount()
  document.body.innerHTML = ''
  vi.restoreAllMocks()
})

describe('SettingsDialog nested data maintenance focus', () => {
  it('moves focus to the maintenance heading after explicit Advanced activation', async () => {
    const { el, router } = await mountDialog('/settings/advanced')
    const activate = el.querySelector<HTMLButtonElement>('[data-testid="activate-data-maintenance"]')!
    activate.focus()
    activate.click()
    await settle()

    const heading = el.querySelector<HTMLElement>('[data-testid="data-migration-heading"]')
    expect(router.currentRoute.value.path).toBe('/settings/dataMigration')
    expect(heading).toBeTruthy()
    expect(document.activeElement).toBe(heading)
  })

  it('keeps initial modal focus on Close for a cold maintenance deep link', async () => {
    const { el } = await mountDialog('/settings/dataMigration')
    const heading = el.querySelector<HTMLElement>('[data-testid="data-migration-heading"]')
    const close = el.querySelector<HTMLButtonElement>('.settings-modal__head button')

    expect(heading).toBeTruthy()
    expect(document.activeElement).toBe(close)
    expect(document.activeElement).not.toBe(heading)
  })
})
