// @vitest-environment happy-dom
import { readFileSync } from 'node:fs'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick } from 'vue'
import i18n from '@/i18n'
import SetupModelStrategyPanel from './SetupModelStrategyPanel.vue'

const FACTS = {
  perTurnCalls: 3,
  proposerCount: 2,
  proposerMaxRetries: 0,
  proposerTimeoutSeconds: 300,
  configuredAggregatorTimeoutSeconds: 3600,
  aggregatorTimeoutSeconds: 480,
}

function customLineup(overrides: Record<string, unknown> = {}) {
  return {
    aggregator: null,
    aggregatorInherited: true,
    inheritedAggregatorProvider: 'openrouter',
    inheritedAggregatorModel: 'deepseek/deepseek-v4-pro',
    proposers: [],
    proposerCount: 0,
    minProposers: 2,
    maxProposers: 6,
    recommendedMin: 3,
    recommendedMax: 4,
    capacity: 'ok',
    canAddProposer: true,
    belowMinimum: true,
    diversityWarning: false,
    facts: FACTS,
    ...overrides,
  }
}

function tierEnsembleStatus(overrides: Record<string, unknown> = {}) {
  return {
    selectionMode: 'router_dynamic',
    activationTiers: ['c3'],
    tierSelectionModes: { c3: 'router_dynamic' },
    runtimeStatus: 'conditional',
    configurationReady: null,
    blockedReason: '',
    blockedTierCandidates: [],
    fixedFallbackReady: true,
    fixedFallbackBlockedReason: '',
    ...overrides,
  }
}

function panel(overrides: Record<string, unknown> = {}) {
  const base = {
    activeStrategy: 'router',
    hasSavedProvider: true,
    profileSaveSupported: true,
    providerLabel: 'OpenRouter',
    routerTemplateState: 'recommended',
    cards: [
      { id: 'router', enabled: true, titleKey: 'setup.modelStrategy.cards.router.title', descKey: 'setup.modelStrategy.cards.router.desc', badgeKey: 'setup.modelStrategy.cards.router.badge' },
      { id: 'single', enabled: false, titleKey: 'setup.modelStrategy.cards.single.title', descKey: 'setup.modelStrategy.cards.single.desc', badgeKey: 'setup.modelStrategy.cards.single.badge' },
      { id: 'ensemble', enabled: false, titleKey: 'setup.modelStrategy.cards.ensemble.title', descKey: 'setup.modelStrategy.cards.ensemble.desc', badgeKey: 'setup.modelStrategy.cards.ensemble.badge' },
    ],
    router: {
      routerDefaultTier: 'c1',
      routerVisualMode: 'real_candidates',
      routerVisualModeOptions: [{ value: 'real_candidates', label: 'Real routing candidates' }],
      routerConfigDisabled: false,
      hasSavedProvider: true,
      textTiers: ['c0', 'c1'],
      tierRows: [
        { name: 'c0', provider: 'openrouter', model: 'deepseek/deepseek-v4-flash', thinkingLevel: 'high', supportsImage: false },
        { name: 'c1', provider: 'openrouter', model: 'deepseek/deepseek-v4-pro', thinkingLevel: 'high', supportsImage: false },
      ],
      tierLabel: (tier: string) => tier,
      providerOptions: [
        { providerId: 'openrouter', label: 'OpenRouter' },
        { providerId: 'deepseek', label: 'DeepSeek' },
        { providerId: 'tokenrhythm', label: 'TokenRhythm' },
      ],
      providerCredentialStatus: [],
      discoveredModelsByProvider: {},
      hasMixedTierProviders: false,
      routerProviderRoles: {},
    },
    single: {
      providerId: 'openrouter',
      providerLabel: 'OpenRouter',
      model: 'deepseek/deepseek-v4-pro',
      models: [],
      modelSource: 'none',
    },
    ensemble: {
      enabled: false,
      activeProvider: 'openrouter',
      activeModel: 'deepseek/deepseek-v4-pro',
      selectionMode: 'custom_b5',
      scheme: 'custom',
      schemeCardsAvailable: true,
      modelOptions: [],
      candidates: [],
      tierCandidates: [
        {
          key: 'tier:openrouter:deepseek/deepseek-v4-flash',
          provider: 'openrouter',
          model: 'deepseek/deepseek-v4-flash',
          source: 'tier',
          enabled: true,
          role: '',
          credential: { provider: 'openrouter', available: true, source: 'env', envKey: 'OPENROUTER_API_KEY' },
        },
        {
          key: 'tier:openrouter:deepseek/deepseek-v4-pro',
          provider: 'openrouter',
          model: 'deepseek/deepseek-v4-pro',
          source: 'tier',
          enabled: true,
          role: '',
          credential: { provider: 'openrouter', available: true, source: 'env', envKey: 'OPENROUTER_API_KEY' },
        },
      ],
      customCandidates: [],
      custom: customLineup(),
      fixedProfile: null,
      presetFacts: {
        perTurnCalls: 5,
        proposerCount: 4,
        proposerMaxRetries: 0,
        proposerTimeoutSeconds: 120,
        configuredAggregatorTimeoutSeconds: 3600,
        aggregatorTimeoutSeconds: 180,
      },
      minSuccessfulProposers: 1,
      allFailedPolicy: 'fallback_single',
      showCandidateEditor: true,
      statusText: 'Ensemble is on.',
    },
  }
  return {
    ...base,
    ...overrides,
    router: {
      ...base.router,
      ...((overrides.router as Record<string, unknown> | undefined) || {}),
    },
    ensemble: {
      ...base.ensemble,
      ...((overrides.ensemble as Record<string, unknown> | undefined) || {}),
    },
    single: {
      ...base.single,
      ...((overrides.single as Record<string, unknown> | undefined) || {}),
    },
  }
}

async function mountPanel(props = {}, listeners: Record<string, unknown> = {}) {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(SetupModelStrategyPanel, { panel: panel(props), ...listeners })
  app.use(i18n)
  app.mount(el)
  await nextTick()
  return { app, el }
}

beforeEach(() => {
  i18n.global.locale.value = 'en'
  document.body.innerHTML = ''
})

describe('SetupModelStrategyPanel', () => {
  it('renders an ordered set of native routing choices with clear guidance', async () => {
    const { app, el } = await mountPanel()

    expect(el.querySelector('[role="radiogroup"]')?.getAttribute('aria-label')).toBe('Choose how models are used')
    expect(el.textContent).toContain('Intelligent model routing')
    expect(el.textContent).toContain('Fixed model')
    expect(el.textContent).toContain('Model ensemble')
    expect(el.textContent).toContain(
      'Candidate models answer in parallel, then one aggregator model produces the final reply.',
    )
    expect(el.querySelector('[role="radiogroup"]')).toBeTruthy()
    const choices = Array.from(el.querySelectorAll<HTMLInputElement>('input[type="radio"][name="setup_model_strategy"]'))
    expect(choices).toHaveLength(3)
    expect(choices.map(choice => choice.value)).toEqual(['router', 'single', 'ensemble'])
    expect(choices[0]?.checked).toBe(true)
    const strategyRowsText = el.querySelector('[role="radiogroup"]')?.textContent || ''
    expect(strategyRowsText).toContain('Token-efficient')
    expect(strategyRowsText).toContain('Predictable')
    expect(strategyRowsText).toContain('Capability-first')
    const strategyBadges = Array.from(el.querySelectorAll<HTMLElement>('.setup-model-strategy__card .control-pill'))
    expect(strategyBadges).toHaveLength(3)
    expect(strategyBadges.every(badge => badge.classList.contains('control-pill--info'))).toBe(true)
    expect(strategyBadges.some(badge => badge.classList.contains('control-pill--ok'))).toBe(false)
    expect(strategyBadges.some(badge => badge.classList.contains('control-pill--queued'))).toBe(false)
    expect(strategyRowsText).not.toContain('Recommended')
    expect(strategyRowsText).not.toContain('Advanced')
    expect(strategyRowsText).not.toContain('Default')
    expect(strategyRowsText).not.toContain('Multi-model collaboration')
    expect(el.textContent).not.toContain('Preset and credentials')
    expect(el.textContent).not.toContain('OpenRouter aggregated')
    expect(el.textContent).not.toContain('OpenRouter mix')
    expect(el.textContent).not.toContain('openrouter-mix')
    expect(el.textContent).not.toContain('router_dynamic')
    expect(el.textContent).not.toContain('static_openrouter_b5')

    app.unmount()
  })

  it('emits the selected strategy from a strategy card', async () => {
    const onUpdateStrategy = vi.fn()
    const { app, el } = await mountPanel({}, { onUpdateStrategy })

    el.querySelector<HTMLInputElement>('input[name="setup_model_strategy"][value="ensemble"]')?.click()
    await nextTick()

    expect(onUpdateStrategy).toHaveBeenCalledWith('ensemble')
    app.unmount()
  })

  it('places model service management beside the page description', async () => {
    const onGoToSection = vi.fn()
    const { app, el } = await mountPanel({}, { onGoToSection })
    const pageMeta = el.querySelector('.setup-model-strategy__page-meta')
    const manageButton = pageMeta?.querySelector<HTMLButtonElement>('button')

    expect(pageMeta?.textContent).toContain('Choose how models are picked for each request.')
    expect(manageButton?.textContent).toContain('Manage Model Service')
    expect(el.textContent).not.toContain('Models come from Model Service.')

    manageButton?.click()
    await nextTick()

    expect(onGoToSection).toHaveBeenCalledWith('provider')
    app.unmount()
  })

  it('shows a provider-first empty state without selectable routing modes', async () => {
    const onGoToSection = vi.fn()
    const onUpdateStrategy = vi.fn()
    const { app, el } = await mountPanel(
      { hasSavedProvider: false },
      { onGoToSection, onUpdateStrategy },
    )

    expect(el.querySelector('[data-testid="model-strategy-provider-first"]')).toBeTruthy()
    expect(el.textContent).toContain('Add a model provider to start')
    expect(el.querySelector('[role="radiogroup"]')).toBeNull()
    expect(el.querySelector('input[name="setup_model_strategy"]')).toBeNull()

    el.querySelector<HTMLButtonElement>('[data-testid="model-strategy-provider-first"] button')?.click()
    await nextTick()

    expect(onGoToSection).toHaveBeenCalledWith('provider')
    expect(onUpdateStrategy).not.toHaveBeenCalled()
    app.unmount()
  })

  it('shows router details when model router is active', async () => {
    const { app, el } = await mountPanel({ activeStrategy: 'router' })

    expect(el.textContent).toContain('Model roles')
    expect(el.textContent).toContain('Pick a model for each request level. One provider can supply every level.')
    expect(el.textContent).not.toContain('Preset and credentials from OpenRouter')
    expect(el.querySelector('[role="table"]')).toBeTruthy()
    // The fallback tier rides in the collapsed advanced fold: rarely needed,
    // but a saved non-default tier must stay reachable from the UI.
    const fallbackTier = el.querySelector<HTMLSelectElement>('select[name="setup_model_strategy_router_default_tier"]')
    expect(fallbackTier).toBeTruthy()
    const advanced = fallbackTier?.closest<HTMLDetailsElement>('details')
    expect(advanced?.open).toBe(false)
    expect(advanced?.querySelector('summary')?.textContent).toContain('Advanced options')
    advanced?.querySelector<HTMLElement>('summary')?.click()
    await nextTick()
    expect(advanced?.open).toBe(true)
    expect(el.textContent).toContain('When routing is uncertain')

    app.unmount()
  })

  it('offers a compact provider shortcut when only one provider is available', async () => {
    const onGoToSection = vi.fn()
    const { app, el } = await mountPanel({
      activeStrategy: 'router',
      router: {
        providerOptions: [
          { providerId: 'openrouter', label: 'OpenRouter' },
          { providerId: 'retired-provider', label: 'Retired provider', disabled: true },
        ],
      },
    }, { onGoToSection })

    const shortcut = el.querySelector<HTMLButtonElement>('[data-testid="router-add-provider"]')
    const hint = 'Add another model provider to choose a different provider for each tier.'
    expect(shortcut?.textContent?.trim()).toBe('Add provider')
    expect(shortcut?.hasAttribute('aria-label')).toBe(false)
    expect(shortcut?.getAttribute('title')).toBe(hint)
    expect(shortcut?.getAttribute('aria-describedby')).toBe('router-add-provider-hint')
    const description = el.querySelector<HTMLElement>('#router-add-provider-hint')
    expect(description?.textContent?.trim()).toBe(hint)
    expect(description?.classList.contains('setup-model-strategy__sr-only')).toBe(true)
    expect(el.querySelector('[aria-label="c0 request entry"]')).toBeNull()

    shortcut?.click()
    await nextTick()

    expect(onGoToSection).toHaveBeenCalledWith('provider')
    app.unmount()
  })

  it('removes the provider shortcut once tier provider choices are available', async () => {
    const { app, el } = await mountPanel({ activeStrategy: 'router' })

    expect(el.querySelector('[data-testid="router-add-provider"]')).toBeNull()
    expect(el.querySelector('[aria-label="c0 request entry"]')).toBeTruthy()
    expect(el.querySelector('.setup-tier-table__row.is-head')?.textContent)
      .toContain('Request entry')

    app.unmount()
  })

  it('does not offer an add-provider shortcut on a legacy Gateway', async () => {
    const { app, el } = await mountPanel({
      activeStrategy: 'router',
      profileSaveSupported: false,
      router: {
        providerOptions: [{ providerId: 'openrouter', label: 'OpenRouter' }],
      },
    })

    expect(el.querySelector('[data-testid="router-add-provider"]')).toBeNull()
    app.unmount()
  })

  it('lets a router tier choose a discovered model from the model input', async () => {
    const onUpdateTierField = vi.fn()
    const discoveredModels = [
      {
        id: 'test-vendor/alpha',
        name: 'Alpha',
        contextWindow: 262144,
        maxOutputTokens: 16384,
        capabilities: ['chat', 'tools'],
        pricing: null,
        capabilitySource: 'provider',
      },
    ]
    const { app, el } = await mountPanel(
      {
        router: {
          discoveredModelsByProvider: {
            openrouter: { models: discoveredModels, source: 'live' },
          },
        },
      },
      { onUpdateTierField },
    )

    const input = el.querySelector<HTMLInputElement>(
      'input[role="combobox"][aria-label="c0 model"]',
    )!
    expect(input.getAttribute('aria-expanded')).toBe('false')
    input.dispatchEvent(new Event('focus'))
    await nextTick()
    expect(input.getAttribute('aria-expanded')).toBe('true')

    const option = Array.from(
      document.querySelectorAll<HTMLButtonElement>('[role="option"]'),
    ).find(row => row.textContent?.includes('test-vendor/alpha'))

    expect(option).toBeTruthy()
    option!.click()
    await nextTick()

    expect(onUpdateTierField).toHaveBeenCalledWith('c0', 'model', 'test-vendor/alpha')
    app.unmount()
  })

  it('no longer renders the retired routing-panel-style select', async () => {
    // The cosmetic visual-mode picker was cut in the mist declutter pass; the
    // saved value keeps applying, it is just not editable from this page.
    const { app, el } = await mountPanel({
      router: {
        routerVisualModeOptions: [
          { value: 'real_candidates', label: 'Real routing candidates' },
          { value: 'legacy_grid', label: 'Three-tier visual panel' },
        ],
      },
    })

    expect(el.querySelector('select[name="setup_model_strategy_router_visual_mode"]')).toBeNull()
    app.unmount()
  })

  it('explains that one provider can supply every routing level', async () => {
    const { app, el } = await mountPanel({
      providerLabel: 'Groq',
      ensemble: {
        activeProvider: 'groq',
        activeModel: 'llama-3.3-70b-versatile',
      },
      router: {
        ...panel().router,
        routerDefaultTier: 'c1',
        textTiers: ['c1'],
        tierRows: [
          { name: 'c1', provider: 'groq', model: 'llama-3.3-70b-versatile', thinkingLevel: '', supportsImage: false },
        ],
      },
    })

    expect(el.textContent).toContain('One provider can supply every level.')
    expect(el.textContent).not.toContain('provider default model')

    app.unmount()
  })

  it('binds shared C3 fusion to the global fixed and fallback target', async () => {
    const { app, el } = await mountPanel({
      router: {
        routerProviderRoles: { c3: 'dormant_draft' },
        tierRows: [{
          name: 'c3',
          provider: 'tokenrhythm',
          model: 'sleeping-c3-model',
          thinkingLevel: 'high',
          supportsImage: false,
          ensembleEnabled: true,
        }],
      },
      single: {
        providerId: 'deepseek',
        providerLabel: 'DeepSeek',
        model: 'deepseek-chat',
      },
      ensemble: { allFailedPolicy: 'fallback_single' },
    })

    const picker = el.querySelector<HTMLInputElement>(
      'input[aria-label="C3 processing mode or model"]',
    )
    expect(picker?.value).toBe('Multi-model fusion')
    expect(picker?.getAttribute('aria-describedby'))
      .toContain('setup-tier-c3-ensemble-summary')
    const summary = el.querySelector('.setup-tier-table__model-note')?.textContent || ''
    expect(summary).toContain('Fixed and fallback model: DeepSeek · deepseek-chat')
    expect(summary).not.toContain('sleeping-c3-model')
    expect(el.textContent).toContain('Determined by Multi-model fusion')
    expect(el.textContent).toContain('Current fusion plan needs attention')
    app.unmount()
  })

  it('shows a compact ready state for a healthy shared C3 plan', async () => {
    const readyCandidate = {
      key: 'custom:proposer:openrouter:ready-model',
      provider: 'openrouter',
      model: 'ready-model',
      source: 'custom',
      enabled: true,
      role: '',
      credential: { provider: 'openrouter', available: true, source: 'env' },
    }
    const { app, el } = await mountPanel({
      router: {
        routerProviderRoles: { c3: 'dormant_draft' },
        tierEnsembleStatusFresh: true,
        tierEnsembleStatus: tierEnsembleStatus({
          selectionMode: 'custom_b5',
          tierSelectionModes: { c3: 'custom_b5' },
          proposerCount: 4,
          configuredMinSuccessfulProposers: 1,
          effectiveMinSuccessfulProposers: 1,
          configuredProposerMaxRetries: 0,
          effectiveProposerMaxRetries: 1,
          proposerMaxRetriesSource: 'c3_default',
        }),
        tierRows: [{
          name: 'c3',
          provider: 'openrouter',
          model: 'sleeping-c3-model',
          thinkingLevel: 'high',
          supportsImage: false,
          ensembleEnabled: true,
        }],
      },
      ensemble: {
        custom: customLineup({
          proposers: [readyCandidate, { ...readyCandidate, key: 'custom:proposer:openrouter:ready-model-2', model: 'ready-model-2' }],
          proposerCount: 2,
          belowMinimum: false,
        }),
      },
    })

    expect(el.querySelector('.setup-tier-table__model-note')).toBeNull()
    expect(el.querySelector('.setup-tier-table__plan-status')).toBeNull()
    expect(el.querySelector('button[aria-label="Show C3 fusion details"]')).toBeTruthy()
    expect(el.querySelector('[role="tooltip"]')?.textContent)
      .toContain('Current fusion plan is ready')
    expect(el.querySelector('[role="tooltip"]')?.textContent)
      .toContain('at least 1/4 proposers return normally')
    expect(el.querySelector('[role="tooltip"]')?.textContent)
      .toContain('per-proposer retry limit 1')
    app.unmount()
  })

  it('evaluates a legacy dynamic shared plan from router tier candidates', async () => {
    const onMigrateEnsembleLegacy = vi.fn()
    const tierCandidate = {
      key: 'tier:openrouter:dynamic-member',
      provider: 'openrouter',
      model: 'dynamic-member',
      source: 'tier',
      enabled: true,
      role: '',
      credential: { provider: 'openrouter', available: true, source: 'env' },
    }
    const { app, el } = await mountPanel({
      router: {
        routerProviderRoles: { c3: 'dynamic_member' },
        tierRows: [{
          name: 'c3',
          provider: 'openrouter',
          model: 'dynamic-member',
          thinkingLevel: 'high',
          supportsImage: false,
          ensembleEnabled: true,
        }],
      },
      ensemble: {
        selectionMode: 'router_dynamic',
        scheme: 'legacy',
        tierCandidates: [tierCandidate],
        customCandidates: [],
      },
    }, { onMigrateEnsembleLegacy })

    expect(el.querySelector('.setup-tier-table__plan-status')).toBeNull()
    expect(el.querySelector('.setup-tier-table__model-note')?.textContent)
      .toContain('previously saved tier-following fusion plan')
    expect(el.querySelector('[data-testid="tier-ensemble-migrate-legacy"]')).toBeTruthy()
    el.querySelector<HTMLButtonElement>('[data-testid="tier-ensemble-migrate-legacy"]')?.click()
    expect(onMigrateEnsembleLegacy).toHaveBeenCalledOnce()
    app.unmount()
  })

  it.each([
    {
      name: 'route mode through the fixed provider',
      credentialAvailable: false,
      status: tierEnsembleStatus(),
    },
    {
      name: 'veto mode with a foreign tier excluded',
      credentialAvailable: false,
      status: tierEnsembleStatus({
        blockedTierCandidates: [{
          source: 'router_tier:c0',
          provider: 'foreign-provider',
          model: 'foreign-model',
          reason: 'cross_provider_veto',
        }],
      }),
    },
    {
      name: 'ready cross-provider deployment',
      credentialAvailable: true,
      status: tierEnsembleStatus(),
    },
  ])('trusts saved dynamic runtime status for $name', async ({ credentialAvailable, status }) => {
    const tierCandidate = {
      key: 'tier:foreign-provider:foreign-model',
      provider: 'foreign-provider',
      model: 'foreign-model',
      source: 'tier',
      enabled: true,
      role: '',
      credential: {
        provider: 'foreign-provider',
        available: credentialAvailable,
        source: credentialAvailable ? 'env' : 'none',
      },
    }
    const { app, el } = await mountPanel({
      router: {
        routerProviderRoles: { c3: 'dynamic_member' },
        tierEnsembleStatus: status,
        tierEnsembleStatusFresh: true,
        tierRows: [{
          name: 'c3',
          provider: 'foreign-provider',
          model: 'foreign-model',
          thinkingLevel: 'high',
          supportsImage: false,
          ensembleEnabled: true,
        }],
      },
      ensemble: {
        selectionMode: 'router_dynamic',
        scheme: 'legacy',
        tierCandidates: [tierCandidate],
        customCandidates: [],
      },
    })

    expect(el.querySelector('.setup-tier-table__plan-status')).toBeNull()
    expect(el.querySelector('.setup-tier-table__model-note')?.textContent)
      .toContain('previously saved tier-following fusion plan')
    expect(el.querySelector('[data-testid="tier-ensemble-migrate-legacy"]')).toBeTruthy()
    app.unmount()
  })

  it('shows a saved unready dynamic deployment as an actionable inline error', async () => {
    const { app, el } = await mountPanel({
      router: {
        routerProviderRoles: { c3: 'dynamic_member' },
        tierEnsembleStatus: tierEnsembleStatus({
          runtimeStatus: 'blocked',
          configurationReady: false,
          blockedReason: 'router_dynamic_not_ready:missing_credential',
        }),
        tierEnsembleStatusFresh: true,
        tierRows: [{
          name: 'c3',
          provider: 'foreign-provider',
          model: 'foreign-model',
          thinkingLevel: 'high',
          supportsImage: false,
          ensembleEnabled: true,
        }],
      },
      ensemble: {
        selectionMode: 'router_dynamic',
        scheme: 'legacy',
        tierCandidates: [],
        customCandidates: [],
      },
    })

    expect(el.querySelector('.setup-tier-table__plan-status')?.textContent)
      .toContain('Current fusion plan is unavailable')
    expect(el.querySelector('.setup-tier-table__blocked-reason')?.textContent)
      .toContain('One or more models in this fusion plan are not ready')
    expect(el.querySelector('.setup-tier-table__model-note')).toBeTruthy()
    app.unmount()
  })

  it('falls back to conservative local validation when saved tier status is null or stale', async () => {
    const tierCandidate = {
      key: 'tier:foreign-provider:foreign-model',
      provider: 'foreign-provider',
      model: 'foreign-model',
      source: 'tier',
      enabled: true,
      role: '',
      credential: { provider: 'foreign-provider', available: false, source: 'none' },
    }
    for (const routerStatus of [
      { tierEnsembleStatus: null, tierEnsembleStatusFresh: true },
      { tierEnsembleStatus: tierEnsembleStatus(), tierEnsembleStatusFresh: false },
    ]) {
      const { app, el } = await mountPanel({
        router: {
          routerProviderRoles: { c3: 'dynamic_member' },
          ...routerStatus,
          tierRows: [{
            name: 'c3',
            provider: 'foreign-provider',
            model: 'foreign-model',
            thinkingLevel: 'high',
            supportsImage: false,
            ensembleEnabled: true,
          }],
        },
        ensemble: {
          selectionMode: 'router_dynamic',
          scheme: 'legacy',
          tierCandidates: [tierCandidate],
          customCandidates: [],
        },
      })

      expect(el.querySelector('.setup-tier-table__plan-status')?.textContent)
        .toContain('Current fusion plan needs attention')
      app.unmount()
    }
  })

  it('never upgrades an invalidated saved blocker to ready after an unrelated local edit', async () => {
    const { app, el } = await mountPanel({
      router: {
        routerProviderRoles: { c3: 'dormant_draft' },
        tierEnsembleStatus: tierEnsembleStatus({
          selectionMode: 'static_tokenrhythm_b5',
          tierSelectionModes: { c3: 'static_tokenrhythm_b5' },
          runtimeStatus: 'blocked',
          configurationReady: false,
          blockedReason: 'fixed_fallback:missing_credential:tokenrhythm',
          fixedFallbackReady: false,
          fixedFallbackBlockedReason: 'fixed_fallback:missing_credential:tokenrhythm',
        }),
        tierEnsembleStatusFresh: false,
        tierRows: [{
          name: 'c3',
          provider: 'tokenrhythm',
          model: 'quality-model',
          thinkingLevel: 'high',
          supportsImage: false,
          ensembleEnabled: true,
        }],
      },
      ensemble: {
        selectionMode: 'static_tokenrhythm_b5',
        scheme: 'preset',
        fixedProfile: {
          provider: 'tokenrhythm',
          providerLabel: 'TokenRhythm',
          proposers: [],
          aggregator: null,
        },
      },
    })

    expect(el.querySelector('.setup-tier-table__plan-status')?.textContent)
      .toContain('Current fusion plan needs attention')
    app.unmount()
  })

  it('rejects an ambiguous mixed-tier runtime aggregate instead of borrowing it for C3', async () => {
    const { app, el } = await mountPanel({
      router: {
        routerProviderRoles: { c3: 'dynamic_member' },
        tierEnsembleStatus: tierEnsembleStatus({
          activationTiers: ['c0', 'c3'],
          tierSelectionModes: {
            c0: 'static_openrouter_b5',
            c3: 'router_dynamic',
          },
        }),
        tierEnsembleStatusFresh: true,
        tierRows: [{
          name: 'c3',
          provider: 'foreign-provider',
          model: 'foreign-model',
          thinkingLevel: 'high',
          supportsImage: false,
          ensembleEnabled: true,
        }],
      },
      ensemble: {
        selectionMode: 'router_dynamic',
        scheme: 'legacy',
        tierCandidates: [{
          key: 'tier:foreign-provider:foreign-model',
          provider: 'foreign-provider',
          model: 'foreign-model',
          source: 'tier',
          enabled: true,
          role: '',
          credential: { provider: 'foreign-provider', available: false, source: 'none' },
        }],
        customCandidates: [],
      },
    })

    expect(el.querySelector('.setup-tier-table__plan-status')?.textContent)
      .toContain('Current fusion plan needs attention')
    app.unmount()
  })

  it('surfaces a blocked shared-provider role inline', async () => {
    const { app, el } = await mountPanel({
      router: {
        routerProviderRoles: { c3: 'blocked' },
        tierRows: [{
          name: 'c3',
          provider: 'openrouter',
          model: 'saved-draft',
          thinkingLevel: 'high',
          supportsImage: false,
          ensembleEnabled: true,
        }],
      },
    })

    expect(el.querySelector('.setup-tier-table__plan-status')?.textContent)
      .toContain('Current fusion plan is unavailable')
    expect(el.querySelector('.setup-tier-table__model-note')).toBeTruthy()
    app.unmount()
  })

  it('shows the explicit error policy for shared C3', async () => {
    const { app, el } = await mountPanel({
      router: {
        tierRows: [{
          name: 'c3',
          provider: 'openrouter',
          model: 'sleeping-c3-model',
          thinkingLevel: 'high',
          supportsImage: false,
          ensembleEnabled: true,
        }],
      },
      ensemble: { allFailedPolicy: 'error' },
    })

    const summary = el.querySelector('.setup-tier-table__model-note')?.textContent || ''
    expect(summary).toContain('reports an error')
    expect(summary).not.toContain('uses the Fixed and fallback model')
    app.unmount()
  })

  it('keeps router tier editing enabled after leaving an enabled ensemble strategy', async () => {
    const { app, el } = await mountPanel({
      activeStrategy: 'router',
      router: {
        ...panel().router,
        routerConfigDisabled: true,
      },
    })

    expect(el.querySelector<HTMLSelectElement>('select[name="setup_model_strategy_router_default_tier"]')?.disabled).toBe(false)
    expect(el.querySelector<HTMLInputElement>('input[aria-label="c0 model"]')?.disabled).toBe(false)
    expect(el.querySelector('[role="table"]')?.getAttribute('aria-disabled')).toBeNull()

    app.unmount()
  })

  it('shows a proposer-first custom lineup without legacy advisory roles', async () => {
    const proposer = {
      key: 'custom:proposer:deepseek:deepseek-v4-pro',
      provider: 'deepseek',
      model: 'deepseek-v4-pro',
      source: 'custom',
      enabled: true,
      role: 'primary',
      credential: { provider: 'deepseek', available: true, source: 'explicit', envKey: 'DEEPSEEK_API_KEY' },
    }
    const { app, el } = await mountPanel({
      activeStrategy: 'ensemble',
      ensemble: {
        enabled: true,
        scheme: 'custom',
        custom: customLineup({
          proposers: [proposer],
          proposerCount: 1,
          belowMinimum: true,
        }),
      },
    })

    const lineup = el.querySelector<HTMLElement>('[data-testid="ensemble-custom-lineup"]')!
    const steps = lineup.querySelectorAll<HTMLElement>('.setup-model-strategy__step')
    expect(steps).toHaveLength(2)
    expect(steps[0]?.textContent).toContain('Proposer')
    expect(steps[1]?.textContent).toContain('Aggregator')
    expect(lineup.querySelector('.setup-model-strategy__handoff')).toBeNull()
    expect(lineup.textContent!.indexOf('Proposer')).toBeLessThan(lineup.textContent!.indexOf('Aggregator'))
    expect(el.querySelector('[data-testid="ensemble-custom-aggregator-inherited"]')?.textContent)
      .toContain('deepseek/deepseek-v4-pro')
    expect(steps[0]?.textContent).toContain('Proposers')
    expect(el.textContent).toContain('DeepSeek · deepseek-v4-pro')
    expect(el.textContent).not.toContain('Primary')
    expect(el.textContent).not.toContain('Contrast')
    expect(el.textContent).not.toContain('Fast check')
    expect(el.textContent).not.toContain('Critic')
    expect(el.textContent).not.toContain('No role')
    expect(el.querySelector('select[aria-label^="Role for"]')).toBeNull()
    expect(el.querySelector('option[value="primary"], option[value="contrast"], option[value="fast_check"], option[value="critic"]')).toBeNull()
    expect(el.querySelector('[data-testid="ensemble-below-minimum"]')).toBeTruthy()
    expect(el.querySelector('[data-testid="ensemble-effective-summary"]')?.textContent)
      .toContain('3 model calls')
    expect(el.querySelector('[role="table"]')).toBeNull()

    app.unmount()
  })

  it('edits the current provider model in fixed mode without calling it a routing default', async () => {
    const onUpdateFixedProvider = vi.fn()
    const onUpdateFixedModel = vi.fn()
    const discoveredModel = {
      id: 'deepseek/deepseek-v4-flash',
      name: 'DeepSeek V4 Flash',
      contextWindow: 128000,
      maxOutputTokens: 8192,
      capabilities: ['chat'],
      pricing: null,
      capabilitySource: 'provider',
    }
    const { app, el } = await mountPanel({
      activeStrategy: 'single',
      single: {
        model: discoveredModel.id,
        models: [discoveredModel],
        modelSource: 'live',
      },
    }, { onUpdateFixedProvider, onUpdateFixedModel })

    const detail = el.querySelector('.setup-model-strategy__detail')?.textContent || ''
    const provider = el.querySelector<HTMLButtonElement>('[data-testid="setup-model-strategy-fixed-provider-trigger"]')
    const input = el.querySelector<HTMLInputElement>('input[name="setup_provider_model_strategy_fixed_model"]')
    expect(detail).toContain('Current model provider')
    expect(detail).toContain('OpenRouter')
    expect(provider?.getAttribute('role')).toBe('combobox')
    expect(provider?.getAttribute('aria-expanded')).toBe('false')
    expect(provider?.textContent).toContain('OpenRouter')
    expect(provider?.closest('.setup-model-strategy__single-provider-control')).toBeTruthy()
    expect(provider?.parentElement?.querySelector('.setup-model-strategy__single-provider-chevron')).toBeTruthy()
    expect(input?.value).toBe(discoveredModel.id)
    expect(detail).not.toContain('default tier')

    if (provider) {
      provider.click()
      await nextTick()
      expect(provider.getAttribute('aria-expanded')).toBe('true')
      const options = Array.from(document.querySelectorAll<HTMLElement>('#setup-model-strategy-fixed-provider-listbox [role="option"]'))
      expect(options.map(option => option.textContent?.trim()))
        .toEqual(['OpenRouter', 'DeepSeek', 'TokenRhythm'])
      expect(options.every(option => option.tabIndex === -1)).toBe(true)
      provider.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', bubbles: true }))
      await nextTick()
      expect(provider.getAttribute('aria-expanded')).toBe('false')
      provider.click()
      await nextTick()
      provider.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }))
      provider.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
      await nextTick()
    }
    expect(onUpdateFixedProvider).toHaveBeenCalledWith('deepseek')
    expect(provider?.getAttribute('aria-expanded')).toBe('false')

    const source = readFileSync('src/components/setup/SetupModelStrategyPanel.vue', 'utf8')
    expect(source).toContain('rotate(180deg)')
    expect(source).toContain('FIXED_PROVIDER_MENU_MAX_HEIGHT = 272')
    expect(source).toContain('<Teleport to="body">')
    expect(source).toContain('position: fixed')
    expect(source).toContain('scrollIntoView')
    expect(source).toContain('fixed-provider-menu-enter-from')

    if (input) {
      input.value = 'deepseek/deepseek-v4-pro'
      input.dispatchEvent(new Event('input', { bubbles: true }))
      await nextTick()
    }
    expect(onUpdateFixedModel).toHaveBeenCalledWith('deepseek/deepseek-v4-pro')

    app.unmount()
  })

  it('keeps a long provider menu keyboard-visible and closes when focus leaves', async () => {
    const providerOptions = Array.from({ length: 24 }, (_, index) => ({
      providerId: `provider-${index}`,
      label: `Provider ${index}`,
    }))
    const { app, el } = await mountPanel({
      activeStrategy: 'single',
      router: { providerOptions },
      single: {
        providerId: 'provider-0',
        providerLabel: 'Provider 0',
      },
    })
    const trigger = el.querySelector<HTMLButtonElement>('[data-testid="setup-model-strategy-fixed-provider-trigger"]')!
    trigger.click()
    await nextTick()

    const listbox = document.getElementById('setup-model-strategy-fixed-provider-listbox')!
    const options = Array.from(listbox.querySelectorAll<HTMLElement>('[role="option"]'))
    expect(options).toHaveLength(24)
    const scrollIntoView = vi.fn()
    options[23]!.scrollIntoView = scrollIntoView
    trigger.dispatchEvent(new KeyboardEvent('keydown', { key: 'End', bubbles: true }))
    await nextTick()
    await nextTick()
    expect(trigger.getAttribute('aria-activedescendant'))
      .toBe('setup-model-strategy-fixed-provider-option-23')
    expect(scrollIntoView).toHaveBeenCalledWith({ block: 'nearest' })

    const modelInput = el.querySelector<HTMLInputElement>('input[name="setup_provider_model_strategy_fixed_model"]')!
    trigger.dispatchEvent(new FocusEvent('focusout', { bubbles: true, relatedTarget: modelInput }))
    await nextTick()
    expect(trigger.getAttribute('aria-expanded')).toBe('false')

    app.unmount()
  })

  it('adds and imports candidates with the canonical proposer role', async () => {
    const onAddEnsembleCandidate = vi.fn()
    const onImportEnsembleTierCandidates = vi.fn()
    const onRequestProviderModels = vi.fn()
    const customCandidate = {
      key: 'custom:proposer:deepseek:deepseek-v4-pro',
      provider: 'deepseek',
      model: 'deepseek-v4-pro',
      source: 'custom',
      enabled: true,
      role: '',
      credential: { provider: 'deepseek', available: true, source: 'explicit', envKey: 'DEEPSEEK_API_KEY' },
    }
    const { app, el } = await mountPanel(
      {
        activeStrategy: 'ensemble',
        ensemble: {
          enabled: true,
          scheme: 'custom',
          custom: customLineup({ proposers: [customCandidate], proposerCount: 1 }),
        },
      },
      {
        onAddEnsembleCandidate,
        onImportEnsembleTierCandidates,
        onRequestProviderModels,
      },
    )

    expect(el.textContent).toContain('DeepSeek · deepseek-v4-pro')
    expect(el.textContent).toContain('Connected')

    el.querySelector<HTMLButtonElement>('[data-testid="setup-model-strategy-add-candidate-trigger"]')?.click()
    await nextTick()

    const provider = el.querySelector<HTMLSelectElement>('[aria-label="Candidate provider"]')!
    expect(provider.value).toBe('openrouter')
    provider.value = 'deepseek'
    provider.dispatchEvent(new Event('change', { bubbles: true }))
    await nextTick()
    expect(onRequestProviderModels).toHaveBeenCalledWith('deepseek')
    const add = el.querySelector<HTMLButtonElement>('[data-testid="setup-model-strategy-add-candidate"]')
    expect(add?.disabled).toBe(true)

    const model = el.querySelector<HTMLInputElement>('input[name="setup_provider_ensemble_candidate_model"]')
    model!.value = 'claude-opus'
    model!.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()
    expect(add?.disabled).toBe(false)
    add?.click()
    await nextTick()
    expect(onAddEnsembleCandidate).toHaveBeenCalledWith('deepseek', 'claude-opus', 'proposer')

    el.querySelector<HTMLButtonElement>('[data-testid="setup-model-strategy-import-tiers"]')?.click()
    await nextTick()
    expect(onImportEnsembleTierCandidates).toHaveBeenCalledOnce()

    app.unmount()
  })

  it('copies a proposer into the aggregator slot and replaces a proposer atomically', async () => {
    const onAddEnsembleCandidate = vi.fn()
    const onRemoveEnsembleCandidate = vi.fn()
    const onReplaceEnsembleCandidate = vi.fn()
    const onSetEnsembleAggregator = vi.fn()
    const customCandidate = {
      key: 'custom:proposer:deepseek:deepseek-v4-pro',
      provider: 'deepseek',
      model: 'deepseek-v4-pro',
      source: 'custom',
      enabled: true,
      role: 'critic',
      credential: { provider: 'deepseek', available: true, source: 'explicit', envKey: 'DEEPSEEK_API_KEY' },
    }
    const { app, el } = await mountPanel(
      {
        activeStrategy: 'ensemble',
        ensemble: {
          enabled: true,
          scheme: 'custom',
          custom: customLineup({ proposers: [customCandidate], proposerCount: 1 }),
        },
      },
      {
        onAddEnsembleCandidate,
        onRemoveEnsembleCandidate,
        onReplaceEnsembleCandidate,
        onSetEnsembleAggregator,
      },
    )

    const actions = el.querySelector<HTMLDetailsElement>('.setup-model-strategy__candidate-actions')!
    actions.open = true
    await nextTick()
    actions.querySelector<HTMLButtonElement>('[data-testid="ensemble-promote-aggregator"]')?.click()
    await nextTick()
    expect(onSetEnsembleAggregator).toHaveBeenCalledWith(
      customCandidate.provider,
      customCandidate.model,
    )
    expect(actions.open).toBe(false)

    actions.open = true
    actions.querySelector<HTMLButtonElement>('[data-testid="ensemble-replace-proposer"]')?.click()
    await nextTick()
    expect(el.querySelector('[data-testid="ensemble-replace-proposer-editor"]')).toBeTruthy()
    const replacement = el.querySelector<HTMLInputElement>(
      'input[name="setup_provider_ensemble_candidate_replacement"]',
    )!
    replacement.value = 'deepseek-v4-next'
    replacement.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()
    el.querySelector<HTMLButtonElement>('[data-testid="ensemble-replace-proposer-confirm"]')?.click()
    await nextTick()

    expect(onReplaceEnsembleCandidate).toHaveBeenCalledWith(
      customCandidate,
      'deepseek',
      'deepseek-v4-next',
    )
    expect(onRemoveEnsembleCandidate).not.toHaveBeenCalled()
    expect(onAddEnsembleCandidate).not.toHaveBeenCalled()

    app.unmount()
  })

  it('blocks replacing a proposer with a duplicate from the same provider', async () => {
    const onReplaceEnsembleCandidate = vi.fn()
    const first = {
      key: 'custom:proposer:deepseek:deepseek-v4-pro',
      provider: 'deepseek',
      model: 'deepseek-v4-pro',
      source: 'custom',
      enabled: true,
      role: 'primary',
    }
    const duplicate = {
      key: 'custom:proposer:deepseek:deepseek-v4-flash',
      provider: 'deepseek',
      model: 'deepseek-v4-flash',
      source: 'custom',
      enabled: true,
      role: '',
    }
    const { app, el } = await mountPanel(
      {
        activeStrategy: 'ensemble',
        ensemble: {
          enabled: true,
          scheme: 'custom',
          custom: customLineup({
            proposers: [first, duplicate],
            proposerCount: 2,
            belowMinimum: false,
          }),
        },
      },
      { onReplaceEnsembleCandidate },
    )

    const actions = el.querySelector<HTMLDetailsElement>('.setup-model-strategy__candidate-actions')!
    actions.open = true
    actions.querySelector<HTMLButtonElement>('[data-testid="ensemble-replace-proposer"]')?.click()
    await nextTick()

    const replacement = el.querySelector<HTMLInputElement>(
      'input[name="setup_provider_ensemble_candidate_replacement"]',
    )!
    replacement.value = duplicate.model
    replacement.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()

    expect(el.querySelector('[data-testid="ensemble-replacement-duplicate"]')?.textContent)
      .toContain('already in the proposer list')
    const confirm = el.querySelector<HTMLButtonElement>(
      '[data-testid="ensemble-replace-proposer-confirm"]',
    )!
    expect(confirm.disabled).toBe(true)
    confirm.click()
    await nextTick()
    expect(onReplaceEnsembleCandidate).not.toHaveBeenCalled()

    app.unmount()
  })

  it('shows an unconfigured historical proposer without allowing it to be reused', async () => {
    const onReplaceEnsembleCandidate = vi.fn()
    const historical = {
      key: 'custom:proposer:private-gateway:archived-model',
      provider: 'private-gateway',
      model: 'archived-model',
      source: 'custom',
      enabled: true,
      role: '',
    }
    const { app, el } = await mountPanel(
      {
        activeStrategy: 'ensemble',
        ensemble: {
          enabled: true,
          scheme: 'custom',
          custom: customLineup({
            proposers: [historical],
            proposerCount: 1,
          }),
        },
      },
      { onReplaceEnsembleCandidate },
    )

    const actions = el.querySelector<HTMLDetailsElement>('.setup-model-strategy__candidate-actions')!
    actions.open = true
    expect(actions.querySelector<HTMLButtonElement>(
      '[data-testid="ensemble-promote-aggregator"]',
    )?.disabled).toBe(true)
    actions.querySelector<HTMLButtonElement>('[data-testid="ensemble-replace-proposer"]')?.click()
    await nextTick()

    const provider = el.querySelector<HTMLSelectElement>(
      'select[name="setup_model_strategy_replace_candidate_provider"]',
    )!
    const historicalOption = Array.from(provider.options).find(option => (
      option.value === 'private-gateway'
    ))
    expect(provider.value).toBe('private-gateway')
    expect(historicalOption?.disabled).toBe(true)
    expect(historicalOption?.textContent).toBe('private-gateway (not configured)')

    const model = el.querySelector<HTMLInputElement>(
      'input[name="setup_provider_ensemble_candidate_replacement"]',
    )!
    model.value = 'another-archived-model'
    model.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()

    const confirm = el.querySelector<HTMLButtonElement>(
      '[data-testid="ensemble-replace-proposer-confirm"]',
    )!
    expect(confirm.disabled).toBe(true)
    confirm.click()
    expect(onReplaceEnsembleCandidate).not.toHaveBeenCalled()
    app.unmount()
  })

  it('keeps add, replace, and aggregator editors mutually exclusive', async () => {
    const proposer = {
      key: 'custom:proposer:openrouter:anthropic/claude-sonnet',
      provider: 'openrouter',
      model: 'anthropic/claude-sonnet',
      source: 'custom',
      enabled: true,
      role: '',
    }
    const { app, el } = await mountPanel({
      activeStrategy: 'ensemble',
      ensemble: {
        enabled: true,
        scheme: 'custom',
        custom: customLineup({ proposers: [proposer], proposerCount: 1 }),
      },
    })

    el.querySelector<HTMLButtonElement>(
      '[data-testid="setup-model-strategy-add-candidate-trigger"]',
    )?.click()
    await nextTick()
    expect(el.querySelector('[data-testid="ensemble-add-proposer-editor"]')).toBeTruthy()

    const actions = el.querySelector<HTMLDetailsElement>('.setup-model-strategy__candidate-actions')!
    actions.open = true
    actions.querySelector<HTMLButtonElement>('[data-testid="ensemble-replace-proposer"]')?.click()
    await nextTick()
    expect(el.querySelector('[data-testid="ensemble-add-proposer-editor"]')).toBeNull()
    expect(el.querySelector('[data-testid="ensemble-replace-proposer-editor"]')).toBeTruthy()

    el.querySelector<HTMLButtonElement>('[data-testid="ensemble-replace-aggregator"]')?.click()
    await nextTick()
    expect(el.querySelector('[data-testid="ensemble-replace-proposer-editor"]')).toBeNull()
    expect(el.querySelector('[data-testid="ensemble-aggregator-picker"]')).toBeTruthy()

    el.querySelector<HTMLButtonElement>(
      '[data-testid="setup-model-strategy-add-candidate-trigger"]',
    )?.click()
    await nextTick()
    expect(el.querySelector('[data-testid="ensemble-aggregator-picker"]')).toBeNull()
    expect(el.querySelector('[data-testid="ensemble-add-proposer-editor"]')).toBeTruthy()

    app.unmount()
  })

  it('replaces the aggregator from a proposer or another model', async () => {
    const onSetEnsembleAggregator = vi.fn()
    const proposer = {
      key: 'custom:proposer:openrouter:anthropic/claude-sonnet',
      provider: 'openrouter',
      model: 'anthropic/claude-sonnet',
      source: 'custom',
      enabled: true,
      role: '',
      credential: { provider: 'openrouter', available: true, source: 'env', envKey: 'OPENROUTER_API_KEY' },
    }
    const aggregator = {
      key: 'custom:aggregator:openrouter:deepseek/deepseek-v4-pro',
      provider: 'openrouter',
      model: 'deepseek/deepseek-v4-pro',
      source: 'custom',
      enabled: true,
      role: 'aggregator',
      credential: { provider: 'openrouter', available: true, source: 'env', envKey: 'OPENROUTER_API_KEY' },
    }
    const { app, el } = await mountPanel(
      {
        activeStrategy: 'ensemble',
        ensemble: {
          enabled: true,
          scheme: 'custom',
          custom: customLineup({
            aggregator,
            aggregatorInherited: false,
            proposers: [proposer],
            proposerCount: 1,
          }),
        },
      },
      { onSetEnsembleAggregator },
    )

    const replace = el.querySelector<HTMLButtonElement>('[data-testid="ensemble-replace-aggregator"]')!
    replace.click()
    await nextTick()
    const picker = el.querySelector<HTMLElement>('[data-testid="ensemble-aggregator-picker"]')!
    expect(picker).toBeTruthy()
    picker.querySelector<HTMLButtonElement>('[data-testid="ensemble-aggregator-option"]')?.click()
    await nextTick()
    expect(onSetEnsembleAggregator).toHaveBeenCalledWith(proposer.provider, proposer.model)

    replace.click()
    await nextTick()
    const model = el.querySelector<HTMLInputElement>(
      'input[name="setup_provider_ensemble_aggregator_model"]',
    )!
    model.value = 'google/gemini-next'
    model.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()
    el.querySelector<HTMLButtonElement>('[data-testid="setup-model-strategy-set-aggregator"]')?.click()
    await nextTick()
    expect(onSetEnsembleAggregator).toHaveBeenLastCalledWith(
      'openrouter',
      'google/gemini-next',
    )

    app.unmount()
  })

  it('allows the same model id to become aggregator when the provider changes', async () => {
    const onSetEnsembleAggregator = vi.fn()
    const aggregator = {
      key: 'custom:aggregator:openrouter:shared-model',
      provider: 'openrouter',
      model: 'shared-model',
      source: 'custom',
      enabled: true,
      role: 'aggregator',
    }
    const { app, el } = await mountPanel(
      {
        activeStrategy: 'ensemble',
        ensemble: {
          enabled: true,
          activeProvider: 'tokenrhythm',
          scheme: 'custom',
          custom: customLineup({
            aggregator,
            aggregatorInherited: false,
            inheritedAggregatorProvider: 'tokenrhythm',
          }),
        },
      },
      { onSetEnsembleAggregator },
    )

    el.querySelector<HTMLButtonElement>('[data-testid="ensemble-replace-aggregator"]')?.click()
    await nextTick()
    const provider = el.querySelector<HTMLSelectElement>(
      'select[name="setup_model_strategy_aggregator_provider"]',
    )!
    provider.value = 'tokenrhythm'
    provider.dispatchEvent(new Event('change', { bubbles: true }))
    await nextTick()
    const model = el.querySelector<HTMLInputElement>(
      'input[name="setup_provider_ensemble_aggregator_model"]',
    )!
    model.value = 'shared-model'
    model.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()

    const submit = el.querySelector<HTMLButtonElement>(
      '[data-testid="setup-model-strategy-set-aggregator"]',
    )!
    expect(submit.disabled).toBe(false)
    submit.click()
    await nextTick()
    expect(onSetEnsembleAggregator).toHaveBeenCalledWith('tokenrhythm', 'shared-model')

    app.unmount()
  })

  it('shows and emits the configured threshold and failure policy', async () => {
    const onUpdateEnsembleMinSuccessful = vi.fn()
    const onUpdateEnsembleAllFailedPolicy = vi.fn()
    const onUpdateEnsembleProposerMaxRetries = vi.fn()
    const { app, el } = await mountPanel({
      activeStrategy: 'ensemble',
      ensemble: { enabled: true, scheme: 'custom' },
    }, {
      onUpdateEnsembleMinSuccessful,
      onUpdateEnsembleAllFailedPolicy,
      onUpdateEnsembleProposerMaxRetries,
    })

    const runtime = el.querySelector<HTMLDetailsElement>('[data-testid="ensemble-runtime-strategy"]')!
    expect(runtime.open).toBe(false)
    runtime.querySelector('summary')?.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await nextTick()
    expect(runtime.open).toBe(true)
    expect(runtime.textContent).toContain(
      'aggregator 480s idle between provider events (configured 3600s)',
    )
    expect(runtime.textContent).toContain('outer turn deadline separate')
    expect(runtime.textContent).toContain('At least 1 of 2 return normally')
    expect(runtime.textContent).toContain('Other proposers still wait for their own completion or timeout')
    const threshold = el.querySelector<HTMLSelectElement>('select[name="setup_model_strategy_min_successful"]')!
    expect(threshold.value).toBe('1')
    expect(threshold.textContent).toContain('At least 1 of 2 return normally')
    threshold.value = '2'
    threshold.dispatchEvent(new Event('change', { bubbles: true }))
    expect(onUpdateEnsembleMinSuccessful).toHaveBeenCalledWith(2)

    const policy = el.querySelector<HTMLSelectElement>('select[name="setup_model_strategy_all_failed_policy"]')!
    expect(policy.value).toBe('fallback_single')
    expect(policy.closest('label')?.textContent)
      .toContain('Fixed and fallback model: OpenRouter · deepseek/deepseek-v4-pro')
    policy.value = 'error'
    policy.dispatchEvent(new Event('change', { bubbles: true }))
    expect(onUpdateEnsembleAllFailedPolicy).toHaveBeenCalledWith('error')
    const retries = el.querySelector<HTMLSelectElement>(
      'select[name="setup_model_strategy_proposer_max_retries"]',
    )!
    expect(retries.value).toBe('0')
    expect(retries.textContent).toContain('At most 0 retries after the initial request')
    retries.value = '2'
    retries.dispatchEvent(new Event('change', { bubbles: true }))
    expect(onUpdateEnsembleProposerMaxRetries).toHaveBeenCalledWith(2)

    app.unmount()
  })

  it('displays an explicit error policy without fallback copy', async () => {
    const { app, el } = await mountPanel({
      activeStrategy: 'ensemble',
      ensemble: {
        enabled: true,
        scheme: 'custom',
        allFailedPolicy: 'error',
        custom: customLineup({ facts: { ...FACTS, proposerMaxRetries: 2 } }),
      },
    })

    const policy = el.querySelector<HTMLSelectElement>('select[name="setup_model_strategy_all_failed_policy"]')!
    expect(policy.value).toBe('error')
    const copy = policy.closest('label')?.textContent || ''
    expect(copy).toContain('without calling the fixed fallback model')
    expect(copy).not.toContain('Fixed and fallback model: OpenRouter')
    expect(el.querySelector<HTMLSelectElement>(
      'select[name="setup_model_strategy_proposer_max_retries"]',
    )?.value).toBe('2')
    app.unmount()
  })

  it('renders an explicit 4-of-4, error, and two-retry policy unchanged', async () => {
    const { app, el } = await mountPanel({
      activeStrategy: 'ensemble',
      ensemble: {
        enabled: true,
        scheme: 'preset',
        minSuccessfulProposers: 4,
        allFailedPolicy: 'error',
        presetFacts: {
          ...panel().ensemble.presetFacts,
          proposerMaxRetries: 2,
        },
      },
    })

    const threshold = el.querySelector<HTMLSelectElement>(
      'select[name="setup_model_strategy_min_successful"]',
    )!
    expect(threshold.value).toBe('4')
    expect(threshold.selectedOptions[0]?.textContent).toContain('At least 4 of 4 return normally')
    expect(el.querySelector<HTMLSelectElement>(
      'select[name="setup_model_strategy_all_failed_policy"]',
    )?.value).toBe('error')
    expect(el.querySelector<HTMLSelectElement>(
      'select[name="setup_model_strategy_proposer_max_retries"]',
    )?.value).toBe('2')
    app.unmount()
  })

  it('clamps an oversized stored threshold to the effective proposer count', async () => {
    const { app, el } = await mountPanel({
      activeStrategy: 'ensemble',
      ensemble: {
        enabled: true,
        scheme: 'custom',
        minSuccessfulProposers: 5,
        custom: customLineup({
          proposerCount: 2,
          facts: { ...FACTS, proposerCount: 2 },
        }),
      },
    })

    const threshold = el.querySelector<HTMLSelectElement>('select[name="setup_model_strategy_min_successful"]')!
    expect(threshold.value).toBe('2')
    expect(threshold.textContent).toContain('At least 2 of 2 return normally')

    app.unmount()
  })

  it('surfaces capacity warnings and disables adding at the proposer cap', async () => {
    const { app, el } = await mountPanel({
      activeStrategy: 'ensemble',
      ensemble: {
        enabled: true,
        scheme: 'custom',
        custom: customLineup({
          proposerCount: 6,
          capacity: 'full',
          canAddProposer: false,
          belowMinimum: false,
          diversityWarning: true,
          facts: { ...FACTS, perTurnCalls: 7, proposerCount: 6 },
        }),
      },
    })

    expect(el.querySelector('[data-testid="ensemble-capacity-full"]')?.textContent).toContain('6')
    expect(el.querySelector('[data-testid="ensemble-diversity-warn"]')).toBeTruthy()
    expect(el.querySelector<HTMLButtonElement>('[data-testid="setup-model-strategy-add-candidate-trigger"]')?.disabled).toBe(true)
    expect(el.querySelector('[data-testid="ensemble-add-proposer-editor"]')).toBeNull()

    app.unmount()
  })

  it('keeps a saved preset until the user explicitly chooses the custom editor', async () => {
    const onUpdateEnsembleScheme = vi.fn()
    const { app, el } = await mountPanel({
      activeStrategy: 'ensemble',
      ensemble: {
        enabled: true,
        selectionMode: 'static_openrouter_b5',
        scheme: 'preset',
        schemeCardsAvailable: true,
        fixedProfile: {
          providerLabel: 'OpenRouter',
          proposers: [
            { key: 'openrouter-fixed:proposer:openrouter:deepseek/deepseek-v4-pro', provider: 'openrouter', model: 'deepseek/deepseek-v4-pro', source: 'openrouter_fixed', enabled: true, role: '' },
            { key: 'openrouter-fixed:proposer:openrouter:z-ai/glm-5.2', provider: 'openrouter', model: 'z-ai/glm-5.2', source: 'openrouter_fixed', enabled: true, role: '' },
            { key: 'openrouter-fixed:proposer:openrouter:moonshotai/kimi-k2.7-code', provider: 'openrouter', model: 'moonshotai/kimi-k2.7-code', source: 'openrouter_fixed', enabled: true, role: '' },
            { key: 'openrouter-fixed:proposer:openrouter:qwen/qwen3.7-max', provider: 'openrouter', model: 'qwen/qwen3.7-max', source: 'openrouter_fixed', enabled: true, role: '' },
          ],
          aggregator: { key: 'openrouter-fixed:aggregator:openrouter:z-ai/glm-5.2', provider: 'openrouter', model: 'z-ai/glm-5.2', source: 'openrouter_fixed', enabled: true, role: 'aggregator' },
        },
        showCandidateEditor: false,
      },
    }, { onUpdateEnsembleScheme })

    expect(el.textContent).toContain('deepseek/deepseek-v4-pro')
    expect(el.textContent).toContain('moonshotai/kimi-k2.7-code')
    expect(el.textContent).toContain('Aggregator')
    expect(el.querySelector('.setup-model-strategy__ensemble > .control-section__head')).toBeNull()
    expect(el.textContent).not.toContain('Models draft in parallel')
    expect(el.querySelector('[data-testid="ensemble-preset-provider-mismatch"]')).toBeNull()
    const preset = el.querySelector<HTMLElement>('[data-testid="ensemble-preset-lineup"]')!
    const steps = preset.querySelectorAll<HTMLElement>('.setup-model-strategy__step')
    expect(steps).toHaveLength(2)
    expect(steps[0]?.textContent).toContain('Proposer')
    expect(steps[1]?.textContent).toContain('Aggregator')
    expect(preset.querySelector('.setup-model-strategy__handoff')).toBeNull()
    expect(preset.querySelector('.setup-model-strategy__candidate-actions')).toBeNull()
    expect(preset.querySelector('[data-testid="setup-model-strategy-add-candidate-trigger"]')).toBeNull()
    expect(preset.querySelector('[data-testid="ensemble-replace-aggregator"]')).toBeNull()
    expect(el.querySelector('[data-testid="ensemble-effective-summary"]')?.textContent).toContain('5 model calls')
    const presetScheme = el.querySelector<HTMLButtonElement>('[data-testid="ensemble-scheme-preset"]')!
    const customScheme = el.querySelector<HTMLButtonElement>('[data-testid="ensemble-scheme-custom"]')!
    expect(presetScheme.getAttribute('aria-pressed')).toBe('true')
    expect(customScheme.getAttribute('aria-pressed')).toBe('false')
    expect(onUpdateEnsembleScheme).not.toHaveBeenCalled()
    customScheme.click()
    expect(onUpdateEnsembleScheme).toHaveBeenCalledWith('custom')
    expect(preset.querySelector('.setup-model-strategy__step-role')).toBeNull()
    expect(steps[1]?.querySelector('.setup-model-strategy__step-role')).toBeNull()
    expect(el.textContent).not.toContain('legacy OpenRouter candidate template')
    expect(el.querySelector('.setup-model-strategy__candidate-provider')).toBeNull()

    app.unmount()
  })

  it('flags a stored preset that belongs to a different provider than the active one', async () => {
    const { app, el } = await mountPanel({
      activeStrategy: 'ensemble',
      providerLabel: 'OpenRouter',
      ensemble: {
        enabled: true,
        selectionMode: 'static_tokenrhythm_b5',
        scheme: 'preset',
        schemeCardsAvailable: true,
        presetProviderMismatch: true,
        fixedProfile: {
          providerLabel: 'TokenRhythm',
          proposers: [
            { key: 'openrouter-fixed:proposer:tokenrhythm:deepseek-v4-pro', provider: 'tokenrhythm', model: 'deepseek-v4-pro', source: 'openrouter_fixed', enabled: true, role: '' },
            { key: 'openrouter-fixed:proposer:tokenrhythm:glm-5.2', provider: 'tokenrhythm', model: 'glm-5.2', source: 'openrouter_fixed', enabled: true, role: '' },
          ],
          aggregator: { key: 'openrouter-fixed:aggregator:tokenrhythm:glm-5.2', provider: 'tokenrhythm', model: 'glm-5.2', source: 'openrouter_fixed', enabled: true, role: 'aggregator' },
        },
        showCandidateEditor: false,
      },
    })

    const notice = el.querySelector<HTMLElement>('[data-testid="ensemble-preset-provider-mismatch"]')
    expect(notice).toBeTruthy()
    expect(notice!.textContent).toContain('TokenRhythm')
    expect(notice!.textContent).toContain('OpenRouter')
    // The card itself renders the stored (actually running) lineup.
    const preset = el.querySelector<HTMLElement>('[data-testid="ensemble-preset-lineup"]')!
    expect(preset.textContent).toContain('deepseek-v4-pro')

    app.unmount()
  })

  it('shows the stored legacy lineup read-only beside its migration banner', async () => {
    const onMigrateEnsembleLegacy = vi.fn()
    const sharedProposer = {
      key: 'legacy:proposer:deepseek:shared-model',
      provider: 'deepseek',
      model: 'shared-model',
      source: 'legacy_model_options',
      enabled: true,
      role: '',
    }
    const sharedAggregator = {
      ...sharedProposer,
      key: 'legacy:aggregator:deepseek:shared-model',
      role: 'aggregator',
    }
    const { app, el } = await mountPanel(
      {
        activeStrategy: 'ensemble',
        ensemble: {
          enabled: true,
          selectionMode: 'router_dynamic',
          scheme: 'legacy',
          schemeCardsAvailable: true,
          customCandidates: [
            sharedProposer,
            {
              key: 'legacy:proposer:tokenrhythm:glm-5.2',
              provider: 'tokenrhythm',
              model: 'glm-5.2',
              source: 'legacy_model_options',
              enabled: true,
              role: '',
            },
            sharedAggregator,
          ],
        },
      },
      { onMigrateEnsembleLegacy },
    )

    const banner = el.querySelector('[data-testid="ensemble-legacy-banner"]')
    expect(banner).toBeTruthy()
    expect(banner?.textContent).toContain('follows the saved model tiers')
    expect(banner?.textContent).toContain('Fixed and fallback model: OpenRouter · deepseek/deepseek-v4-pro')
    expect(banner?.textContent).not.toContain('router_dynamic')
    // Legacy dynamic selection has different runtime semantics, so it stays
    // read-only until the user explicitly migrates to a custom lineup.
    expect(el.querySelector('[data-testid="ensemble-scheme-preset"]')).toBeNull()
    expect(el.querySelector('[data-testid="ensemble-custom-lineup"]')).toBeNull()
    const lineup = el.querySelector<HTMLElement>('[data-testid="ensemble-legacy-lineup"]')!
    expect(lineup).toBeTruthy()
    expect(lineup.textContent).toContain('DeepSeek · shared-model')
    expect(lineup.textContent).toContain('TokenRhythm · glm-5.2')
    expect(lineup.querySelectorAll('[role="listitem"]')).toHaveLength(3)
    expect(lineup.querySelector('.setup-model-strategy__candidate-actions')).toBeNull()
    expect(lineup.querySelector('[data-testid="ensemble-replace-aggregator"]')).toBeNull()
    expect(el.querySelector('[data-testid="ensemble-effective-summary"]')).toBeNull()
    expect(el.querySelector('[data-testid="ensemble-runtime-strategy"]')).toBeNull()
    el.querySelector<HTMLButtonElement>('[data-testid="ensemble-migrate-legacy"]')?.click()
    await nextTick()
    expect(onMigrateEnsembleLegacy).toHaveBeenCalledOnce()

    app.unmount()
  })

  it('hides scheme cards for providers without a preset', async () => {
    const { app, el } = await mountPanel({
      providerLabel: 'DeepSeek',
      activeStrategy: 'ensemble',
      ensemble: {
        enabled: true,
        activeProvider: 'deepseek',
        scheme: 'custom',
        schemeCardsAvailable: false,
        custom: customLineup({ inheritedAggregatorProvider: 'deepseek', inheritedAggregatorModel: 'deepseek-v4-pro' }),
      },
    })

    expect(el.querySelector('[data-testid="ensemble-scheme-preset"]')).toBeNull()
    expect(el.textContent).not.toContain('OpenRouter fixed ensemble')
    expect(el.textContent).toContain('Proposers')
    expect(el.querySelector('.setup-model-strategy__candidate-provider')).toBeNull()
    el.querySelector<HTMLButtonElement>('[data-testid="setup-model-strategy-add-candidate-trigger"]')?.click()
    await nextTick()
    const provider = el.querySelector<HTMLSelectElement>('.setup-model-strategy__candidate-provider')
    expect(provider?.value).toBe('deepseek')

    app.unmount()
  })

  it('uses the active provider live catalog for new ensemble candidates', async () => {
    const { app, el } = await mountPanel({
      activeStrategy: 'ensemble',
      router: {
        discoveredModelsByProvider: {
          openrouter: {
            source: 'live',
            models: [{
              id: 'anthropic/claude-sonnet',
              name: 'Claude Sonnet',
              contextWindow: 200000,
              maxOutputTokens: 8192,
              capabilities: ['chat', 'tools'],
              pricing: null,
              capabilitySource: 'provider',
            }],
          },
        },
      },
      ensemble: {
        enabled: true,
        scheme: 'custom',
      },
    })

    expect(el.querySelector('input[name="setup_provider_ensemble_candidate_model"]')).toBeNull()
    el.querySelector<HTMLButtonElement>('[data-testid="setup-model-strategy-add-candidate-trigger"]')?.click()
    await nextTick()
    const model = el.querySelector<HTMLInputElement>('input[name="setup_provider_ensemble_candidate_model"]')
    expect(model?.getAttribute('role')).toBe('combobox')
    expect(model?.classList.contains('control-input')).toBe(true)
    model?.dispatchEvent(new Event('focus'))
    await nextTick()
    expect(document.body.textContent).toContain('anthropic/claude-sonnet')

    app.unmount()
  })

  it.each(['router', 'single', 'ensemble'] as const)(
    'keeps the fixed and fallback model selector available in %s mode',
    async activeStrategy => {
      const { app, el } = await mountPanel({ activeStrategy })

      expect(el.querySelector('[data-testid="setup-model-strategy-fixed-model"]')).toBeTruthy()
      const fixedModelInput = el.querySelector<HTMLInputElement>(
        'input[name="setup_provider_model_strategy_fixed_model"]',
      )
      expect(fixedModelInput?.value).toBe('deepseek/deepseek-v4-pro')
      expect(el.querySelector('[data-testid="setup-model-strategy-fixed-model"]')
        ?.classList.contains('setup-model-strategy__fixed-model-row')).toBe(true)
      const fixedSection = el.querySelector<HTMLElement>(
        '[data-testid="setup-model-strategy-fixed-section"]',
      )!
      const fixedFieldDescription = fixedSection.querySelector<HTMLElement>(
        '#setup-provider-model_strategy_fixed_model-description',
      )
      expect(fixedFieldDescription?.textContent)
        .toContain('as the fallback if routing or ensemble execution cannot complete')
      expect(fixedFieldDescription?.classList.contains('setup-model-combobox__sr-only')).toBe(true)
      const fixedModelInfo = fixedSection.querySelector<HTMLElement>(
        '.setup-model-combobox__info',
      )
      expect(fixedModelInfo?.getAttribute('title')).toBeNull()
      expect(fixedModelInfo?.getAttribute('aria-describedby'))
        .toBe('setup-provider-model_strategy_fixed_model-info-tooltip')
      expect(fixedModelInfo?.querySelector('[role="tooltip"]')?.textContent)
        .toContain('as the fallback if routing or ensemble execution cannot complete')
      expect(fixedModelInput?.getAttribute('aria-describedby'))
        .toBe('setup-provider-model_strategy_fixed_model-description')
      if (activeStrategy === 'single') {
        expect(fixedSection.querySelector('h4')?.textContent).toContain('Fixed model')
        expect(fixedSection.querySelector('.control-section__head .control-section__desc')?.textContent)
          .toContain('Choose the model used for every request.')
        expect(fixedSection.textContent)
          .toContain('without automatic routing or model ensemble')
      } else {
        expect(fixedSection.querySelector('.control-section__head')).toBeNull()
        expect(fixedSection.querySelector('.control-row__desc')).toBeNull()
        expect(fixedSection.textContent).not.toContain('Choose the model used for every request.')
        expect(fixedSection.textContent)
          .not.toContain('without automatic routing or model ensemble')
      }

      app.unmount()
    },
  )

  it('uses one page heading followed by section and subsection headings', async () => {
    const { app, el } = await mountPanel({ activeStrategy: 'router' })

    expect(Array.from(el.querySelectorAll('h3')).map(node => node.textContent?.trim()))
      .toEqual(['Model routing'])
    expect(Array.from(el.querySelectorAll('h4')).map(node => node.textContent?.trim()))
      .toEqual(expect.arrayContaining([
        'Intelligent model routing',
      ]))
    expect(Array.from(el.querySelectorAll('h4')).map(node => node.textContent?.trim()))
      .not.toContain('Fixed and fallback model')
    // The redundant "Choose how models are used" section heading was removed;
    // the mode radiogroup is labelled for AT via aria-label instead.
    expect(el.textContent).not.toContain('Choose how models are used')
    expect(el.querySelector('.setup-model-strategy__cards')?.getAttribute('aria-label'))
      .toBe('Choose how models are used')
    expect(el.querySelector('.setup-model-strategy__roles-head h5')?.textContent)
      .toContain('Model roles')

    app.unmount()
  })

  it('shows non-empty single model details', async () => {
    const { app, el } = await mountPanel({
      activeStrategy: 'single',
      cards: [
        { id: 'router', enabled: false, titleKey: 'setup.modelStrategy.cards.router.title', descKey: 'setup.modelStrategy.cards.router.desc' },
        { id: 'single', enabled: true, titleKey: 'setup.modelStrategy.cards.single.title', descKey: 'setup.modelStrategy.cards.single.desc' },
        { id: 'ensemble', enabled: false, titleKey: 'setup.modelStrategy.cards.ensemble.title', descKey: 'setup.modelStrategy.cards.ensemble.desc' },
      ],
      ensemble: {
        enabled: false,
        selectionMode: 'router_dynamic',
        modelOptions: [],
        minSuccessfulProposers: 1,
        allFailedPolicy: 'fallback_single',
        showModelOptions: true,
        showOpenrouterHint: false,
        advancedOpen: false,
        statusText: 'Ensemble is off.',
      },
    })

    expect(el.textContent).toContain('Fixed model')
    expect(el.textContent).toContain('Choose the model used for every request.')
    expect(el.textContent).toContain('Fixed and fallback model')
    expect(el.textContent).toContain('without automatic routing or model ensemble')
    expect(el.querySelector('[data-testid="setup-model-strategy-fixed-model"]')).toBeTruthy()
    expect(el.textContent).not.toContain('When routing is uncertain')
    expect(el.querySelector('[role="table"]')).toBeNull()

    app.unmount()
  })

  it('forwards default tier changes from router controls', async () => {
    const onUpdateRouterDefaultTier = vi.fn()
    const { app, el } = await mountPanel(
      { activeStrategy: 'router' },
      { onUpdateRouterDefaultTier },
    )

    const select = el.querySelector<HTMLSelectElement>('select[name="setup_model_strategy_router_default_tier"]')
    expect(select).toBeTruthy()
    select!.value = 'c0'
    select!.dispatchEvent(new Event('change', { bubbles: true }))
    await nextTick()

    expect(onUpdateRouterDefaultTier).toHaveBeenCalledWith('c0')
    app.unmount()
  })

  it('does not expose banned technical terms in title attributes', async () => {
    const { app, el } = await mountPanel()

    const titles = Array.from(el.querySelectorAll('[title]')).map(node => node.getAttribute('title') || '').join('\n')
    expect(titles).not.toMatch(/openrouter-mix|router_dynamic|static_openrouter_b5|tier_profile|Recommended|Default/)

    app.unmount()
  })

  it('shows cross-provider notice when model tiers use mixed providers', async () => {
    const { app, el } = await mountPanel({
      router: {
        hasMixedTierProviders: true,
      },
    })

    expect(el.textContent).toContain('Cross-provider routing')

    app.unmount()
  })
})
