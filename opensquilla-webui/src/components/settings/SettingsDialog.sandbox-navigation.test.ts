// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, defineComponent, h, nextTick, ref, type App } from 'vue'
import { createPinia } from 'pinia'
import { createMemoryHistory, createRouter, isNavigationFailure, RouterView, type Router } from 'vue-router'
import i18n, { loadLocaleMessages } from '@/i18n'
import { SANDBOX_RUNTIME_KEY, type SandboxRuntime } from '@/modules/sandboxRuntime'
import { useToasts } from '@/composables/useToasts'
import type { SandboxPolicy } from '@/types/sandbox'
import SettingsDialog from './SettingsDialog.vue'
import SandboxSettingsPanel from './SandboxSettingsPanel.vue'

let catalogApi: Record<string, any>

vi.mock('@/composables/setup/useSetupCatalog', () => ({
  SETTINGS_SECTIONS: [
    { id: 'general', label: 'General', icon: 'settings', client: false, group: 'preferences' },
    { id: 'securityPrivacy', label: 'Security & Privacy', icon: 'settings', client: false, group: 'preferences' },
  ],
  useSetupCatalog: () => catalogApi,
}))

// The destination panel is unrelated to Sandbox. The dialog, both exposing
// security panels, their composable, and Vue Router are all real components.
vi.mock('./SettingsGeneralPanel.vue', () => ({
  default: { template: '<section data-testid="general-panel">General</section>' },
}))

vi.mock('@/platform', () => {
  const platform = {
    id: 'web',
    capabilities: { isDesktop: false, hasTerminalWorkflow: false },
    gateway: {}, files: {}, workbench: { native: {} },
    getOsLocale: vi.fn(async () => 'en'),
    setNativeTheme: vi.fn(async () => undefined),
  }
  return { getPlatform: () => platform, usePlatform: () => platform }
})

const initialPolicy: SandboxPolicy = {
  schemaVersion: 2,
  policyVersion: 0,
  files: {
    customDenyWritePaths: ['C:/sandbox/original'],
    recursiveDeleteBackupEnabled: true,
    backupQuotaBytes: 3 * 1024 ** 3,
  },
  commands: { requireApprovalPrefixes: [], autoAllowPrefixes: [], systemTools: 'prompt' },
  network: { blockAllNetwork: false, allowDomains: [], denyDomains: [] },
  runtimes: { enabled: true, python: true, node: true, gitBash: true },
}

interface PendingWrite {
  baseVersion: number
  policy: SandboxPolicy
  resolve: (policy: SandboxPolicy) => void
  reject: (error: Error) => void
}

let app: App<Element> | null = null
let router: Router
let events: string[]
let writes: PendingWrite[]
let updatePolicy: ReturnType<typeof vi.fn<SandboxRuntime['updatePolicy']>>

function mockCatalog() {
  const section = ref('securityPrivacy')
  const state: Record<PropertyKey, any> = {
    section,
    setSection: (value: string) => { section.value = value },
    loaded: ref(true),
    providerPanel: ref({ credentialPanel: null, providerSelected: '' }),
    behaviorPanel: ref({ autoSessionTitles: false, autoSessionTitlesDirty: false, statusText: '' }),
    privacyPanel: ref({ networkReportingEnabled: false, networkReportingForcedOff: false }),
    memoryPanel: ref({ autoCapture: true }),
    modelStrategyPanel: ref({}), capabilitiesPanel: ref({}),
    configPath: ref('/isolated/config.toml'),
    sectionStatus: () => ({ label: 'Ready', tone: 'is-ok' }),
    sectionDirty: () => false,
    dirtySections: ref([]),
    hasUnsavedChanges: ref(false), providerDraftDirty: ref(false),
    saveAllPending: ref(false), providerSavePending: ref(false),
    primaryMutationPending: ref(false), modelStrategyRoutingBusy: ref(false),
  }
  // Catalog callbacks for other settings are not part of these navigations.
  catalogApi = new Proxy(state, {
    get(target, property) {
      if (!(property in target)) target[property] = vi.fn()
      return target[property]
    },
  })
}

async function settle() {
  for (let turn = 0; turn < 20; turn += 1) await Promise.resolve()
  await nextTick()
}

async function mountDialog() {
  mockCatalog()
  updatePolicy = vi.fn((baseVersion: number, policy: SandboxPolicy) => {
    events.push(`save:${writes.length + 1}:started`)
    return new Promise<SandboxPolicy>((resolve, reject) => {
      writes.push({ baseVersion, policy: structuredClone(policy), resolve, reject })
    })
  })
  const unexpectedRuntimeAction = async (): Promise<never> => {
    throw new Error('This navigation must not install or remove a runtime')
  }
  const sandbox: SandboxRuntime = {
    loadSettings: vi.fn<SandboxRuntime['loadSettings']>(async () => ({ policy: structuredClone(initialPolicy), defaults: {}, preference: { runMode: 'full' } })),
    updatePolicy,
    readiness: vi.fn(async () => ({ status: null, capability: null })),
    ensureReady: unexpectedRuntimeAction,
    preference: async () => ({ runMode: 'full' }),
    selectMode: async mode => ({ runMode: mode }),
    onPreferenceChanged: () => () => {},
    runtimeStatus: vi.fn(async () => null),
    installRuntime: unexpectedRuntimeAction,
    cancelRuntime: unexpectedRuntimeAction,
    removeRuntime: unexpectedRuntimeAction,
    discardRuntimeDownload: unexpectedRuntimeAction,
    resumeSession: unexpectedRuntimeAction,
  }
  const outside = defineComponent({ render: () => h('main', { 'data-testid': 'outside' }, 'Outside Settings') })
  router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/chat', component: outside },
      { path: '/outside', component: outside },
      { path: '/settings', component: SettingsDialog, meta: { viewKey: 'settings' } },
      { path: '/settings/:section', component: SettingsDialog, meta: { viewKey: 'settings' } },
    ],
  })
  await router.push('/chat')
  await router.push('/settings/securityPrivacy')
  await router.isReady()
  const root = defineComponent({
    render: () => h(RouterView, null, {
      default: ({ Component, route }: any) => Component ? h(Component, { key: route.meta.viewKey ?? route.path }) : null,
    }),
  })
  const container = document.createElement('div')
  document.body.appendChild(container)
  app = createApp(root)
  app.mixin({
    unmounted() {
      if (this.$.type === SandboxSettingsPanel) events.push('sandbox:unmounted')
    },
  })
  app.use(router).use(createPinia()).use(i18n)
  app.provide(SANDBOX_RUNTIME_KEY, sandbox)
  app.mount(container)
  await settle()
  await vi.advanceTimersByTimeAsync(40)
  expect(document.querySelector('[data-testid="sandbox-overview"]')).not.toBeNull()
  document.querySelector<HTMLButtonElement>('[data-testid="sandbox-open-files"]')!.click()
  await settle()
}

async function editPath(value: string) {
  const input = document.querySelector<HTMLInputElement>('.sandbox-rule--editable input')!
  input.value = value
  input.dispatchEvent(new Event('input', { bubbles: true }))
  await settle()
}

function completeWrite(index: number) {
  const write = writes[index]!
  events.push(`save:${index + 1}:completed`)
  write.resolve({ ...structuredClone(write.policy), policyVersion: write.baseVersion + 1 })
}

function beforeUnloadPrevented(): boolean {
  const event = new Event('beforeunload', { cancelable: true })
  window.dispatchEvent(event)
  return event.defaultPrevented
}

function expectWaitingForSave() {
  expect(router.currentRoute.value.path).toBe('/settings/securityPrivacy')
  expect(document.querySelector('.sandbox-settings')).not.toBeNull()
  expect(events).not.toContain('sandbox:unmounted')
  expect(beforeUnloadPrevented()).toBe(true)
}

function expectSavedBeforeUnmount(saveNumber = 1) {
  expect(events.filter(event => event === 'sandbox:unmounted')).toHaveLength(1)
  expect(events.indexOf('sandbox:unmounted')).toBeGreaterThan(events.indexOf(`save:${saveNumber}:completed`))
  expect(beforeUnloadPrevented()).toBe(false)
}

beforeEach(async () => {
  await loadLocaleMessages('en')
  i18n.global.locale.value = 'en'
  events = []
  writes = []
  vi.useFakeTimers()
  vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })))
  vi.spyOn(HTMLElement.prototype, 'scrollIntoView').mockImplementation(() => {})
  vi.spyOn(HTMLElement.prototype, 'scrollTo').mockImplementation(() => {})
})

afterEach(() => {
  app?.unmount()
  app = null
  const { toasts, dismissToast } = useToasts()
  for (const toast of [...toasts.value]) dismissToast(toast.id)
  document.body.innerHTML = ''
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('SettingsDialog Sandbox navigation through the exposed save contract', () => {
  it.each(['push', 'back', 'section'] as const)('saves a debounced edit before %s navigation unmounts Sandbox', async kind => {
    await mountDialog()
    await editPath('C:/sandbox/edited')
    await vi.advanceTimersByTimeAsync(100)
    expect(updatePolicy).not.toHaveBeenCalled()

    let navigation: ReturnType<Router['push']> | undefined
    if (kind === 'push') navigation = router.push('/outside')
    else if (kind === 'back') router.back()
    else document.querySelector<HTMLButtonElement>('#settings-rail-general')!.click()
    await settle()

    expect(updatePolicy).toHaveBeenCalledTimes(1)
    expect(writes[0]!.policy.files.customDenyWritePaths).toEqual(['C:/sandbox/edited'])
    expectWaitingForSave()
    completeWrite(0)
    await navigation
    await settle()
    await vi.advanceTimersByTimeAsync(100)
    expect(router.currentRoute.value.path).toBe(kind === 'push' ? '/outside' : kind === 'back' ? '/chat' : '/settings/general')
    expectSavedBeforeUnmount()
    await vi.advanceTimersByTimeAsync(500)
    expect(updatePolicy).toHaveBeenCalledTimes(1)
  })

  it.each(['close', 'escape', 'backdrop'] as const)('waits for the policy write before %s closes the dialog', async kind => {
    await mountDialog()
    await editPath('C:/sandbox/close')
    if (kind === 'close') document.querySelector<HTMLButtonElement>('.settings-modal__close')!.click()
    else if (kind === 'escape') document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    else document.querySelector<HTMLElement>('.settings-overlay')!.click()
    await settle()
    expect(updatePolicy).toHaveBeenCalledTimes(1)
    expectWaitingForSave()

    completeWrite(0)
    await settle()
    await vi.advanceTimersByTimeAsync(100)
    await settle()
    expect(router.currentRoute.value.path).toBe('/chat')
    expectSavedBeforeUnmount()
    expect(document.querySelector('.settings-modal')).toBeNull()
  })

  it('keeps the route and edit after a failed save, then permits an explicit retry', async () => {
    await mountDialog()
    await editPath('C:/sandbox/retry')
    const navigation = router.push('/outside')
    await settle()
    expectWaitingForSave()
    writes[0]!.reject(new Error('Gateway write failed'))
    expect(isNavigationFailure(await navigation)).toBe(true)
    await settle()

    expectWaitingForSave()
    expect(document.querySelector<HTMLInputElement>('.sandbox-rule--editable input')!.value).toBe('C:/sandbox/retry')
    expect(document.querySelector<HTMLFieldSetElement>('.settings-panel__interactions')!.disabled).toBe(false)
    await vi.advanceTimersByTimeAsync(600)
    expect(updatePolicy).toHaveBeenCalledTimes(1)
    const retry = router.push('/outside')
    await settle()
    expect(updatePolicy).toHaveBeenCalledTimes(2)
    expect(writes[1]!.baseVersion).toBe(0)
    expect(writes[1]!.policy.files.customDenyWritePaths).toEqual(['C:/sandbox/retry'])
    completeWrite(1)
    await retry
    await settle()
    expect(router.currentRoute.value.path).toBe('/outside')
    expectSavedBeforeUnmount(2)
  })

  it('saves an edit made during autosave once more before repeated exit attempts can unmount', async () => {
    await mountDialog()
    await editPath('C:/sandbox/first')
    await vi.advanceTimersByTimeAsync(500)
    expect(updatePolicy).toHaveBeenCalledTimes(1)
    await editPath('C:/sandbox/latest')
    const firstExit = router.push('/outside')
    await settle()
    const secondExit = router.push('/outside')
    await settle()
    expectWaitingForSave()
    expect(updatePolicy).toHaveBeenCalledTimes(1)

    completeWrite(0)
    await settle()
    expect(updatePolicy).toHaveBeenCalledTimes(2)
    expect(writes[1]!.baseVersion).toBe(1)
    expect(writes[1]!.policy.files.customDenyWritePaths).toEqual(['C:/sandbox/latest'])
    expectWaitingForSave()
    completeWrite(1)
    await Promise.all([firstExit, secondExit])
    await settle()
    expect(router.currentRoute.value.path).toBe('/outside')
    expectSavedBeforeUnmount(2)
    await vi.advanceTimersByTimeAsync(600)
    expect(updatePolicy).toHaveBeenCalledTimes(2)
  })

  it('guards beforeunload only while dirty or saving and removes its listener on unmount', async () => {
    const addListener = vi.spyOn(window, 'addEventListener')
    const removeListener = vi.spyOn(window, 'removeEventListener')
    await mountDialog()
    expect(beforeUnloadPrevented()).toBe(false)
    await editPath('C:/sandbox/saved')
    expect(beforeUnloadPrevented()).toBe(true)
    await vi.advanceTimersByTimeAsync(500)
    expect(beforeUnloadPrevented()).toBe(true)
    completeWrite(0)
    await settle()
    expect(beforeUnloadPrevented()).toBe(false)
    expect(document.querySelector('.sandbox-settings')).not.toBeNull()

    await editPath('C:/sandbox/pending-unload')
    expect(beforeUnloadPrevented()).toBe(true)
    const listeners = addListener.mock.calls.filter(([type]) => type === 'beforeunload')
    expect(listeners).toHaveLength(2)
    app!.unmount()
    app = null
    expect(beforeUnloadPrevented()).toBe(false)
    for (const [, listener] of listeners) expect(removeListener).toHaveBeenCalledWith('beforeunload', listener)
    await vi.advanceTimersByTimeAsync(600)
    expect(updatePolicy).toHaveBeenCalledTimes(1)
  })
})
