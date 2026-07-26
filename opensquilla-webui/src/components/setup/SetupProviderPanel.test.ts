// @vitest-environment happy-dom
import { readFileSync } from 'node:fs'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick, reactive } from 'vue'
import i18n from '@/i18n'
import zhHans from '@/locales/zh-Hans.json'
import SetupProviderPanel from './SetupProviderPanel.vue'
import type { ConnectionState, DiscoveredModel } from '@/composables/setup/useSetupProviderForm'

function connection(overrides: Partial<ConnectionState> = {}): ConnectionState {
  return {
    phase: 'unverified',
    failureKind: '',
    detail: '',
    firstResponseMs: null,
    totalMs: null,
    latencyMs: null,
    models: [],
    modelSource: 'none',
    discoverError: '',
    ...overrides,
  }
}

const DISCOVERED: DiscoveredModel[] = [
  {
    id: 'test-vendor/alpha',
    name: 'Alpha',
    contextWindow: 262144,
    maxOutputTokens: 16384,
    capabilities: ['chat'],
    pricing: null,
    capabilitySource: 'provider',
  },
]

const TOKENRHYTHM_REGISTRATION_URL = 'https://tokenrhythm.studio/register'
const TOKENRHYTHM_PROVIDER = { providerId: 'tokenrhythm', label: 'TokenRhythm' }
const OPENROUTER_PROVIDER = { providerId: 'openrouter', label: 'OpenRouter' }

function panel(overrides: Record<string, unknown> = {}) {
  const base = {
    providerSummary: 'OpenAI',
    providerSelected: 'openai',
    runtimeProviders: [{ providerId: 'openai', label: 'OpenAI' }],
    configuredProviders: [],
    credentialRemovalPending: false,
    editingPrimary: true,
    selectedStoredProfile: false,
    editingNew: false,
    routingEnabled: false,
    routerEnabled: false,
    routerBinding: 'legacy',
    crossProviderRoutingEnabled: false,
    ensembleEnabled: false,
    activationRouterConflict: false,
    configuredProviderProbes: {},
    activation: {
      providerId: '',
      phase: 'idle',
      models: [],
      suggestedModel: '',
      error: '',
    },
    routerSupportTone: 'is-ready',
    routerSupportText: 'SquillaRouter ready',
    canConfigureRouter: false,
    providerNeeds: [],
    providerCoreFields: [
      { name: 'model', label: 'Model' },
    ],
    providerAdvancedFields: [],
    credentialPanel: {
      providerLabel: 'OpenAI',
      providerSelected: true,
      acceptsApiKey: true,
      requiresApiKey: true,
      available: true,
      removable: false,
      removing: false,
      source: 'explicit',
      envKey: 'OPENAI_API_KEY',
      masked: 'sk-••••1234',
      revealAllowed: true,
      revealed: '',
      revealError: '',
      replacing: false,
      apiKeyValue: '',
      apiKeyEnvValue: '',
      probeReady: true,
      probeDisabledReason: '',
      probeButtonLabel: 'Verify current configuration',
      connection: connection(),
      onReveal: vi.fn(),
      onReplace: vi.fn(),
      onCancelReplace: vi.fn(),
    },
    providerAdvancedOpen: false,
    providerEnvMissing: false,
    providerEnvKey: '',
    providerEnvCommand: '',
    llmTimeoutSeconds: 120,
    contextWindowTokens: '',
    contextWindowGlobal: null,
    effectiveMaxTokens: null,
    providerIsLocal: false,
    connection: connection(),
    providerFieldValue: () => '',
    ...overrides,
  }
  const credentialPanel = (base.credentialPanel as Record<string, unknown>) || {}
  return {
    ...base,
    configuredProviders: ((overrides.configuredProviders as Array<Record<string, unknown>> | undefined) ?? [{
      providerId: String(base.providerSelected || 'openai'),
      label: String((base.credentialPanel as Record<string, unknown>)?.providerLabel || base.providerSelected),
      active: true,
      ready: true,
      credentialSource: 'explicit',
      credentialEnv: '',
      endpointSource: 'registry',
      reason: '',
    }]).map(provider => ({
      primaryEligible: provider.active !== true,
      primaryBlockReason: provider.active === true ? 'already_active' : '',
      probeModelAvailable: true,
      ...provider,
    })),
    credentialPanel: {
      ...credentialPanel,
      providerSelected: (overrides.providerSelected as string | undefined) !== undefined
        ? Boolean(overrides.providerSelected)
        : credentialPanel.providerSelected,
      connection: (overrides.connection as ConnectionState | undefined) || (credentialPanel.connection as ConnectionState) || base.connection,
    },
  }
}

async function mountPanel(props: Record<string, unknown> = {}, listeners: Record<string, unknown> = {}) {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const panelState = reactive(panel(props))
  const app = createApp(SetupProviderPanel, { panel: panelState, ...listeners })
  app.use(i18n)
  app.mount(el)
  await nextTick()
  return { app, el, panelState }
}

function testButton(el: HTMLElement): HTMLButtonElement | null {
  return Array.from(el.querySelectorAll<HTMLButtonElement>('.setup-provider-credential button.btn'))
    .find(btn => (btn.textContent || '').includes('Verify current configuration') || (btn.textContent || '').includes('Verifying')) || null
}

beforeEach(() => {
  i18n.global.locale.value = 'en'
  // The context-window keys land in the locale JSONs via the i18n merge step;
  // inject them here so assertions exercise interpolation, not raw key names.
  i18n.global.mergeLocaleMessage('en', {
    setup: {
      provider: {
        contextWindowLabel: 'Context window override (tokens)',
        contextWindowDesc: 'desc',
        contextWindowAuto: 'auto',
        contextWindowUnknown: 'unknown',
        contextWindowNone: 'none',
        contextWindowReadout: 'auto-detected {auto} · override {override} · effective {effective}',
        contextWindowLocalWarning: 'Effective context window is {tokens} tokens.',
      },
    },
  })
  document.body.innerHTML = ''
})

describe('SetupProviderPanel — verify configuration', () => {
  it('emits probeConnection when Verify current configuration is clicked', async () => {
    const onProbeConnection = vi.fn()
    const { app, el } = await mountPanel({}, { onProbeConnection })
    const button = testButton(el)
    expect(button?.disabled).toBe(false)
    button?.click()
    expect(onProbeConnection).toHaveBeenCalledTimes(1)
    app.unmount()
  })

  it('disables the button with no provider selected and while probing', async () => {
    const noProvider = await mountPanel({ providerSelected: '' })
    expect(testButton(noProvider.el)).toBeNull()
    noProvider.app.unmount()

    const probing = await mountPanel({ connection: connection({ phase: 'probing' }) })
    const button = testButton(probing.el)
    expect(button?.disabled).toBe(true)
    expect(button?.textContent).toContain('Verifying configuration')
    expect(probing.el.querySelector('.setup-connection__spinner')).toBeTruthy()
    probing.app.unmount()
  })

  it('does not emit a probe while required provider fields are missing', async () => {
    const onProbeConnection = vi.fn()
    const reason = 'Complete required fields before verifying: Model, Base URL.'
    const { app, el } = await mountPanel({
      credentialPanel: {
        ...(panel().credentialPanel as Record<string, unknown>),
        probeReady: false,
        probeDisabledReason: reason,
      },
    }, { onProbeConnection })

    const button = testButton(el)
    expect(button?.disabled).toBe(true)
    expect(button?.title).toBe(reason)
    button?.click()
    expect(onProbeConnection).not.toHaveBeenCalled()
    expect(el.textContent).toContain(reason)
    app.unmount()
  })

  it('shows Configuration verified for the current editor settings when verified', async () => {
    const { app, el } = await mountPanel({ connection: connection({ phase: 'verified' }) })
    const pill = el.querySelector('.setup-connection__actions .control-pill.control-pill--ok')
    expect(pill?.textContent).toContain('Configuration verified')
    app.unmount()
  })

  it('shows a human sentence for key_invalid and keeps the raw kind in the tooltip only', async () => {
    const { app, el } = await mountPanel({
      connection: connection({ phase: 'key_invalid', failureKind: 'auth_invalid', detail: 'HTTP 401' }),
    })
    const pill = el.querySelector('.control-pill.control-pill--danger')
    expect(pill?.textContent).toContain('✗ Key rejected — The provider rejected this API key.')
    expect(pill?.textContent).not.toContain('auth_invalid')
    expect(pill?.getAttribute('title')).toContain('auth_invalid')
    app.unmount()
  })

  it('shows a couldn\'t-connect pill for unreachable failures', async () => {
    const { app, el } = await mountPanel({
      connection: connection({ phase: 'unreachable', failureKind: 'transport_transient', detail: 'timeout' }),
    })
    const pill = el.querySelector('.control-pill.control-pill--warn')
    expect(pill?.textContent).toContain("✗ Couldn't connect — Couldn't reach the endpoint.")
    app.unmount()
  })

  it('shows a discover hint when verified but model listing failed', async () => {
    const { app, el } = await mountPanel({
      connection: connection({ phase: 'verified', discoverError: 'listing unsupported' }),
    })
    expect(el.querySelector('.setup-connection__hint')?.textContent)
      .toContain('Couldn\'t list models — type a model id.')
    app.unmount()
  })
})

describe('SetupProviderPanel — model field', () => {
  it('keeps the shared model picker as a manual text input when no catalog is available', async () => {
    const onUpdateProviderField = vi.fn()
    const { app, el } = await mountPanel({ configuredProviders: [] }, { onUpdateProviderField })
    const input = el.querySelector<HTMLInputElement>('input[name="setup_provider_model"]')

    expect(el.querySelector('.setup-model-combobox')).toBeTruthy()
    expect(input?.getAttribute('role')).toBeNull()
    expect(el.querySelector('[data-testid="setup-model-options-toggle"]')).toBeNull()

    input!.value = 'my/manual-model'
    input!.dispatchEvent(new Event('input', { bubbles: true }))
    expect(onUpdateProviderField).toHaveBeenCalledWith('model', 'my/manual-model')
    app.unmount()
  })

  it('enables catalog behavior in the same model picker when discovery returned models', async () => {
    const { app, el } = await mountPanel({
      configuredProviders: [],
      connection: connection({ phase: 'verified', models: DISCOVERED, modelSource: 'live' }),
    })
    const combobox = el.querySelector('.setup-model-combobox input[role="combobox"]')
    expect(combobox?.getAttribute('name')).toBe('setup_provider_model')
    expect(el.querySelector('input[name="setup_provider_api_key"]')).toBeNull()
    app.unmount()
  })

  it('does not render api_key or api_key_env as generic provider fields when the credential card is present', async () => {
    const { app, el } = await mountPanel({
      providerCoreFields: [
        { name: 'model', label: 'Model' },
      ],
      providerAdvancedFields: [
        { name: 'base_url', label: 'Base URL' },
      ],
    })

    expect(el.querySelector('[data-name="api_key"]')).toBeNull()
    expect(el.querySelector('[data-name="api_key_env"]')).toBeNull()
    expect(el.textContent).toContain('OpenAI authentication')

    app.unmount()
  })
})

describe('SetupProviderPanel — configured provider management', () => {
  const configured = [
    {
      providerId: 'openai',
      label: 'OpenAI',
      active: true,
      ready: true,
      credentialSource: 'explicit',
      credentialEnv: '',
      endpointSource: 'registry',
      reason: '',
    },
    {
      providerId: 'deepseek',
      label: 'DeepSeek',
      active: false,
      ready: false,
      credentialSource: 'missing_env',
      credentialEnv: 'DEEPSEEK_API_KEY',
      endpointSource: 'registry',
      reason: 'missing_credentials',
    },
  ]

  it('keeps Active and Editing as separate provider-list concepts', async () => {
    const { app, el } = await mountPanel({
      configuredProviders: configured,
      providerSelected: 'deepseek',
      editingPrimary: false,
      selectedStoredProfile: true,
      credentialPanel: {
        ...(panel().credentialPanel as Record<string, unknown>),
        providerLabel: 'DeepSeek',
      },
    })

    const active = el.querySelector<HTMLElement>('[data-provider-id="openai"]')!
    const editing = el.querySelector<HTMLElement>('[data-provider-id="deepseek"]')!
    expect(active.textContent).toContain('Active')
    expect(active.textContent).not.toContain('Editing')
    expect(active.querySelector('.setup-provider-card__select')?.getAttribute('aria-current')).toBeNull()
    expect(editing.textContent).not.toContain('Active')
    expect(editing.textContent).toContain('Editing')
    expect(editing.querySelector('.setup-provider-card__select')?.getAttribute('aria-current')).toBe('true')
    expect(editing.querySelector('.setup-provider-card__name')?.getAttribute('aria-current')).toBeNull()
    expect(editing.getAttribute('aria-current')).toBeNull()

    app.unmount()
  })

  it('selects the editor from the full information block with native click and Enter semantics', async () => {
    const onSelectConfiguredProvider = vi.fn()
    const { app, el } = await mountPanel({ configuredProviders: configured }, {
      onSelectConfiguredProvider,
    })
    const row = el.querySelector<HTMLElement>('[data-provider-id="deepseek"]')!
    const select = row.querySelector<HTMLButtonElement>('.setup-provider-card__select')!

    expect(select.tagName).toBe('BUTTON')
    expect(select.type).toBe('button')
    expect(row.getAttribute('role')).toBeNull()
    expect(select.querySelector('.setup-provider-card__name')?.textContent).toBe('DeepSeek')

    select.click()
    expect(onSelectConfiguredProvider).toHaveBeenCalledWith('deepseek')

    onSelectConfiguredProvider.mockClear()
    select.focus()
    const keydown = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true })
    if (select.dispatchEvent(keydown)) select.click()
    select.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', bubbles: true }))
    expect(document.activeElement).toBe(select)
    expect(onSelectConfiguredProvider).toHaveBeenCalledOnce()
    expect(onSelectConfiguredProvider).toHaveBeenCalledWith('deepseek')

    app.unmount()
  })

  it('separates credential readiness from an untested saved connection', async () => {
    const { app, el } = await mountPanel({
      configuredProviders: [configured[0]],
    })
    const row = el.querySelector<HTMLElement>('[data-provider-id="openai"]')!
    const test = row.querySelector<HTMLButtonElement>('.setup-provider-card__test')

    expect(row.textContent).toContain('Credentials ready')
    expect(row.textContent).toContain('Not verified')
    expect(row.textContent).not.toContain('Configuration verified')
    expect(test?.textContent?.trim()).toBe('Verify saved configuration')
    expect(test?.getAttribute('aria-describedby')).toBe('setup-provider-configured-desc')
    expect(el.querySelector('#setup-provider-configured-desc')?.textContent).toContain(
      'Verification sends one small model request and may incur provider charges.',
    )

    app.unmount()
  })

  it('shows first model response before complete probe duration for a saved provider', async () => {
    const { app, el } = await mountPanel({
      configuredProviders: [configured[0]],
      configuredProviderProbes: {
        openai: connection({
          phase: 'verified',
          firstResponseMs: 123,
          totalMs: 412,
        }),
      },
    })
    const row = el.querySelector<HTMLElement>('[data-provider-id="openai"]')!

    expect(row.textContent).toContain('✓ Configuration verified')
    expect(row.textContent).toContain('First model response · 123 ms')
    expect(row.textContent).toContain('Complete probe · 412 ms')
    expect(row.textContent?.indexOf('First model response'))
      .toBeLessThan(row.textContent?.indexOf('Complete probe') ?? 0)

    app.unmount()
  })

  it('labels an old gateway latency only as complete probe duration', async () => {
    const { app, el } = await mountPanel({
      configuredProviders: [configured[0]],
      configuredProviderProbes: {
        openai: connection({
          phase: 'verified',
          firstResponseMs: null,
          totalMs: 4082,
          latencyMs: 4082,
        }),
      },
    })
    const row = el.querySelector<HTMLElement>('[data-provider-id="openai"]')!

    expect(row.textContent).toContain('Complete probe · 4082 ms')
    expect(row.textContent).not.toContain('First model response')
    expect(row.textContent).not.toContain('Connected · 4082')

    app.unmount()
  })

  it('shows the classified reason when saved configuration verification fails', async () => {
    const { app, el } = await mountPanel({
      configuredProviders: [configured[0]],
      configuredProviderProbes: {
        openai: connection({
          phase: 'unreachable',
          failureKind: 'insufficient_credits',
          detail: 'billing account rejected the request',
        }),
      },
    })
    const row = el.querySelector<HTMLElement>('[data-provider-id="openai"]')!

    expect(row.textContent).toContain("✗ Couldn't connect — This account has no credits left.")
    expect(row.textContent).not.toContain('Connection unavailable')
    expect(row.textContent).not.toContain('insufficient_credits')

    app.unmount()
  })

  it('reports a malformed saved-provider stream without calling it unreachable', async () => {
    const { app, el } = await mountPanel({
      configuredProviders: [configured[0]],
      configuredProviderProbes: {
        openai: connection({
          phase: 'unreachable',
          failureKind: 'malformed_response',
          detail: 'invalid_stream_order',
        }),
      },
    })
    const row = el.querySelector<HTMLElement>('[data-provider-id="openai"]')!

    expect(row.textContent).toContain('Streaming response incompatible')
    expect(row.textContent).not.toContain("Couldn't connect")
    expect(row.textContent).not.toContain('invalid_stream_order')

    app.unmount()
  })

  it('keeps the full catalog hidden until Add provider opens a searchable list', async () => {
    const onAddProvider = vi.fn()
    const { app, el } = await mountPanel({
      configuredProviders: configured,
      runtimeProviders: [
        { providerId: 'openai', label: 'OpenAI' },
        { providerId: 'deepseek', label: 'DeepSeek' },
        { providerId: 'gemini', label: 'Google Gemini' },
      ],
    }, { onAddProvider })

    expect(el.querySelector('select[name="setup_provider"]')).toBeNull()
    expect(el.querySelector('[data-testid="configured-provider-list"]')?.textContent).toContain('OpenAI')
    expect(el.querySelector('[data-testid="configured-provider-list"]')?.textContent).toContain('DeepSeek')
    expect(el.querySelector('[data-testid="provider-catalog-picker"]')).toBeNull()
    const addArea = el.querySelector<HTMLElement>('.setup-provider-add')!
    const editor = el.querySelector<HTMLElement>('[data-testid="provider-editor-scope"]')!
    expect(addArea.compareDocumentPosition(editor) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()

    const add = Array.from(el.querySelectorAll<HTMLButtonElement>('button'))
      .find(button => button.textContent?.trim() === 'Add provider')
    add?.click()
    await nextTick()

    expect(el.querySelector('[data-provider-picker-trigger]')).toBeNull()
    expect(el.querySelector('[role="dialog"]')).toBeNull()
    expect(el.querySelector('[data-testid="provider-catalog-picker"]')).toBeTruthy()
    expect(el.querySelector('input[name="setup_provider_search"]')).toBeTruthy()
    const options = Array.from(el.querySelectorAll<HTMLButtonElement>('.provider-picker__option'))
    expect(options.map(option => option.textContent)).toEqual([expect.stringContaining('Google Gemini')])

    options[0]?.click()
    expect(onAddProvider).toHaveBeenCalledWith('gemini')
    app.unmount()
  })

  it('closes the inline picker with Escape and restores focus to Add provider', async () => {
    const { app, el } = await mountPanel({
      runtimeProviders: [
        { providerId: 'openai', label: 'OpenAI' },
        { providerId: 'deepseek', label: 'DeepSeek' },
      ],
    })
    const add = Array.from(el.querySelectorAll<HTMLButtonElement>('button'))
      .find(button => button.textContent?.trim() === 'Add provider')!
    add.click()
    await nextTick()
    await nextTick()
    const search = el.querySelector<HTMLInputElement>('input[name="setup_provider_search"]')!
    expect(document.activeElement).toBe(search)

    search.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }))
    await nextTick()

    expect(el.querySelector('[data-testid="provider-catalog-picker"]')).toBeNull()
    const restoredAdd = el.querySelector<HTMLButtonElement>('[data-provider-picker-trigger]')!
    expect(restoredAdd.getAttribute('aria-expanded')).toBe('false')
    expect(document.activeElement).toBe(restoredAdd)
    app.unmount()
  })

  it('does not steal focus when an outside pointer closes the inline picker', async () => {
    const { app, el } = await mountPanel({
      runtimeProviders: [
        { providerId: 'openai', label: 'OpenAI' },
        { providerId: 'deepseek', label: 'DeepSeek' },
      ],
    })
    Array.from(el.querySelectorAll<HTMLButtonElement>('button'))
      .find(button => button.textContent?.trim() === 'Add provider')?.click()
    await nextTick()
    await nextTick()
    const outside = document.createElement('button')
    document.body.appendChild(outside)
    outside.focus()
    outside.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }))
    await nextTick()

    expect(el.querySelector('[data-testid="provider-catalog-picker"]')).toBeNull()
    expect(document.activeElement).toBe(outside)
    app.unmount()
  })

  it('opens the selected provider directly below the picker and focuses its API key', async () => {
    const el = document.createElement('div')
    document.body.appendChild(el)
    const panelState = reactive(panel({
      providerSelected: '',
      configuredProviders: [],
      runtimeProviders: [
        { providerId: 'tokenrhythm', label: 'TokenRhythm' },
        { providerId: 'deepseek', label: 'DeepSeek' },
      ],
    }))
    const mutablePanelState = panelState as unknown as Record<string, unknown>
    const app = createApp(SetupProviderPanel, {
      panel: panelState,
      onAddProvider: (providerId: string) => {
        panelState.providerSelected = providerId
        panelState.editingPrimary = true
        mutablePanelState.providerNeeds = ['API key via DEEPSEEK_API_KEY or a one-time paste.']
        mutablePanelState.credentialPanel = {
          providerLabel: 'DeepSeek',
          providerSelected: true,
          acceptsApiKey: true,
          requiresApiKey: true,
          available: false,
          source: 'none',
          envKey: 'DEEPSEEK_API_KEY',
          masked: '',
          revealAllowed: false,
          revealed: '',
          revealError: '',
          replacing: false,
          apiKeyValue: '',
          apiKeyEnvValue: '',
          probeReady: false,
          probeDisabledReason: 'Add an API key before verifying this provider.',
          probeButtonLabel: 'Add key to verify',
          connection: connection(),
          onReveal: vi.fn(),
          onReplace: vi.fn(),
          onCancelReplace: vi.fn(),
        }
      },
    })
    app.use(i18n)
    app.mount(el)
    await nextTick()

    Array.from(el.querySelectorAll<HTMLButtonElement>('button'))
      .find(button => button.textContent?.trim() === 'Add provider')?.click()
    await nextTick()
    await nextTick()
    Array.from(el.querySelectorAll<HTMLButtonElement>('.provider-picker__option'))
      .find(option => option.textContent?.includes('DeepSeek'))?.click()
    await nextTick()
    await nextTick()

    expect(el.querySelector('[data-testid="provider-catalog-picker"]')).toBeNull()
    const addArea = el.querySelector<HTMLElement>('.setup-provider-add')!
    const editor = el.querySelector<HTMLElement>('[data-testid="provider-editor-scope"]')!
    expect(addArea.compareDocumentPosition(editor) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(document.activeElement).toBe(el.querySelector('input[name="setup_provider_api_key"]'))
    app.unmount()
  })

  it('tests saved config without switching the editor and keeps activate/delete visible', async () => {
    const onSelectConfiguredProvider = vi.fn()
    const onProbeConfiguredProvider = vi.fn()
    const onRemoveProviderProfile = vi.fn()
    const readyConfigured = configured.map(row => row.providerId === 'deepseek' ? { ...row, ready: true } : row)
    const { app, el } = await mountPanel({ configuredProviders: readyConfigured }, {
      onSelectConfiguredProvider,
      onProbeConfiguredProvider,
      onRemoveProviderProfile,
    })
    const deepseek = el.querySelector<HTMLElement>('[data-provider-id="deepseek"]')!
    const buttons = Array.from(deepseek.querySelectorAll<HTMLButtonElement>('button'))

    buttons.find(button => button.textContent?.trim() === 'Verify saved configuration')?.click()
    expect(onSelectConfiguredProvider).not.toHaveBeenCalled()
    expect(onProbeConfiguredProvider).toHaveBeenCalledWith('deepseek')

    expect(onSelectConfiguredProvider).not.toHaveBeenCalled()
    expect(buttons.find(button => button.textContent?.trim() === 'Set active')).toBeTruthy()
    buttons.find(button => button.textContent?.trim() === 'Delete')?.click()
    await nextTick()
    expect(onRemoveProviderProfile).toHaveBeenCalledWith('deepseek')
    expect(onSelectConfiguredProvider).not.toHaveBeenCalled()
    expect(Array.from(el.querySelectorAll<HTMLButtonElement>('[data-provider-id="openai"] button'))
      .some(button => button.textContent?.trim() === 'Delete')).toBe(false)
    app.unmount()
  })

  it('summarizes the active model usage without presenting inactive features as settings', async () => {
    const onGoToSection = vi.fn()
    const { app, el } = await mountPanel({
      configuredProviders: configured,
      providerSelected: 'deepseek',
      editingPrimary: false,
      selectedStoredProfile: true,
      routingEnabled: false,
    }, { onGoToSection })

    const summary = el.querySelector<HTMLElement>('[data-testid="provider-model-usage"]')!
    expect(summary.textContent).toContain('Model usage')
    expect(summary.textContent).toContain('Fixed model')
    expect(summary.textContent).not.toContain('Cross-provider')
    expect(summary.textContent).not.toContain('Ensemble · Off')
    const cta = Array.from(el.querySelectorAll<HTMLButtonElement>('button'))
      .find(button => button.textContent?.includes('Set up model routing'))
    cta?.click()
    expect(onGoToSection).toHaveBeenCalledWith('modelStrategy')
    app.unmount()
  })

  it('hides the model-usage summary while the configured primary model card is shown', async () => {
    const { app, el } = await mountPanel({ configuredProviders: configured })

    expect(el.querySelector('[data-testid="configured-primary-model-readonly"]')).toBeTruthy()
    expect(el.querySelector('[data-testid="provider-model-usage"]')).toBeNull()
    expect(Array.from(el.querySelectorAll<HTMLButtonElement>('button'))
      .filter(button => button.textContent?.includes('Set up model routing'))).toHaveLength(1)

    app.unmount()
  })

  it('keeps the model-usage summary during a primary draft, where no model card is shown', async () => {
    const { app, el } = await mountPanel({
      configuredProviders: configured,
      providerSelected: 'deepseek',
      editingPrimary: true,
    })

    expect(el.querySelector('[data-testid="configured-primary-model-readonly"]')).toBeNull()
    expect(el.querySelector('[data-testid="provider-model-usage"]')).toBeTruthy()

    app.unmount()
  })

  it('renders the persisted verification state when no in-session probe ran', async () => {
    const at = new Date(Date.now() - 5 * 60_000).toISOString()
    const rows = [
      { ...configured[0], lastProbe: { ok: true, at, configChanged: false, failureKind: '' } },
      {
        ...configured[1],
        ready: true,
        lastProbe: { ok: false, at, configChanged: false, failureKind: 'auth_invalid' },
      },
    ]
    const { app, el } = await mountPanel({ configuredProviders: rows })

    const openai = el.querySelector<HTMLElement>('[data-provider-id="openai"]')!
    const deepseek = el.querySelector<HTMLElement>('[data-provider-id="deepseek"]')!
    expect(openai.textContent).toContain('Verified ·')
    expect(openai.textContent).toContain('minutes ago')
    expect(openai.querySelector('.setup-provider-card__probe')?.classList.contains('is-ready'))
      .toBe(true)
    expect(deepseek.textContent).toContain('Last verification failed ·')
    expect(deepseek.querySelector('.setup-provider-card__probe')?.classList.contains('is-warn'))
      .toBe(true)

    app.unmount()
  })

  it('prompts for re-verification when the saved config changed since the last probe', async () => {
    const rows = [{
      ...configured[0],
      lastProbe: { ok: true, at: new Date().toISOString(), configChanged: true, failureKind: '' },
    }]
    const { app, el } = await mountPanel({ configuredProviders: rows })

    const probe = el.querySelector<HTMLElement>(
      '[data-provider-id="openai"] .setup-provider-card__probe',
    )!
    expect(probe.textContent).toContain('Configuration changed since verification')
    expect(probe.classList.contains('is-warn')).toBe(false)
    expect(probe.classList.contains('is-ready')).toBe(false)

    app.unmount()
  })

  it('keeps the plain not-verified fallback for rows with no probe history', async () => {
    const { app, el } = await mountPanel({ configuredProviders: configured })

    const probe = el.querySelector<HTMLElement>(
      '[data-provider-id="openai"] .setup-provider-card__probe',
    )!
    expect(probe.textContent).toContain('Not verified')
    expect(probe.classList.contains('is-warn')).toBe(true)

    app.unmount()
  })

  it('shows a concise readiness summary for one saved deployment', async () => {
    const { app, el } = await mountPanel({
      configuredProviders: [configured[0]],
    })

    expect(el.textContent).toContain('1 of 1 credentials ready')
    expect(el.textContent).not.toContain('Multi-provider features')

    app.unmount()
  })

  it('keeps the zero-provider state focused on the first connection', async () => {
    const { app, el } = await mountPanel({
      configuredProviders: [],
      providerSelected: '',
    })

    const empty = el.querySelector<HTMLElement>('[data-testid="provider-empty-state"]')!
    expect(empty.textContent).toContain('Add your first model provider')
    expect(empty.textContent).toContain('one provider can still route between multiple models')
    expect(el.querySelector('[data-testid="provider-model-usage"]')).toBeNull()
    expect(el.textContent).not.toContain('Multi-provider features')
    expect(Array.from(el.querySelectorAll<HTMLButtonElement>('button'))
      .filter(button => button.textContent?.includes('Add provider'))).toHaveLength(1)

    app.unmount()
  })

  it('shows every row through four and collapses five or more to the first three', async () => {
    const five = ['openai', 'deepseek', 'gemini', 'openrouter', 'tokenrhythm'].map((providerId, index) => ({
      providerId,
      label: providerId,
      active: index === 0,
      ready: true,
      credentialSource: 'explicit',
      credentialEnv: '',
      endpointSource: 'registry',
      reason: '',
    }))
    const { app, el } = await mountPanel({ configuredProviders: five })

    expect(el.querySelectorAll('[data-provider-id]')).toHaveLength(3)
    const viewAll = Array.from(el.querySelectorAll<HTMLButtonElement>('button'))
      .find(button => button.textContent?.includes('View all (5)'))
    viewAll?.click()
    await nextTick()
    expect(el.querySelectorAll('[data-provider-id]')).toHaveLength(5)

    app.unmount()
  })

  it('keeps unknown old-gateway status honest and disables its saved-config test', async () => {
    const unavailable = [{
      ...configured[1],
      reason: 'profile_status_unavailable',
      ready: false,
      probeModelAvailable: false,
    }]
    const { app, el } = await mountPanel({ configuredProviders: unavailable, providerSelected: '' })
    const row = el.querySelector<HTMLElement>('[data-provider-id="deepseek"]')!
    const test = row.querySelector<HTMLButtonElement>('.setup-provider-card__test')

    expect(row.textContent).toContain('Status unavailable')
    expect(test?.disabled).toBe(true)
    expect(test?.getAttribute('aria-label')).toBe('Status unavailable — DeepSeek')
    expect(test?.title).toContain('status is unavailable')

    app.unmount()
  })

  it('names the disabled no-key action from its visible label and provider', async () => {
    const missing = [{
      ...configured[1],
      ready: false,
      reason: 'missing_credentials',
      probeModelAvailable: true,
    }]
    const { app, el } = await mountPanel({ configuredProviders: missing, providerSelected: '' })
    const test = el.querySelector<HTMLButtonElement>('[data-provider-id="deepseek"] .setup-provider-card__test')

    expect(test?.disabled).toBe(true)
    expect(test?.textContent?.trim()).toBe('Add key to verify')
    expect(test?.getAttribute('aria-label')).toBe('Add key to verify — DeepSeek')

    app.unmount()
  })

  it('exposes activation and delete as native buttons without an overflow menu', async () => {
    const ready = configured.map(row => row.providerId === 'deepseek'
      ? { ...row, ready: true, primaryEligible: true, probeModelAvailable: true }
      : row)
    const { app, el } = await mountPanel({ configuredProviders: ready })
    const row = el.querySelector<HTMLElement>('[data-provider-id="deepseek"]')!
    const labels = Array.from(row.querySelectorAll<HTMLButtonElement>('button'))
      .map(button => button.textContent?.trim())

    expect(labels).toContain('Set active')
    expect(labels).toContain('Delete')
    expect(row.querySelector('[aria-haspopup="menu"]')).toBeNull()
    expect(row.querySelector('.setup-provider-card__identity')?.getAttribute('aria-label'))
      .toBe('Edit DeepSeek — Credentials ready — Not verified')
    expect(row.querySelector('.setup-provider-card__activate')?.getAttribute('aria-label'))
      .toBe('Set active — DeepSeek')
    expect(row.querySelector('.setup-provider-card__delete')?.getAttribute('aria-label'))
      .toBe('Delete — DeepSeek')

    app.unmount()
  })

  it('disables the complete provider interaction surface while activation is in progress', async () => {
    const ready = configured.map(row => row.providerId === 'deepseek'
      ? { ...row, ready: true, primaryEligible: true, probeModelAvailable: true }
      : row)
    const { app, el } = await mountPanel({
      configuredProviders: ready,
      activation: {
        providerId: 'deepseek',
        phase: 'activating',
        models: [],
        suggestedModel: '',
        error: '',
      },
    })

    const interactions = el.querySelector<HTMLFieldSetElement>('.setup-provider-interactions')
    expect(interactions?.disabled).toBe(true)
    expect(interactions?.getAttribute('aria-busy')).toBe('true')
    expect(el.querySelector('[data-provider-id="deepseek"] .setup-provider-card__activate')
      ?.getAttribute('aria-label')).toBe('Activating… — DeepSeek')

    app.unmount()
  })

  it('disables provider interactions while removing a credential and restores credential focus', async () => {
    const { app, el, panelState } = await mountPanel({
      credentialRemovalPending: true,
      credentialPanel: {
        ...(panel().credentialPanel as Record<string, unknown>),
        removable: true,
        removing: true,
      },
    })
    const interactions = el.querySelector<HTMLFieldSetElement>('.setup-provider-interactions')

    expect(interactions?.disabled).toBe(true)
    expect(interactions?.getAttribute('aria-busy')).toBe('true')
    expect(el.querySelector('.setup-provider-credential__remove')?.textContent)
      .toContain('Removing saved key')

    const mutablePanel = panelState as unknown as {
      credentialRemovalPending: boolean
      credentialPanel: Record<string, unknown>
    }
    mutablePanel.credentialPanel.removing = false
    mutablePanel.credentialPanel.masked = ''
    mutablePanel.credentialPanel.available = false
    mutablePanel.credentialPanel.source = 'none'
    mutablePanel.credentialPanel.removable = false
    mutablePanel.credentialRemovalPending = false
    await nextTick()
    await nextTick()

    expect(interactions?.disabled).toBe(false)
    expect(document.activeElement)
      .toBe(el.querySelector<HTMLInputElement>('input[name="setup_provider_api_key"]'))

    app.unmount()
  })

  it('activates a provider directly without opening a dialog', async () => {
    const ready = configured.map(row => row.providerId === 'deepseek'
      ? { ...row, ready: true, primaryEligible: true, probeModelAvailable: true }
      : row)
    const onActivateProvider = vi.fn()
    const { app, el } = await mountPanel({
      configuredProviders: ready,
    }, { onActivateProvider })
    const row = el.querySelector<HTMLElement>('[data-provider-id="deepseek"]')!
    const trigger = row.querySelector<HTMLButtonElement>('.setup-provider-card__activate')!
    trigger.click()
    await nextTick()

    expect(onActivateProvider).toHaveBeenCalledWith('deepseek')
    expect(el.querySelector('[role="dialog"]')).toBeNull()
    app.unmount()
  })

  it('never offers activation or deletion on the active provider row', async () => {
    const { app, el } = await mountPanel({ configuredProviders: configured })
    const row = el.querySelector<HTMLElement>('[data-provider-id="openai"]')!
    const labels = Array.from(row.querySelectorAll<HTMLButtonElement>('button'))
      .map(button => button.textContent?.trim())

    expect(labels).not.toContain('Set active')
    expect(labels).not.toContain('Delete')
    app.unmount()
  })

  it('opens the complete scrollable catalog, prioritizes featured providers, and excludes configured providers', async () => {
    const onAddProvider = vi.fn()
    const { app, el } = await mountPanel({
      configuredProviders: [configured[0]],
      runtimeProviders: [
        { providerId: 'openai', label: 'OpenAI' },
        { providerId: 'tokenrhythm', label: 'TokenRhythm' },
        { providerId: 'deepseek', label: 'DeepSeek' },
        { providerId: 'gemini', label: 'Gemini' },
        { providerId: 'custom', label: 'Custom endpoint' },
        { providerId: 'anthropic', label: 'Anthropic' },
      ],
    }, { onAddProvider })
    Array.from(el.querySelectorAll<HTMLButtonElement>('button'))
      .find(button => button.textContent?.trim() === 'Add provider')?.click()
    await nextTick()

    const initial = Array.from(el.querySelectorAll<HTMLElement>('.provider-picker__option'))
      .map(option => option.textContent)
    expect(initial).toEqual([
      expect.stringContaining('TokenRhythm'),
      expect.stringContaining('DeepSeek'),
      expect.stringContaining('Gemini'),
      expect.stringContaining('Anthropic'),
      expect.stringContaining('Custom endpoint'),
    ])
    expect(initial.join(' ')).not.toContain('OpenAI')
    expect(el.querySelector('.provider-picker__list')).toBeTruthy()
    expect(Array.from(el.querySelectorAll<HTMLButtonElement>('button'))
      .some(button => button.textContent?.includes('Browse all providers'))).toBe(false)
    expect(el.querySelector('.provider-picker__option .control-pill')?.textContent).toContain('Recommended')

    const search = el.querySelector<HTMLInputElement>('input[name="setup_provider_search"]')!
    search.value = 'custom'
    search.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()
    const result = el.querySelector<HTMLButtonElement>('.provider-picker__option')!
    expect(result.textContent).toContain('Custom endpoint')
    result.click()
    expect(onAddProvider).toHaveBeenCalledWith('custom')

    app.unmount()
  })

  it('keeps listbox options out of the Tab order and selects the active option with Enter', async () => {
    const onAddProvider = vi.fn()
    const { app, el } = await mountPanel({
      configuredProviders: [configured[0]],
      runtimeProviders: [
        { providerId: 'openai', label: 'OpenAI' },
        { providerId: 'tokenrhythm', label: 'TokenRhythm' },
        { providerId: 'deepseek', label: 'DeepSeek' },
        { providerId: 'gemini', label: 'Gemini' },
      ],
    }, { onAddProvider })
    Array.from(el.querySelectorAll<HTMLButtonElement>('button'))
      .find(button => button.textContent?.trim() === 'Add provider')?.click()
    await nextTick()
    await nextTick()
    await nextTick()

    const options = Array.from(el.querySelectorAll<HTMLButtonElement>('.provider-picker__option'))
    expect(options).toHaveLength(3)
    expect(options.every(option => option.tabIndex === -1)).toBe(true)
    const search = el.querySelector<HTMLInputElement>('input[name="setup_provider_search"]')!
    // Wait for the catalog's scheduled focus before driving ArrowDown. Under a
    // loaded full-suite worker, dispatching first lets the delayed focus handler
    // reset activeIndex back to zero after the key event.
    await vi.waitFor(() => expect(document.activeElement).toBe(search))
    search.focus()
    search.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true, cancelable: true }))
    await vi.waitFor(() => {
      expect(search.getAttribute('aria-activedescendant')).toBe('setup-provider-catalog-option-1')
    })
    search.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }))
    expect(onAddProvider).toHaveBeenCalledWith('deepseek')

    app.unmount()
  })

  it('keeps activation direct even when the Router reports a provider conflict', async () => {
    const onActivateProvider = vi.fn()
    const ready = configured.map(row => row.providerId === 'deepseek'
      ? { ...row, ready: true, primaryEligible: true, probeModelAvailable: true }
      : row)
    const { app, el } = await mountPanel({
      configuredProviders: ready,
      routerEnabled: true,
      routerBinding: 'custom',
      activationRouterConflict: true,
    }, { onActivateProvider })
    const row = el.querySelector<HTMLElement>('[data-provider-id="deepseek"]')!
    row.querySelector<HTMLButtonElement>('.setup-provider-card__activate')?.click()
    await nextTick()

    expect(onActivateProvider).toHaveBeenCalledWith('deepseek')
    expect(el.textContent).not.toContain('Resolve Model Routing')
    expect(el.querySelector('[role="dialog"]')).toBeNull()
    app.unmount()
  })

  it('lets backend eligibility decide activation without frontend model metadata', async () => {
    const onActivateProvider = vi.fn()
    const ready = configured.map(row => row.providerId === 'deepseek'
      ? {
          ...row,
          ready: true,
          primaryEligible: true,
          probeModelAvailable: true,
        }
      : row)
    const { app, el } = await mountPanel({ configuredProviders: ready }, { onActivateProvider })

    const row = el.querySelector<HTMLElement>('[data-provider-id="deepseek"]')!
    const activate = row.querySelector<HTMLButtonElement>('.setup-provider-card__activate')!

    expect(activate.disabled).toBe(false)
    activate.click()
    expect(onActivateProvider).toHaveBeenCalledWith('deepseek')
    expect(row.textContent).not.toContain('Choose and save a model below')
    expect(el.querySelector('[role="dialog"]')).toBeNull()
    app.unmount()
  })

  it('keeps a provider without a saved or default model in the editor flow', async () => {
    const onActivateProvider = vi.fn()
    const rows = configured.map(row => row.providerId === 'deepseek'
      ? {
          ...row,
          ready: true,
          primaryEligible: false,
          primaryBlockReason: 'missing_model',
          probeModelAvailable: true,
        }
      : row)
    const { app, el } = await mountPanel({ configuredProviders: rows }, { onActivateProvider })

    const row = el.querySelector<HTMLElement>('[data-provider-id="deepseek"]')!
    const activate = row.querySelector<HTMLButtonElement>('.setup-provider-card__activate')!
    expect(activate.disabled).toBe(true)
    expect(activate.title).toContain('Choose a direct/fallback model')
    expect(row.textContent).not.toContain('Choose and save a model below')
    expect(el.querySelector('[role="dialog"]')).toBeNull()
    activate.click()
    expect(onActivateProvider).not.toHaveBeenCalled()
    app.unmount()
  })
})

describe('SetupProviderPanel — editor scope', () => {
  it('keeps the editor heading in normal document flow', () => {
    const source = readFileSync('src/components/setup/SetupProviderPanel.vue', 'utf8')
    const editorHeadingRule = source.match(/\.setup-provider-editor-head \{([\s\S]*?)\n\}/)?.[1] || ''

    expect(editorHeadingRule).not.toContain('position: sticky')
    expect(editorHeadingRule).not.toMatch(/^\s*top\s*:/m)
    expect(editorHeadingRule).not.toMatch(/^\s*z-index\s*:/m)
  })

  const fields = {
    providerCoreFields: [{ name: 'model', label: 'Model' }],
    providerAdvancedFields: [
      { name: 'base_url', label: 'Base URL' },
      { name: 'proxy', label: 'HTTP proxy' },
    ],
    providerAdvancedOpen: true,
    providerFieldValue: (field: { name: string }) => ({
      model: 'deepseek-chat',
      base_url: 'https://api.deepseek.com',
      proxy: '',
    })[field.name] || '',
  }

  it('labels a saved profile direct model without showing gateway-wide timeout', async () => {
    const { app, el } = await mountPanel({
      ...fields,
      providerSelected: 'deepseek',
      providerSummary: 'DeepSeek',
      editingPrimary: false,
      selectedStoredProfile: true,
      editingNew: false,
      credentialPanel: {
        ...(panel().credentialPanel as Record<string, unknown>),
        providerLabel: 'DeepSeek',
        probeButtonLabel: 'Verify current configuration',
      },
    })

    const scope = el.querySelector('[data-testid="provider-editor-scope"]')
    expect(scope?.textContent).toContain('Edit DeepSeek')
    expect(scope?.textContent).toContain('Routing profile')
    expect(scope?.textContent).toContain('does not make it active')
    expect(el.textContent).toContain('DeepSeek endpoint & advanced options')
    expect(el.textContent).toContain('DeepSeek endpoint')
    expect(el.textContent).toContain('Direct and fallback model for DeepSeek')
    expect(el.textContent).toContain('Direct / fallback model')
    expect(el.textContent).toContain('Saved with this provider')
    expect(el.querySelector('input[name="setup_provider_model"]')?.closest('details')).toBeNull()
    expect(el.querySelector('input[name="setup_provider_request_timeout"]')).toBeNull()

    app.unmount()
  })

  it('labels a newly added provider as unsaved and explains the save effect', async () => {
    const { app, el } = await mountPanel({
      ...fields,
      providerSelected: 'deepseek',
      editingPrimary: false,
      selectedStoredProfile: false,
      editingNew: true,
      configuredProviders: [],
      credentialPanel: {
        ...(panel().credentialPanel as Record<string, unknown>),
        providerLabel: 'DeepSeek',
      },
    })

    const scope = el.querySelector('[data-testid="provider-editor-scope"]')
    expect(scope?.textContent).toContain('Not saved')
    expect(scope?.textContent).toContain('Saving makes this provider available to Routing and Ensemble')
    expect(scope?.textContent).toContain('will not become active')
    expect(el.textContent).toContain('Direct / fallback model')

    app.unmount()
  })

  it('does not call the first provider active before its initial save', async () => {
    const { app, el } = await mountPanel({
      ...fields,
      providerSelected: 'deepseek',
      providerSummary: 'DeepSeek',
      editingPrimary: true,
      selectedStoredProfile: false,
      editingNew: false,
      configuredProviders: [],
      credentialPanel: {
        ...(panel().credentialPanel as Record<string, unknown>),
        providerLabel: 'DeepSeek',
      },
    })

    const scope = el.querySelector('[data-testid="provider-editor-scope"]')
    expect(scope?.textContent).toContain('New active provider')
    expect(scope?.textContent).toContain('Saving will make this provider active')
    expect(scope?.textContent).not.toContain('Used for direct requests')
    expect(el.querySelector('input[name="setup_provider_model"]')).toBeTruthy()

    app.unmount()
  })

  it('keeps the configured active model owned by Model Routing and timeout gateway-wide', async () => {
    const onGoToSection = vi.fn()
    const { app, el } = await mountPanel({
      ...fields,
      providerSelected: 'deepseek',
      providerSummary: 'DeepSeek',
      editingPrimary: true,
      selectedStoredProfile: false,
      editingNew: false,
      credentialPanel: {
        ...(panel().credentialPanel as Record<string, unknown>),
        providerLabel: 'DeepSeek',
      },
    }, { onGoToSection })

    const scope = el.querySelector('[data-testid="provider-editor-scope"]')
    expect(scope?.textContent).toContain('Active provider')
    expect(scope?.textContent).toContain('primary fallback')
    expect(el.textContent).toContain('Model for DeepSeek')
    expect(el.textContent).toContain('Fixed model mode: every request uses the model below.')
    expect(el.textContent).not.toContain('A recommended model is prefilled when available')
    expect(el.textContent).not.toContain('Saved with this provider')
    expect(el.querySelector('input[name="setup_provider_model"]')).toBeNull()
    const modelOwner = el.querySelector<HTMLElement>('[data-testid="configured-primary-model-readonly"]')
    expect(modelOwner?.textContent).toContain('Current model')
    expect(modelOwner?.textContent).toContain('Fixed model')
    expect(modelOwner?.textContent).toContain('deepseek-chat')
    expect(el.querySelector('[data-testid="provider-model-usage"]')).toBeNull()
    const routingLink = modelOwner?.querySelector<HTMLButtonElement>('button')
    expect(routingLink?.textContent).toContain('Set up model routing')
    routingLink?.click()
    expect(onGoToSection).toHaveBeenCalledWith('modelStrategy')
    expect(el.textContent).toContain('Context window · DeepSeek / deepseek-chat')
    const timeout = el.querySelector<HTMLInputElement>('input[name="setup_provider_request_timeout"]')
    expect(timeout).toBeTruthy()
    expect(timeout?.closest('details')?.textContent).toContain('Runtime defaults · All providers')
    expect(timeout?.closest('details')?.textContent).toContain('shared by every normal model request')

    app.unmount()
  })
})

describe('SetupProviderPanel — TokenRhythm recommendation', () => {
  function recommendation(el: HTMLElement): HTMLElement | null {
    return el.querySelector<HTMLElement>('[data-testid="tokenrhythm-recommendation"]')
  }

  function tokenRhythmCredential(overrides: Record<string, unknown> = {}) {
    return {
      ...(panel().credentialPanel as Record<string, unknown>),
      providerLabel: 'TokenRhythm',
      available: false,
      source: 'none',
      envKey: 'TOKENRHYTHM_API_KEY',
      masked: '',
      revealAllowed: false,
      replacing: false,
      apiKeyValue: '',
      apiKeyEnvValue: 'TOKENRHYTHM_API_KEY',
      ...overrides,
    }
  }

  it('keeps OpenRouter selected while showing exactly one TokenRhythm recommendation', async () => {
    const onUpdateProviderSelected = vi.fn()
    const onProviderChange = vi.fn()
    const onUpdateProviderField = vi.fn()
    const { app, el } = await mountPanel(
      {
        providerSelected: 'openrouter',
        runtimeProviders: [OPENROUTER_PROVIDER, TOKENRHYTHM_PROVIDER],
        credentialPanel: {
          ...(panel().credentialPanel as Record<string, unknown>),
          providerLabel: 'OpenRouter',
        },
      },
      { onUpdateProviderSelected, onProviderChange, onUpdateProviderField },
    )

    const select = el.querySelector<HTMLSelectElement>('select[name="setup_provider"]')
    const recommendations = el.querySelectorAll('[data-testid="tokenrhythm-recommendation"]')
    const link = el.querySelector<HTMLAnchorElement>(`a[href="${TOKENRHYTHM_REGISTRATION_URL}"]`)

    expect(select).toBeNull()
    expect(el.querySelector('[data-provider-id="openrouter"]')).toBeTruthy()
    expect(recommendations).toHaveLength(1)
    const recommendationCard = recommendation(el)!
    const addArea = el.querySelector<HTMLElement>('.setup-provider-add')!
    const editor = el.querySelector<HTMLElement>('[data-testid="provider-editor-scope"]')!
    expect(addArea.compareDocumentPosition(recommendationCard) & Node.DOCUMENT_POSITION_FOLLOWING)
      .toBeTruthy()
    expect(recommendationCard.compareDocumentPosition(editor) & Node.DOCUMENT_POSITION_FOLLOWING)
      .toBeTruthy()
    expect(recommendationCard.querySelector('.setup-provider-recommendation__scope')?.textContent)
      .toContain('OpenSquilla recommendation · Optional')
    expect(el.querySelector('.setup-provider-global-block__eyebrow')).toBeNull()
    expect(recommendation(el)?.textContent).toContain('Recommended: TokenRhythm')
    expect(recommendation(el)?.textContent)
      .toContain('TokenRhythm API calls are free for a limited time.')
    expect(recommendation(el)?.textContent)
      .toContain('During the promotion, register and get an API key to call DeepSeek, GLM, MiniMax, Kimi, and other leading models for free.')
    expect(
      Array.from(recommendation(el)?.querySelectorAll('[data-testid="tokenrhythm-recommendation-step"]') || [])
        .map(step => step.textContent?.replace(/\s+/g, ' ').trim()),
    ).toEqual([
      '1 Create a TokenRhythm account',
      '2 Copy your API key',
      '3 Choose Add provider, select TokenRhythm, then paste your API key',
    ])

    link?.addEventListener('click', event => event.preventDefault(), { once: true })
    link?.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }))
    await nextTick()

    expect(select).toBeNull()
    expect(onUpdateProviderSelected).not.toHaveBeenCalled()
    expect(onProviderChange).not.toHaveBeenCalled()
    expect(onUpdateProviderField).not.toHaveBeenCalled()
    app.unmount()
  })

  it('uses the exact safe external URL without graphical assets or alert semantics', async () => {
    const { app, el } = await mountPanel({
      providerSelected: 'openrouter',
      runtimeProviders: [OPENROUTER_PROVIDER, TOKENRHYTHM_PROVIDER],
    })
    const card = recommendation(el)
    const link = card?.querySelector<HTMLAnchorElement>('a')

    expect(link?.href).toBe(TOKENRHYTHM_REGISTRATION_URL)
    expect(link?.getAttribute('target')).toBe('_blank')
    expect(link?.getAttribute('rel')).toBe('noopener noreferrer')
    expect(link?.getAttribute('aria-label')).toContain('opens in a new tab')
    expect(link?.textContent).toContain('Register and get an API key')
    expect(card?.querySelector('img, svg, canvas')).toBeNull()
    expect(card?.querySelector('[role="alert"]')).toBeNull()
    app.unmount()
  })

  it('guides users through registering, copying, and pasting the key before the primary action', async () => {
    const { app, el } = await mountPanel({
      providerSelected: 'tokenrhythm',
      runtimeProviders: [OPENROUTER_PROVIDER, TOKENRHYTHM_PROVIDER],
      credentialPanel: tokenRhythmCredential(),
    })
    const card = recommendation(el)
    const steps = Array.from(
      card?.querySelectorAll<HTMLElement>('[data-testid="tokenrhythm-recommendation-step"]') || [],
    )
    const link = card?.querySelector<HTMLAnchorElement>('a')

    expect(card?.querySelector('ol')?.getAttribute('aria-label')).toBe('How to connect TokenRhythm')
    expect(steps.map(step => step.textContent?.replace(/\s+/g, ' ').trim())).toEqual([
      '1 Create a TokenRhythm account',
      '2 Copy your API key',
      '3 Paste it into the API key field below',
    ])
    expect(link?.classList.contains('btn')).toBe(true)
    expect(link?.classList.contains('btn--primary')).toBe(true)
    const thirdStep = steps[2]
    if (!link || !thirdStep) throw new Error('TokenRhythm guidance controls are missing')
    expect(link.compareDocumentPosition(thirdStep) & Node.DOCUMENT_POSITION_PRECEDING).toBeTruthy()
    app.unmount()
  })

  it('shows the recommendation when TokenRhythm is selected without an available or draft key', async () => {
    const { app, el } = await mountPanel({
      providerSelected: 'tokenrhythm',
      runtimeProviders: [OPENROUTER_PROVIDER, TOKENRHYTHM_PROVIDER],
      credentialPanel: tokenRhythmCredential(),
    })

    expect(recommendation(el)).toBeTruthy()
    expect(el.querySelectorAll('[data-testid="tokenrhythm-recommendation"]')).toHaveLength(1)
    app.unmount()
  })

  it('keeps the registration entry actionable for a saved TokenRhythm credential', async () => {
    const { app, el } = await mountPanel({
      providerSelected: 'tokenrhythm',
      runtimeProviders: [OPENROUTER_PROVIDER, TOKENRHYTHM_PROVIDER],
      credentialPanel: tokenRhythmCredential({
        available: true,
        source: 'explicit',
        masked: 'tr-•••1234',
        replacing: false,
      }),
    })

    const card = recommendation(el)
    expect(card).toBeTruthy()
    expect(card?.querySelector<HTMLAnchorElement>('a')?.href).toBe(TOKENRHYTHM_REGISTRATION_URL)
    expect(
      Array.from(card?.querySelectorAll('[data-testid="tokenrhythm-recommendation-step"]') || [])
        .map(step => step.textContent?.replace(/\s+/g, ' ').trim()),
    ).toEqual([
      '1 Create a TokenRhythm account',
      '2 Copy your API key',
      '3 Choose Replace key below, then paste your API key',
    ])
    expect(el.querySelector<HTMLInputElement>('input[name="setup_provider_api_key_display"]')?.readOnly)
      .toBe(true)
    expect(Array.from(el.querySelectorAll('button')).some(button => button.textContent?.trim() === 'Replace key'))
      .toBe(true)
    app.unmount()
  })

  it('keeps the registration entry visible and direct-paste guidance for a draft TokenRhythm key', async () => {
    const { app, el } = await mountPanel({
      providerSelected: 'tokenrhythm',
      runtimeProviders: [OPENROUTER_PROVIDER, TOKENRHYTHM_PROVIDER],
      credentialPanel: tokenRhythmCredential({ apiKeyValue: 'tr-test-key' }),
    })

    const card = recommendation(el)
    expect(card).toBeTruthy()
    expect(card?.querySelector<HTMLAnchorElement>('a')?.href).toBe(TOKENRHYTHM_REGISTRATION_URL)
    expect(
      Array.from(card?.querySelectorAll('[data-testid="tokenrhythm-recommendation-step"]') || [])
        .map(step => step.textContent?.replace(/\s+/g, ' ').trim()),
    ).toEqual([
      '1 Create a TokenRhythm account',
      '2 Copy your API key',
      '3 Paste it into the API key field below',
    ])
    app.unmount()
  })

  it('uses direct-paste guidance while replacing a saved TokenRhythm credential', async () => {
    const { app, el } = await mountPanel({
      providerSelected: 'tokenrhythm',
      runtimeProviders: [OPENROUTER_PROVIDER, TOKENRHYTHM_PROVIDER],
      credentialPanel: tokenRhythmCredential({
        available: true,
        source: 'explicit',
        masked: 'tr-•••1234',
        replacing: true,
      }),
    })

    const card = recommendation(el)
    expect(
      Array.from(card?.querySelectorAll('[data-testid="tokenrhythm-recommendation-step"]') || [])
        .map(step => step.textContent?.replace(/\s+/g, ' ').trim()),
    ).toEqual([
      '1 Create a TokenRhythm account',
      '2 Copy your API key',
      '3 Paste it into the API key field below',
    ])
    expect(el.querySelector<HTMLInputElement>('input[name="setup_provider_api_key"]')?.readOnly)
      .toBe(false)
    expect(Array.from(el.querySelectorAll('button')).some(button => button.textContent?.trim() === 'Cancel'))
      .toBe(true)
    app.unmount()
  })

  it('hides the recommendation when TokenRhythm is absent from the runtime catalog', async () => {
    const { app, el } = await mountPanel({
      providerSelected: 'openrouter',
      runtimeProviders: [OPENROUTER_PROVIDER],
    })

    expect(recommendation(el)).toBeNull()
    app.unmount()
  })

  it('shows the full recommendation even before an editor provider is selected', async () => {
    const { app, el } = await mountPanel({
      providerSelected: '',
      runtimeProviders: [TOKENRHYTHM_PROVIDER],
      credentialPanel: null,
    })

    expect(recommendation(el)).toBeTruthy()
    expect(el.querySelector('.setup-provider-editor-head')).toBeNull()
    expect(recommendation(el)?.querySelectorAll('[data-testid="tokenrhythm-recommendation-step"]'))
      .toHaveLength(3)

    app.unmount()
  })

  it('renders the approved zh-Hans copy exactly', async () => {
    i18n.global.setLocaleMessage('zh-Hans', zhHans)
    i18n.global.locale.value = 'zh-Hans'
    const { app, el } = await mountPanel({
      providerSelected: 'openrouter',
      runtimeProviders: [OPENROUTER_PROVIDER, TOKENRHYTHM_PROVIDER],
    })
    const card = recommendation(el)

    expect(card?.querySelector('[data-testid="tokenrhythm-recommendation-title"]')?.textContent)
      .toBe('推荐使用 TokenRhythm')
    expect(card?.querySelector('[data-testid="tokenrhythm-recommendation-value"]')?.textContent)
      .toBe('TokenRhythm API 调用限时免费。')
    expect(card?.querySelector('[data-testid="tokenrhythm-recommendation-registration"]')?.textContent)
      .toBe('活动期间，注册并获取 API Key，即可免费调用 DeepSeek、GLM、MiniMax、Kimi 等主流模型。')
    expect(
      Array.from(card?.querySelectorAll('[data-testid="tokenrhythm-recommendation-step"]') || [])
        .map(step => step.textContent?.replace(/\s+/g, ' ').trim()),
    ).toEqual([
      '1 注册 TokenRhythm 账户',
      '2 复制你的 API Key',
      '3 点击“添加服务商”，选择 TokenRhythm，然后粘贴 API Key',
    ])
    expect(card?.querySelector('a')?.textContent?.trim()).toBe('注册并获取 API Key')
    expect(card?.querySelector('a')?.getAttribute('aria-label')).toContain('在新标签页中打开')
    app.unmount()
  })
})

describe('SetupProviderPanel — effective output limit', () => {
  it('shows the exact effective limit and its catalog source', async () => {
    const { app, el } = await mountPanel({
      effectiveMaxTokens: { value: 131072, source: 'catalog' },
    })

    const readout = el.querySelector('[data-testid="setup-effective-max-tokens"]')
    expect(readout?.textContent).toContain('131,072 tokens')
    expect(readout?.textContent).toContain('model catalog')
    expect(readout?.getAttribute('aria-live')).toBe('polite')

    app.unmount()
  })

  it('hides the readout when no identity-matched effective value is supplied', async () => {
    const { app, el } = await mountPanel({ effectiveMaxTokens: null })

    expect(el.querySelector('[data-testid="setup-effective-max-tokens"]')).toBeNull()

    app.unmount()
  })
})

describe('SetupProviderPanel — context-window override', () => {
  function contextInput(el: HTMLElement): HTMLInputElement | null {
    return el.querySelector<HTMLInputElement>('input[name="setup_provider_context_window"]')
  }

  function readout(el: HTMLElement): string {
    return el.querySelector('.setup-context-window__readout')?.textContent || ''
  }

  const modelValue = (value: string) =>
    (field: { name: string }) => (field.name === 'model' ? value : '')

  it('shows the auto-detected window for the current model with no override', async () => {
    const { app, el } = await mountPanel({
      connection: connection({ phase: 'verified', models: DISCOVERED, modelSource: 'live' }),
      providerFieldValue: modelValue('test-vendor/alpha'),
    })

    const input = contextInput(el)
    expect(input).toBeTruthy()
    expect(input?.disabled).toBe(false)
    expect(input?.placeholder).toBe('auto')
    expect(el.querySelector('.setup-context-window__readout')?.getAttribute('aria-live')).toBe('polite')
    expect(readout(el)).toContain('auto-detected 262144')
    expect(readout(el)).toContain('override none')
    expect(readout(el)).toContain('effective 262144')
    expect(el.querySelector('.setup-warning')).toBeNull()

    app.unmount()
  })

  it('reports unknown when the model has no discovery row', async () => {
    const { app, el } = await mountPanel({
      providerFieldValue: modelValue('unlisted-model'),
    })

    expect(readout(el)).toContain('auto-detected unknown')
    expect(readout(el)).toContain('effective unknown')

    app.unmount()
  })

  it('an override beats auto-detection and warns for small local windows', async () => {
    const { app, el } = await mountPanel({
      connection: connection({ phase: 'verified', models: DISCOVERED, modelSource: 'live' }),
      providerFieldValue: modelValue('test-vendor/alpha'),
      contextWindowTokens: '4096',
      providerIsLocal: true,
    })

    expect(readout(el)).toContain('override 4096')
    expect(readout(el)).toContain('effective 4096')
    expect(el.querySelector('.setup-warning')?.textContent).toContain('4096 tokens')

    app.unmount()
  })

  it('does not warn for the same small window on a hosted provider', async () => {
    const { app, el } = await mountPanel({
      providerFieldValue: modelValue('test-vendor/alpha'),
      contextWindowTokens: '4096',
      providerIsLocal: false,
    })

    expect(el.querySelector('.setup-warning')).toBeNull()

    app.unmount()
  })

  it('falls back to the global llm.context_window_tokens layer when no override is set', async () => {
    const { app, el } = await mountPanel({
      connection: connection({ phase: 'verified', models: DISCOVERED, modelSource: 'live' }),
      providerFieldValue: modelValue('test-vendor/alpha'),
      contextWindowTokens: '',
      contextWindowGlobal: 100000,
    })

    // No per-model override → effective takes the global config layer, not auto.
    expect(readout(el)).toContain('override none')
    expect(readout(el)).toContain('auto-detected 262144')
    expect(readout(el)).toContain('effective 100000')

    app.unmount()
  })

  it('a per-model override beats the global config layer', async () => {
    const { app, el } = await mountPanel({
      connection: connection({ phase: 'verified', models: DISCOVERED, modelSource: 'live' }),
      providerFieldValue: modelValue('test-vendor/alpha'),
      contextWindowTokens: '4096',
      contextWindowGlobal: 100000,
    })

    expect(readout(el)).toContain('override 4096')
    expect(readout(el)).toContain('effective 4096')

    app.unmount()
  })

  it('warns for a small global window on a local provider with no override', async () => {
    const { app, el } = await mountPanel({
      providerFieldValue: modelValue('test-vendor/alpha'),
      contextWindowTokens: '',
      contextWindowGlobal: 8192,
      providerIsLocal: true,
    })

    expect(el.querySelector('.setup-warning')?.textContent).toContain('8192 tokens')

    app.unmount()
  })

  it('disables the input while the model field is empty', async () => {
    const { app, el } = await mountPanel()

    expect(contextInput(el)?.disabled).toBe(true)

    app.unmount()
  })

  it('emits updateContextWindow with the raw input string', async () => {
    const onUpdateContextWindow = vi.fn()
    const { app, el } = await mountPanel(
      { providerFieldValue: modelValue('test-vendor/alpha') },
      { onUpdateContextWindow },
    )

    const input = contextInput(el)!
    input.value = '16384'
    input.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()

    expect(onUpdateContextWindow).toHaveBeenCalledWith('16384')

    app.unmount()
  })
})

describe('SetupProviderPanel — model strategy wayfinding', () => {
  it('shows the active smart-routing mode on the model card with a single routing entry', async () => {
    const onGoToSection = vi.fn()
    const preset = {
      hasPreset: true,
      presetLabel: 'OpenAI balanced tiers',
      presetDescription: 'A curated tier split.',
      synthesized: false,
      tierRows: [],
      tierLabel: (tier: string) => tier,
      routerMode: 'custom',
      routerCustomized: true,
    }
    const { app, el } = await mountPanel({
      canConfigureRouter: true,
      routerEnabled: true,
      crossProviderRoutingEnabled: true,
    }, { preset, onGoToSection })
    const routingLinks = Array.from(el.querySelectorAll<HTMLButtonElement>('button'))
      .filter(btn => /Set up model routing/.test(btn.textContent || ''))

    expect(routingLinks).toHaveLength(1)
    const modelOwner = el.querySelector<HTMLElement>('[data-testid="configured-primary-model-readonly"]')
    expect(modelOwner?.textContent).toContain('Fallback model')
    expect(modelOwner?.textContent).toContain('Intelligent model routing')
    expect(modelOwner?.textContent).toContain('Cross-provider routing included')
    expect(el.querySelector('[data-testid="provider-model-usage"]')).toBeNull()
    expect(el.textContent).toContain('Intelligent model routing mode:')
    expect(el.textContent).not.toContain('SquillaRouter ready')
    expect(el.textContent).not.toContain('Multi-provider features')
    expect(el.textContent).not.toContain('Routing template:')
    expect(el.textContent).not.toContain('Model Routing already uses')

    routingLinks.forEach(link => link.click())

    expect(onGoToSection).toHaveBeenCalledTimes(1)
    expect(onGoToSection).toHaveBeenCalledWith('modelStrategy')
    app.unmount()
  })
})
