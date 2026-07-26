// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick } from 'vue'
import i18n from '@/i18n'
import SetupModelStrategyPanel from './SetupModelStrategyPanel.vue'

const FACTS = {
  perTurnCalls: 3,
  quorum: 1,
  proposerCount: 2,
  proposerTimeoutSeconds: 300,
  aggregatorTimeoutSeconds: 480,
  quorumGraceSeconds: 10,
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

function panel(overrides: Record<string, unknown> = {}) {
  const base = {
    activeStrategy: 'router',
    hasSavedProvider: true,
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
        quorum: 3,
        proposerCount: 4,
        proposerTimeoutSeconds: 300,
        aggregatorTimeoutSeconds: 480,
        quorumGraceSeconds: 10,
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
    expect(el.textContent).toContain('Multi-model collaboration')
    expect(el.querySelector('[role="radiogroup"]')).toBeTruthy()
    const choices = Array.from(el.querySelectorAll<HTMLInputElement>('input[type="radio"][name="setup_model_strategy"]'))
    expect(choices).toHaveLength(3)
    expect(choices.map(choice => choice.value)).toEqual(['router', 'single', 'ensemble'])
    expect(choices[0]?.checked).toBe(true)
    const strategyRowsText = el.querySelector('[role="radiogroup"]')?.textContent || ''
    expect(strategyRowsText).toContain('Token-efficient')
    expect(strategyRowsText).toContain('Predictable')
    expect(strategyRowsText).toContain('Capability-first')
    expect(strategyRowsText).not.toContain('Recommended')
    expect(strategyRowsText).not.toContain('Advanced')
    expect(strategyRowsText).not.toContain('Default')
    expect(strategyRowsText).not.toContain('Model ensemble')
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

    expect(el.textContent).toContain('When routing is uncertain')
    expect(el.textContent).toContain('Model roles')
    expect(el.textContent).toContain('Choose models for each request level. One provider can supply every level.')
    expect(el.textContent).not.toContain('Preset and credentials from OpenRouter')
    expect(el.querySelector('[role="table"]')).toBeTruthy()
    // The chat-panel visualization picker rides with the router details; losing
    // it strands a saved legacy_grid choice with no UI path back.
    const visualMode = el.querySelector<HTMLSelectElement>('select[name="setup_model_strategy_router_visual_mode"]')
    expect(visualMode?.value).toBe('real_candidates')
    const advanced = visualMode?.closest<HTMLDetailsElement>('details')
    expect(advanced?.open).toBe(false)
    expect(advanced?.querySelector('summary')?.textContent).toContain('Display options')
    advanced?.querySelector<HTMLElement>('summary')?.click()
    await nextTick()
    expect(advanced?.open).toBe(true)
    expect(el.textContent).toContain('Routing decision panel style')

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

  it('emits the routing panel style from the visual-mode select', async () => {
    const onUpdateRouterVisualMode = vi.fn()
    const { app, el } = await mountPanel(
      {
        router: {
          routerVisualModeOptions: [
            { value: 'real_candidates', label: 'Real routing candidates' },
            { value: 'legacy_grid', label: 'Three-tier visual panel' },
          ],
        },
      },
      { onUpdateRouterVisualMode },
    )

    const select = el.querySelector<HTMLSelectElement>('select[name="setup_model_strategy_router_visual_mode"]')
    expect(select).toBeTruthy()
    select!.value = 'legacy_grid'
    select!.dispatchEvent(new Event('change', { bubbles: true }))
    await nextTick()

    expect(onUpdateRouterVisualMode).toHaveBeenCalledWith('legacy_grid')
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
    }, { onUpdateFixedModel })

    const detail = el.querySelector('.setup-model-strategy__detail')?.textContent || ''
    const input = el.querySelector<HTMLInputElement>('input[name="setup_provider_model_strategy_fixed_model"]')
    expect(detail).toContain('Current model provider')
    expect(detail).toContain('OpenRouter')
    expect(input?.value).toBe(discoveredModel.id)
    expect(detail).not.toContain('default tier')

    if (input) {
      input.value = 'deepseek/deepseek-v4-pro'
      input.dispatchEvent(new Event('input', { bubbles: true }))
      await nextTick()
    }
    expect(onUpdateFixedModel).toHaveBeenCalledWith('deepseek/deepseek-v4-pro')

    app.unmount()
  })

  it('adds and imports proposers without assigning an advisory role', async () => {
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
    expect(onAddEnsembleCandidate).toHaveBeenCalledWith('deepseek', 'claude-opus', '')

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

  it('updates the success threshold and failure policy from runtime strategy', async () => {
    const onUpdateEnsembleMinSuccessful = vi.fn()
    const onUpdateEnsembleAllFailedPolicy = vi.fn()
    const { app, el } = await mountPanel(
      {
        activeStrategy: 'ensemble',
        ensemble: { enabled: true, scheme: 'custom' },
      },
      { onUpdateEnsembleMinSuccessful, onUpdateEnsembleAllFailedPolicy },
    )

    const runtime = el.querySelector<HTMLDetailsElement>('[data-testid="ensemble-runtime-strategy"]')!
    expect(runtime.open).toBe(false)
    runtime.querySelector('summary')?.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await nextTick()
    expect(runtime.open).toBe(true)

    const threshold = el.querySelector<HTMLSelectElement>('select[name="setup_model_strategy_min_successful"]')!
    threshold.value = '2'
    threshold.dispatchEvent(new Event('change', { bubbles: true }))
    await nextTick()
    expect(onUpdateEnsembleMinSuccessful).toHaveBeenCalledWith(2)

    const failure = el.querySelector<HTMLSelectElement>('select[name="setup_model_strategy_all_failed_policy"]')
    failure!.value = 'error'
    failure!.dispatchEvent(new Event('change', { bubbles: true }))
    await nextTick()
    expect(onUpdateEnsembleAllFailedPolicy).toHaveBeenCalledWith('error')

    app.unmount()
  })

  it('clamps an oversized stored threshold to the displayed proposer count', async () => {
    const { app, el } = await mountPanel({
      activeStrategy: 'ensemble',
      ensemble: {
        enabled: true,
        scheme: 'custom',
        minSuccessfulProposers: 5,
        custom: customLineup({
          proposerCount: 2,
          facts: { ...FACTS, proposerCount: 2, quorum: 2 },
        }),
      },
    })

    const threshold = el.querySelector<HTMLSelectElement>(
      'select[name="setup_model_strategy_min_successful"]',
    )!
    expect(threshold.value).toBe('2')
    expect(threshold.selectedOptions[0]?.textContent).toContain('2 of 2')

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
          facts: { ...FACTS, perTurnCalls: 7, proposerCount: 6, quorum: 5 },
        }),
      },
    })

    expect(el.querySelector('[data-testid="ensemble-capacity-full"]')?.textContent).toContain('6')
    expect(el.querySelector('[data-testid="ensemble-diversity-warn"]')).toBeTruthy()
    expect(el.querySelector<HTMLButtonElement>('[data-testid="setup-model-strategy-add-candidate-trigger"]')?.disabled).toBe(true)
    expect(el.querySelector('[data-testid="ensemble-add-proposer-editor"]')).toBeNull()

    app.unmount()
  })

  it('keeps the preset lineup read-only while allowing a switch to custom', async () => {
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
    expect(el.querySelector('[data-testid="ensemble-scheme-preset"]')?.getAttribute('aria-checked')).toBe('true')
    el.querySelector<HTMLButtonElement>('[data-testid="ensemble-scheme-custom"]')?.click()
    await nextTick()
    expect(onUpdateEnsembleScheme).toHaveBeenCalledWith('custom')
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
      const fixedSection = el.querySelector<HTMLElement>(
        '[data-testid="setup-model-strategy-fixed-section"]',
      )!
      const fixedFieldDescription = fixedSection.querySelector<HTMLElement>(
        '#setup-provider-model_strategy_fixed_model-description',
      )
      expect(fixedFieldDescription?.textContent)
        .toContain('as the fallback when routing or collaboration cannot complete')
      expect(fixedModelInput?.getAttribute('aria-describedby'))
        .toBe('setup-provider-model_strategy_fixed_model-description')
      if (activeStrategy === 'single') {
        expect(fixedSection.querySelector('h4')?.textContent).toContain('Fixed model')
        expect(fixedSection.querySelector('.control-section__head .control-section__desc')?.textContent)
          .toContain('Choose the model used for every request.')
        expect(fixedSection.textContent)
          .toContain('without automatic routing or multi-model collaboration')
      } else {
        expect(fixedSection.querySelector('h4')?.textContent)
          .toContain('Fixed and fallback model')
        expect(fixedSection.textContent)
          .toContain('as the fallback when routing or collaboration cannot complete')
        expect(fixedSection.querySelector('.control-section__head .control-section__desc')).toBeNull()
        expect(fixedSection.textContent).not.toContain('Choose the model used for every request.')
        expect(fixedSection.textContent)
          .not.toContain('without automatic routing or multi-model collaboration')
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
        'Choose how models are used',
        'Intelligent model routing',
        'Fixed and fallback model',
      ]))
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
    expect(el.textContent).toContain('without automatic routing or multi-model collaboration')
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
