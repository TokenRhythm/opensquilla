// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { App } from 'vue'

const mounted: App[] = []

async function settle(): Promise<void> {
  for (let i = 0; i < 8; i++) await Promise.resolve()
  await new Promise(resolve => setTimeout(resolve, 10))
}

async function mountDialog(path: '/settings/advanced' | '/settings/dataMigration') {
  vi.resetModules()
  document.body.innerHTML = ''

  const vue = await import('vue')
  const { SETTINGS_SECTIONS } = await import('@/composables/setup/settingsSections')
  const section = vue.ref(path.endsWith('/dataMigration') ? 'dataMigration' : 'advanced')
  const noOp = vi.fn()
  const catalog = new Proxy<Record<string, unknown>>({
    section,
    setSection: (next: string) => { section.value = next },
    loaded: vue.ref(true),
    providerPanel: vue.ref({ runtimeProviders: [] }),
    behaviorPanel: vue.ref({}),
    privacyPanel: vue.ref({}),
    modelStrategyPanel: vue.ref({}),
    presetPanel: vue.ref({}),
    channelsPanel: vue.ref({}),
    capabilitiesPanel: vue.ref({}),
    hasSetupAction: vue.ref(false),
    actionItems: vue.ref([]),
    fixCommands: vue.ref([]),
    handoffCommands: vue.ref([]),
    recipeCommands: vue.ref([]),
    configSummary: vue.ref([]),
    configPath: vue.ref(''),
    dirtySections: vue.ref([]),
    hasUnsavedChanges: vue.ref(false),
    saveAllPending: vue.ref(false),
    sectionStatus: () => ({ label: 'Ready', tone: 'is-ok' }),
    sectionDirty: () => false,
  }, {
    get(target, key) {
      return Reflect.has(target, key) ? Reflect.get(target, key) : noOp
    },
  })

  vi.doMock('@/composables/setup/useSetupCatalog', () => ({
    SETTINGS_SECTIONS,
    useSetupCatalog: () => catalog,
  }))
  vi.doMock('@/components/settings/SettingsAdvancedPanel.vue', () => ({
    default: vue.defineComponent({
      emits: ['open-agent-configuration', 'open-data-maintenance'],
      template: '<button type="button" data-testid="activate-data-maintenance" @click="$emit(\'open-data-maintenance\')">Open data maintenance</button>',
    }),
  }))
  vi.doMock('@/stores/rpc', () => ({
    useRpcStore: () => ({
      waitForConnection: vi.fn(async () => {}),
      supportsMethod: vi.fn(() => false),
      call: vi.fn(),
      isConnected: true,
      isConnecting: false,
    }),
  }))
  vi.doMock('@/platform', () => ({
    usePlatform: () => ({
      capabilities: { isDesktop: false, hasTerminalWorkflow: false },
      gateway: {},
    }),
  }))
  vi.doMock('@/composables/useConfirm', () => ({
    useConfirm: () => ({ confirm: vi.fn(async () => true), confirmState: vue.ref(null) }),
  }))

  const { createMemoryHistory, createRouter } = await import('vue-router')
  const Empty = vue.defineComponent({ template: '<div />' })
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

  const i18n = (await import('@/i18n')).default
  i18n.global.locale.value = 'en'
  const Component = (await import('./SettingsDialog.vue')).default
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = vue.createApp(Component)
  app.use(router)
  app.use(i18n)
  app.mount(el)
  mounted.push(app)
  await settle()
  await vue.nextTick()
  return { el, router }
}

afterEach(() => {
  while (mounted.length) mounted.pop()!.unmount()
  document.body.innerHTML = ''
  vi.doUnmock('@/composables/setup/useSetupCatalog')
  vi.doUnmock('@/components/settings/SettingsAdvancedPanel.vue')
  vi.doUnmock('@/stores/rpc')
  vi.doUnmock('@/platform')
  vi.doUnmock('@/composables/useConfirm')
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
