// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { createApp, nextTick, reactive } from 'vue'
import i18n from '@/i18n'
import SetupProviderCredentialCard from './SetupProviderCredentialCard.vue'

function panel(overrides: Record<string, unknown> = {}) {
  return {
    providerLabel: 'DeepSeek',
    providerSelected: true,
    acceptsApiKey: true,
    requiresApiKey: true,
    available: true,
    removable: true,
    removing: false,
    source: 'env',
    envKey: 'DEEPSEEK_API_KEY',
    masked: 'sk-••••7890',
    revealAllowed: true,
    revealed: '',
    revealError: '',
    replacing: false,
    apiKeyValue: '',
    apiKeyEnvValue: 'DEEPSEEK_API_KEY',
    probeReady: true,
    probeDisabledReason: '',
    probeButtonLabel: 'Verify current configuration',
    connection: { phase: 'unverified' },
    ...overrides,
  }
}

async function mountCard(props: Record<string, unknown> = {}, listeners: Record<string, unknown> = {}) {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(SetupProviderCredentialCard, { panel: panel(props), ...listeners })
  app.use(i18n)
  app.mount(el)
  await nextTick()
  return { app, el }
}

// The real card stays mounted across saves and provider switches, so these
// tests need a panel whose fields mutate in place after mount.
async function mountReactiveCard(
  props: Record<string, unknown> = {},
  listeners: Record<string, unknown> = {},
) {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const livePanel = reactive(panel(props))
  const app = createApp(SetupProviderCredentialCard, { panel: livePanel, ...listeners })
  app.use(i18n)
  app.mount(el)
  await nextTick()
  return { app, el, livePanel }
}

beforeEach(() => {
  i18n.global.locale.value = 'en'
  // The verdict keys land in the locale JSONs via the i18n merge step; inject
  // them here so assertions exercise interpolation instead of raw key names.
  i18n.global.mergeLocaleMessage('en', {
    setup: {
      provider: {
        removeCredential: 'Remove saved key',
        removingCredential: 'Removing saved key…',
        verdictModels: '{count} models · e.g. {samples}',
        verdictSampleJoiner: ', ',
      },
    },
  })
  i18n.global.mergeLocaleMessage('zh-Hans', {
    setup: { provider: { verdictModels: '{count} 个模型 · 例如 {samples}', verdictSampleJoiner: '、' } },
  })
  document.body.innerHTML = ''
})

function discoveredModel(id: string) {
  return {
    id,
    name: id,
    contextWindow: 128000,
    maxOutputTokens: 8192,
    capabilities: ['chat'],
    pricing: null,
    capabilitySource: 'provider',
  }
}

function verifiedConnection(overrides: Record<string, unknown> = {}) {
  return {
    phase: 'verified',
    failureKind: '',
    detail: '',
    firstResponseMs: 123,
    totalMs: 412,
    latencyMs: null,
    models: [
      discoveredModel('test-vendor/alpha'),
      discoveredModel('test-vendor/beta'),
      discoveredModel('test-vendor/gamma'),
      discoveredModel('test-vendor/delta'),
    ],
    modelSource: 'live',
    discoverError: '',
    ...overrides,
  }
}

describe('SetupProviderCredentialCard', () => {
  it('keeps credential controls in tablet layout until phone widths', () => {
    const source = readFileSync('src/components/setup/SetupProviderCredentialCard.vue', 'utf8')

    expect(source).toContain('@media (max-width: 520px)')
    expect(source).not.toContain('@media (max-width: 720px)')
    expect(source).toContain('flex-wrap: wrap;')
    expect(source).toContain('width: auto;')
  })

  it('separates credential readiness from the unverified connection state', async () => {
    const { app, el } = await mountCard()

    expect(el.textContent).toContain('DeepSeek authentication')
    expect(el.textContent).toContain('Key available')
    expect(el.textContent).toContain('Current configuration not verified')
    expect(el.textContent).not.toContain('Configuration verified')
    expect(el.textContent).toContain('Current source: environment variable DEEPSEEK_API_KEY')
    expect(el.querySelector('input[name="setup_provider_api_key_env"]')).toBeNull()

    app.unmount()
  })

  it.each([
    { name: 'first key', available: false, source: 'none', masked: '', replacing: false },
    { name: 'replacement key', available: true, source: 'explicit', masked: 'sk-••••7890', replacing: true },
  ])('marks an unsaved $name as entered without treating it as saved', async state => {
    const { app, el } = await mountCard({
      ...state,
      apiKeyValue: 'synthetic-unsaved-key',
    })

    expect(el.textContent).toContain('Key entered · not saved')
    expect(el.textContent).toContain('Current source: unsaved API key')
    expect(el.textContent).not.toContain('Needs key')

    app.unmount()
  })

  it('labels an explicitly entered environment reference as unsaved credential input', async () => {
    const { app, el } = await mountCard({
      available: false,
      source: 'none',
      masked: '',
      apiKeyValue: '',
      apiKeyEnvValue: 'DEEPSEEK_DRAFT_KEY',
      draftCredentialSource: 'env',
    })

    expect(el.textContent).toContain('Environment variable entered · not saved')
    expect(el.textContent).toContain(
      'Current source: unsaved environment variable DEEPSEEK_DRAFT_KEY',
    )
    expect(el.textContent).not.toContain('Needs key')

    app.unmount()
  })

  it('shows the reveal button only when reveal is allowed and a masked credential exists', async () => {
    const visible = await mountCard()
    expect(visible.el.querySelector('.setup-provider-credential__input-action[aria-label="Show API key"]')).toBeTruthy()
    expect(Array.from(visible.el.querySelectorAll('button')).some(btn => (btn.textContent || '').includes('Show API key'))).toBe(false)
    visible.app.unmount()

    const hidden = await mountCard({ revealAllowed: false })
    expect(hidden.el.querySelector('.setup-provider-credential__input-action[aria-label="Show API key"]')).toBeNull()
    hidden.app.unmount()
  })

  it('shows the public-session hint when a masked credential exists but reveal is not allowed', async () => {
    const { app, el } = await mountCard({ revealAllowed: false })

    expect(el.textContent).toContain('Current session can replace this credential but cannot view its secret.')

    app.unmount()
  })

  it('explains that a write-only saved profile key is kept until replacement', async () => {
    const { app, el } = await mountCard({
      available: true,
      source: 'explicit',
      masked: '',
      revealAllowed: false,
    })

    const input = el.querySelector<HTMLInputElement>('input[name="setup_provider_api_key"]')
    expect(input?.placeholder).toBe('Saved key kept — paste to replace')
    expect(el.textContent).toContain('Current session can replace this credential but cannot view its secret.')

    app.unmount()
  })

  it('does not show the reveal button when no masked credential exists', async () => {
    const { app, el } = await mountCard({ masked: '', revealAllowed: true })

    expect(el.querySelector('.setup-provider-credential__input-action[aria-label="View"]')).toBeNull()

    app.unmount()
  })

  it('keeps reveal and replace controls attached to the API key input', async () => {
    const { app, el } = await mountCard()

    const fieldRow = el.querySelector('.setup-provider-credential__field-row')
    const inputShell = fieldRow?.querySelector('.setup-provider-credential__input-shell')
    expect(inputShell?.querySelector('input[name="setup_provider_api_key_display"]')).toBeTruthy()
    expect(inputShell?.querySelector('.setup-provider-credential__input-action[aria-label="Show API key"]')).toBeTruthy()
    expect(fieldRow?.querySelector('.setup-provider-credential__replace')?.textContent).toContain('Replace key')
    expect(el.querySelector('.setup-provider-credential__actions')).toBeNull()

    app.unmount()
  })

  it.each(['explicit', 'env', 'missing_env'])(
    'offers a low-emphasis removal action for a saved %s credential reference',
    async source => {
      const onRemoveCredential = vi.fn()
      const { app, el } = await mountCard({ source }, { onRemoveCredential })

      const remove = el.querySelector<HTMLButtonElement>('.setup-provider-credential__remove')
      expect(remove).toBeTruthy()
      expect(remove?.textContent?.trim()).toBe('Remove saved key')
      expect(remove?.classList.contains('btn--ghost')).toBe(true)
      expect(remove?.getAttribute('aria-label')).toBe('Remove saved key — DeepSeek')

      remove?.click()
      expect(onRemoveCredential).toHaveBeenCalledTimes(1)

      app.unmount()
    },
  )

  it('keeps removal available for a write-only saved credential', async () => {
    const onRemoveCredential = vi.fn()
    const { app, el } = await mountCard(
      { source: 'explicit', masked: '', revealAllowed: false, available: true },
      { onRemoveCredential },
    )

    expect(el.querySelector('input[name="setup_provider_api_key"]')).toBeTruthy()
    const remove = el.querySelector<HTMLButtonElement>('.setup-provider-credential__remove')
    expect(remove).toBeTruthy()

    remove?.click()
    expect(onRemoveCredential).toHaveBeenCalledTimes(1)

    app.unmount()
  })

  it('disables credential removal and exposes progress while it is pending', async () => {
    const onRemoveCredential = vi.fn()
    const { app, el } = await mountCard(
      { removing: true },
      { onRemoveCredential },
    )

    const card = el.querySelector<HTMLElement>('.setup-provider-credential')
    const remove = el.querySelector<HTMLButtonElement>('.setup-provider-credential__remove')
    expect(card?.getAttribute('aria-busy')).toBe('true')
    expect(remove?.disabled).toBe(true)
    expect(remove?.getAttribute('aria-busy')).toBe('true')
    expect(remove?.textContent).toContain('Removing saved key…')
    expect(remove?.querySelector('.setup-connection__spinner')).toBeTruthy()

    remove?.click()
    expect(onRemoveCredential).not.toHaveBeenCalled()

    app.unmount()
  })

  it.each(['none', 'not_required'])(
    'does not offer credential removal when the current source is %s',
    async source => {
      const { app, el } = await mountCard({
        source,
        masked: '',
        available: false,
        removable: false,
      })

      expect(el.querySelector('.setup-provider-credential__remove')).toBeNull()

      app.unmount()
    },
  )

  it('does not compete with replacement controls while a replacement is in progress', async () => {
    const { app, el } = await mountCard({ source: 'explicit', replacing: true })

    expect(el.querySelector('.setup-provider-credential__remove')).toBeNull()
    expect(Array.from(el.querySelectorAll('button')).some(btn => btn.textContent?.trim() === 'Cancel')).toBe(true)

    app.unmount()
  })

  it('changes the saved-key reveal button into an immediate hide control', async () => {
    const onReveal = vi.fn()
    const onHideReveal = vi.fn()
    const { app, el, livePanel } = await mountReactiveCard({}, { onReveal, onHideReveal })

    const show = el.querySelector<HTMLButtonElement>('[aria-label="Show API key"]')
    show?.click()
    expect(onReveal).toHaveBeenCalledTimes(1)

    livePanel.revealed = 'synthetic-visible-key'
    await nextTick()

    expect(el.querySelector<HTMLInputElement>('input[name="setup_provider_api_key_display"]')?.value)
      .toBe('synthetic-visible-key')
    const hide = el.querySelector<HTMLButtonElement>('[aria-label="Hide API key"]')
    expect(hide).toBeTruthy()
    hide?.click()
    expect(onHideReveal).toHaveBeenCalledTimes(1)
    expect(onReveal).toHaveBeenCalledTimes(1)

    livePanel.revealed = ''
    await nextTick()
    expect(el.querySelector<HTMLInputElement>('input[name="setup_provider_api_key_display"]')?.value)
      .toBe('sk-••••7890')
    expect(el.querySelector('[aria-label="Show API key"]')).toBeTruthy()

    app.unmount()
  })

  it('emits updateField while replacing and toggles the local password visibility control', async () => {
    const onUpdateField = vi.fn()
    const { app, el } = await mountCard({ replacing: true, apiKeyValue: 'sk-new' }, { onUpdateField })

    const input = el.querySelector<HTMLInputElement>('input[name="setup_provider_api_key"]')
    expect(input?.type).toBe('password')

    input!.value = 'sk-next'
    input!.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()

    expect(onUpdateField).toHaveBeenCalledWith('api_key', 'sk-next')

    const toggle = Array.from(el.querySelectorAll<HTMLButtonElement>('button'))
      .find(btn => btn.getAttribute('aria-label') === 'Show API key')
    toggle?.click()
    await nextTick()

    expect(el.querySelector<HTMLInputElement>('input[name="setup_provider_api_key"]')?.type).toBe('text')

    app.unmount()
  })

  it('renders a directly editable input when no key was ever saved', async () => {
    const onUpdateField = vi.fn()
    const { app, el } = await mountCard(
      { masked: '', source: 'none', available: false },
      { onUpdateField },
    )

    // First-run setup: no saved secret to guard, so no readonly display, no
    // "Replace key" detour, and nothing to cancel back to.
    expect(el.querySelector('input[name="setup_provider_api_key_display"]')).toBeNull()
    expect(Array.from(el.querySelectorAll('button')).some(btn => (btn.textContent || '').includes('Replace key'))).toBe(false)
    expect(Array.from(el.querySelectorAll('button')).some(btn => (btn.textContent || '').includes('Cancel'))).toBe(false)

    const input = el.querySelector<HTMLInputElement>('input[name="setup_provider_api_key"]')
    expect(input).toBeTruthy()
    expect(input?.placeholder).toBe('Paste your API key')

    input!.value = 'sk-first'
    input!.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()
    expect(onUpdateField).toHaveBeenCalledWith('api_key', 'sk-first')

    app.unmount()
  })

  it('labels the disabled action as Add key to verify when credentials are missing', async () => {
    const { app, el } = await mountCard({
      masked: '',
      source: 'none',
      available: false,
      probeReady: false,
      probeDisabledReason: 'Add an API key before verifying this provider.',
      probeButtonLabel: 'Add key to verify',
    })

    const button = Array.from(el.querySelectorAll<HTMLButtonElement>('button'))
      .find(candidate => candidate.textContent?.trim() === 'Add key to verify')
    expect(button).toBeTruthy()
    expect(button?.disabled).toBe(true)
    expect(button?.title).toBe('Add an API key before verifying this provider.')
    expect(button?.getAttribute('aria-describedby')).toBe('setup-provider-probe-hint-deepseek')
    expect(el.querySelector('#setup-provider-probe-hint-deepseek')?.textContent)
      .toBe('Add an API key before verifying this provider.')
    expect(el.textContent).toContain('Add an API key before verifying this provider.')

    app.unmount()
  })

  it('keeps the editable input directly available when a declared env var is not visible', async () => {
    const { app, el } = await mountCard({ masked: '', source: 'missing_env', available: false })

    expect(el.querySelector('input[name="setup_provider_api_key"]')).toBeTruthy()
    expect(el.querySelector('input[name="setup_provider_api_key_display"]')).toBeNull()

    app.unmount()
  })

  it('keeps an optional key directly editable when the provider accepts one', async () => {
    const { app, el } = await mountCard({
      masked: '',
      source: 'not_required',
      requiresApiKey: false,
    })

    expect(el.textContent).toContain('API key optional')
    expect(el.textContent).toContain('Current source: no API key configured (optional)')
    expect(el.textContent).toContain('API key (optional)')
    expect(el.textContent).toContain(
      'Enter a key only if this endpoint requires authentication; otherwise leave it blank.',
    )
    expect(el.textContent).not.toContain('Key available')
    expect(el.querySelector('input[name="setup_provider_api_key"]')).toBeTruthy()
    expect(el.querySelector('input[name="setup_provider_api_key_display"]')).toBeNull()

    app.unmount()
  })

  it('does not render an API key control for an OAuth provider', async () => {
    const { app, el } = await mountCard({
      providerLabel: 'OpenAI Codex',
      acceptsApiKey: false,
      requiresApiKey: false,
      masked: '',
      source: 'not_required',
    })

    expect(el.textContent).toContain('No key required')
    expect(el.querySelector('input[name="setup_provider_api_key"]')).toBeNull()
    expect(el.querySelector('details.setup-provider-credential__details')).toBeNull()

    app.unmount()
  })

  it('disables probing with a localized missing-field reason', async () => {
    const reason = '请先填写必填项：模型、基础 URL。'
    const { app, el } = await mountCard({ probeReady: false, probeDisabledReason: reason })

    const button = Array.from(el.querySelectorAll<HTMLButtonElement>('button'))
      .find(candidate => candidate.textContent?.includes('Verify current configuration'))
    expect(button?.disabled).toBe(true)
    expect(button?.title).toBe(reason)
    expect(el.textContent).toContain(reason)

    app.unmount()
  })

  it('keeps the masked display and Replace key guard while a saved key exists', async () => {
    const { app, el } = await mountCard({ masked: 'sk-••••7890', source: 'explicit' })

    expect(el.querySelector('input[name="setup_provider_api_key_display"]')).toBeTruthy()
    expect(el.querySelector('input[name="setup_provider_api_key"]')).toBeNull()
    expect(Array.from(el.querySelectorAll('button')).some(btn => (btn.textContent || '').includes('Replace key'))).toBe(true)

    app.unmount()
  })

  it('re-hides the secret input when first-run editing ends in a saved key', async () => {
    const { app, el, livePanel } = await mountReactiveCard({ masked: '', source: 'none', available: false })

    // First-run: user toggles the key to plaintext while typing it.
    const toggle = () => Array.from(el.querySelectorAll<HTMLButtonElement>('button'))
      .find(btn => /^(Show|Hide) API key$/.test(btn.getAttribute('aria-label') || ''))
    toggle()!.click()
    await nextTick()
    expect(el.querySelector<HTMLInputElement>('input[name="setup_provider_api_key"]')?.type).toBe('text')

    // Save lands: the masked display takes over without replacing ever flipping.
    livePanel.masked = 'sk-••••1234'
    livePanel.source = 'explicit'
    await nextTick()
    expect(el.querySelector('input[name="setup_provider_api_key"]')).toBeNull()

    // A later Replace must start hidden again, not inherit the old toggle.
    livePanel.replacing = true
    await nextTick()
    expect(el.querySelector<HTMLInputElement>('input[name="setup_provider_api_key"]')?.type).toBe('password')

    app.unmount()
  })

  it('re-hides the secret input when the card switches to another provider mid-edit', async () => {
    const { app, el, livePanel } = await mountReactiveCard({ masked: '', source: 'none', available: false })

    const toggle = Array.from(el.querySelectorAll<HTMLButtonElement>('button'))
      .find(btn => btn.getAttribute('aria-label') === 'Show API key')
    toggle!.click()
    await nextTick()
    expect(el.querySelector<HTMLInputElement>('input[name="setup_provider_api_key"]')?.type).toBe('text')

    livePanel.providerLabel = 'OpenRouter'
    livePanel.envKey = 'OPENROUTER_API_KEY'
    await nextTick()

    expect(el.querySelector<HTMLInputElement>('input[name="setup_provider_api_key"]')?.type).toBe('password')

    app.unmount()
  })

  it('shows the replacement placeholder and Cancel when replacing a saved key', async () => {
    const { app, el } = await mountCard({ replacing: true })

    const input = el.querySelector<HTMLInputElement>('input[name="setup_provider_api_key"]')
    expect(input?.placeholder).toBe('Paste a replacement API key')
    expect(Array.from(el.querySelectorAll('button')).some(btn => (btn.textContent || '').includes('Cancel'))).toBe(true)

    app.unmount()
  })

  it('renders the advanced env input on demand and emits api_key_env updates', async () => {
    const onUpdateField = vi.fn()
    const { app, el } = await mountCard({}, { onUpdateField })

    const summary = Array.from(el.querySelectorAll('summary'))
      .find(node => (node.textContent || '').includes('Credential source'))
    ;(summary as HTMLElement | undefined)?.click()
    await nextTick()

    const envInput = el.querySelector<HTMLInputElement>('input[name="setup_provider_api_key_env"]')
    expect(envInput).toBeTruthy()

    envInput!.value = 'ALT_DEEPSEEK_KEY'
    envInput!.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()

    expect(onUpdateField).toHaveBeenCalledWith('api_key_env', 'ALT_DEEPSEEK_KEY')

    app.unmount()
  })
})

describe('SetupProviderCredentialCard — configuration verification verdict', () => {
  it('announces the current-settings verdict and exposes probing as busy', async () => {
    const verified = await mountCard({ connection: verifiedConnection() })
    const status = verified.el.querySelector('[role="status"]')
    expect(status?.getAttribute('aria-live')).toBe('polite')
    expect(status?.getAttribute('aria-atomic')).toBe('true')
    expect(status?.textContent).toContain('Configuration verified')
    verified.app.unmount()

    const probing = await mountCard({ connection: { phase: 'probing' } })
    const button = Array.from(probing.el.querySelectorAll<HTMLButtonElement>('button'))
      .find(candidate => candidate.textContent?.includes('Verifying configuration'))
    expect(button?.getAttribute('aria-busy')).toBe('true')
    probing.app.unmount()
  })

  it.each(['malformed_response', 'invalid_stream_order'])(
    'reports %s as an incompatible stream rather than a connectivity failure',
    async failureKind => {
      const { app, el } = await mountCard({
        connection: verifiedConnection({
          phase: 'unreachable',
          failureKind,
          detail: 'provider stream rejected after finish_reason',
          models: [],
          modelSource: 'none',
        }),
      })

      const status = el.querySelector('[role="status"]')
      expect(status?.textContent).toContain('Streaming response incompatible')
      expect(status?.textContent).not.toContain("Couldn't connect")
      expect(status?.textContent).not.toContain(failureKind)

      app.unmount()
    },
  )

  it('shows first response, complete probe duration, and live model samples when verified', async () => {
    const { app, el } = await mountCard({ connection: verifiedConnection() })

    const verdict = el.querySelector('.setup-connection__verdict')
    expect(verdict).toBeTruthy()
    expect(verdict?.getAttribute('aria-live')).toBe('polite')
    expect(verdict?.textContent).toContain('First model response · 123 ms')
    expect(verdict?.textContent).toContain('Complete probe · 412 ms')
    expect(verdict?.textContent).toContain('4 models')
    expect(verdict?.textContent).toContain('e.g. test-vendor/alpha, test-vendor/beta, test-vendor/gamma')
    expect(verdict?.textContent).not.toContain('test-vendor/delta')

    app.unmount()
  })

  it('joins sample ids with 、 for Chinese locales', async () => {
    i18n.global.locale.value = 'zh-Hans'
    const { app, el } = await mountCard({ connection: verifiedConnection() })

    expect(el.querySelector('.setup-connection__verdict')?.textContent)
      .toContain('test-vendor/alpha、test-vendor/beta、test-vendor/gamma')

    app.unmount()
  })

  it('omits the model summary when discovery returned nothing live', async () => {
    const { app, el } = await mountCard({
      connection: verifiedConnection({ models: [], modelSource: 'none' }),
    })

    const verdict = el.querySelector('.setup-connection__verdict')
    expect(verdict?.textContent).toContain('First model response · 123 ms')
    expect(verdict?.textContent).toContain('Complete probe · 412 ms')
    expect(verdict?.textContent).not.toContain('models')

    app.unmount()
  })

  it('keeps the verdict line empty when timings are unknown and nothing was discovered', async () => {
    const { app, el } = await mountCard({
      connection: verifiedConnection({
        firstResponseMs: null,
        totalMs: null,
        latencyMs: null,
        models: [],
        modelSource: 'none',
      }),
    })

    expect(el.querySelector('.setup-connection__verdict')?.textContent?.trim()).toBe('')
    expect(el.querySelector('.setup-connection__timing')).toBeNull()

    app.unmount()
  })

  it('labels legacy gateway latency explicitly as complete probe duration', async () => {
    const { app, el } = await mountCard({
      connection: verifiedConnection({
        firstResponseMs: null,
        totalMs: 412,
        latencyMs: 412,
        models: [],
        modelSource: 'none',
      }),
    })

    const verdict = el.querySelector('.setup-connection__verdict')
    expect(verdict?.textContent).toContain('Complete probe · 412 ms')
    expect(verdict?.textContent).not.toContain('First model response')

    app.unmount()
  })

  it('appends labeled probe timings to a failure after a model response', async () => {
    const { app, el } = await mountCard({
      connection: {
        phase: 'key_invalid',
        failureKind: 'auth_invalid',
        detail: 'HTTP 401',
        firstResponseMs: 25,
        totalMs: 87,
        latencyMs: null,
        models: [],
        modelSource: 'none',
        discoverError: '',
      },
    })

    const actions = el.querySelector('.setup-connection__actions')
    expect(actions?.textContent).toContain('First model response · 25 ms')
    expect(actions?.textContent).toContain('Complete probe · 87 ms')

    app.unmount()
  })

  it('does not append timings to failure pills when no model probe completed', async () => {
    const { app, el } = await mountCard({
      connection: {
        phase: 'unreachable',
        failureKind: 'transport_transient',
        detail: 'timeout',
        firstResponseMs: null,
        totalMs: null,
        latencyMs: null,
        models: [],
        modelSource: 'none',
        discoverError: '',
      },
    })

    expect(el.querySelector('.setup-connection__actions .setup-connection__timing')).toBeNull()

    app.unmount()
  })
})
