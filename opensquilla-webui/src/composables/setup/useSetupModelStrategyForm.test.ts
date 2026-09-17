import { describe, expect, it } from 'vitest'
import { computed } from 'vue'
import { useSetupRouterForm } from './useSetupRouterForm'
import { useSetupEnsembleForm } from './useSetupEnsembleForm'
import { useSetupModelStrategyForm } from './useSetupModelStrategyForm'

function makeForm(provider = 'openai') {
  const router = useSetupRouterForm()
  const ensemble = useSetupEnsembleForm()
  router.initFromConfig({ enabled: true, tier_profile: provider }, {}, provider)
  ensemble.initFromConfig({ enabled: false })
  const strategy = useSetupModelStrategyForm(
    router,
    ensemble,
    computed(() => provider),
    undefined,
    computed(() => 'gpt-5.4-mini'),
  )
  strategy.initFixedModel()
  return { router, ensemble, strategy }
}

describe('useSetupModelStrategyForm', () => {
  it('derives model router when router is enabled and ensemble is off', () => {
    const { strategy } = makeForm()
    expect(strategy.activeStrategy.value).toBe('router')
  })

  it('derives model ensemble when ensemble is enabled', () => {
    const { ensemble, strategy } = makeForm()
    ensemble.setEnabled(true)
    expect(strategy.activeStrategy.value).toBe('ensemble')
  })

  it('derives model ensemble over single model when ensemble is enabled', () => {
    const { router, ensemble, strategy } = makeForm()
    router.setRouterMode('disabled')
    ensemble.setEnabled(true)

    expect(strategy.activeStrategy.value).toBe('ensemble')
  })

  it('aggregates router and ensemble dirty state', () => {
    const routerDirtyForm = makeForm()
    expect(routerDirtyForm.strategy.isDirty.value).toBe(false)

    routerDirtyForm.router.setRouterMode('disabled')
    expect(routerDirtyForm.strategy.isDirty.value).toBe(true)

    const ensembleDirtyForm = makeForm()
    expect(ensembleDirtyForm.strategy.isDirty.value).toBe(false)

    ensembleDirtyForm.ensemble.setEnabled(true)
    expect(ensembleDirtyForm.strategy.isDirty.value).toBe(true)
  })

  it('marks saved tier-fusion runtime status stale after a local model-strategy edit', () => {
    const router = useSetupRouterForm()
    const ensemble = useSetupEnsembleForm()
    router.initFromConfig({
      enabled: true,
      tiers: {
        c3: { provider: 'openrouter', model: 'quality-model', ensemble_enabled: true },
      },
    }, {}, 'openrouter', 'custom', { c3: 'dormant_draft' }, 'custom_b5', false, {
      selectionMode: 'custom_b5',
      activationTiers: ['c3'],
      runtimeStatus: 'ready',
      configurationReady: true,
      fixedFallbackReady: true,
    })
    ensemble.initFromConfig({ enabled: false, selection_mode: 'custom_b5' })
    const strategy = useSetupModelStrategyForm(
      router,
      ensemble,
      computed(() => 'openrouter'),
      undefined,
      computed(() => 'fallback-model'),
    )
    strategy.initFixedModel()
    const routerPanel = router.createPanel({
      routerSummary: computed(() => ''),
      ensembleProfileActive: computed(() => false),
      hasSavedProvider: computed(() => true),
      isOpenrouter: computed(() => true),
      textTiers: ['c3'],
      tierLabel: tier => tier,
    })
    const ensemblePanel = ensemble.createPanel({
      statusText: computed(() => ''),
      activeProvider: computed(() => 'openrouter'),
      activeModel: computed(() => 'fallback-model'),
    })
    const strategyPanel = strategy.createPanel({
      hasSavedProvider: computed(() => true),
      profileSaveSupported: computed(() => true),
      providerLabel: computed(() => 'OpenRouter'),
      routerPanel,
      ensemblePanel,
      routerTemplateState: computed(() => 'custom'),
      fixedModelCatalog: computed(() => ({ models: [], source: 'none' as const })),
    })

    expect(strategyPanel.value.router.tierEnsembleStatusFresh).toBe(true)
    router.updateTierField('c3', 'model', 'new-quality-model')
    expect(strategyPanel.value.router.tierEnsembleStatusFresh).toBe(false)
  })

  it('tracks the fixed model as part of Model Routing and emits only its config patch', () => {
    const { strategy } = makeForm()

    expect(strategy.fixedModel.value).toBe('gpt-5.4-mini')
    expect(strategy.fixedModelDirty.value).toBe(false)

    strategy.setFixedModel('gpt-5.5')

    expect(strategy.fixedModelDirty.value).toBe(true)
    expect(strategy.isDirty.value).toBe(true)
    expect(strategy.fixedModelPatches()).toEqual({ 'llm.model': 'gpt-5.5' })

    strategy.initFixedModel('gpt-5.5')
    expect(strategy.fixedModelPatches()).toEqual({})
    expect(strategy.fixedModelDirty.value).toBe(false)
  })

  it('tracks a fixed provider draft and leaves provider changes to profile activation', () => {
    const { strategy } = makeForm('openai')

    expect(strategy.fixedProvider.value).toBe('openai')
    expect(strategy.fixedProviderDirty.value).toBe(false)

    strategy.setFixedProvider('DeepSeek')
    strategy.setFixedModel('deepseek-chat')

    expect(strategy.fixedProvider.value).toBe('deepseek')
    expect(strategy.fixedProviderDirty.value).toBe(true)
    expect(strategy.isDirty.value).toBe(true)
    expect(strategy.fixedModelPatches()).toEqual({})

    strategy.initFixedModel('deepseek-chat', 'deepseek')
    expect(strategy.fixedProviderDirty.value).toBe(false)
    expect(strategy.fixedModelDirty.value).toBe(false)
  })

  it('selecting single model disables ensemble and router', () => {
    const { router, ensemble, strategy } = makeForm()
    ensemble.setEnabled(true)

    strategy.setStrategy('single')

    expect(ensemble.enabled.value).toBe(false)
    expect(router.mode.value).toBe('disabled')
    expect(strategy.activeStrategy.value).toBe('single')
  })

  it('selecting model router disables ensemble and enables a custom editable table', () => {
    const { router, ensemble, strategy } = makeForm()
    router.setRouterMode('disabled')
    ensemble.setEnabled(true)

    strategy.setStrategy('router')

    expect(ensemble.enabled.value).toBe(false)
    expect(router.mode.value).toBe('custom')
    expect(strategy.activeStrategy.value).toBe('router')
  })

  it('refreshes provider roles across local ensemble and router strategy transitions', () => {
    const router = useSetupRouterForm()
    const ensemble = useSetupEnsembleForm()
    router.initFromConfig({
      enabled: true,
      tiers: {
        c0: { provider: 'openrouter', model: 'fast-model' },
        c3: { provider: 'tokenrhythm', model: 'quality-model' },
      },
    }, {}, 'openrouter', 'custom', {
      c0: 'direct',
      c3: 'direct',
    }, 'static_openrouter_b5', false)
    ensemble.initFromConfig({ enabled: false, selection_mode: 'static_openrouter_b5' })
    const strategy = useSetupModelStrategyForm(router, ensemble)

    strategy.setStrategy('ensemble')
    expect(router.routerProviderRoles.value).toEqual({
      c0: 'dormant_draft',
      c3: 'dormant_draft',
    })

    strategy.setStrategy('router')
    expect(router.routerProviderRoles.value).toEqual({
      c0: 'direct',
      c3: 'direct',
    })
  })

  it('re-enables a follow-primary router as the managed provider preset', () => {
    const router = useSetupRouterForm()
    const ensemble = useSetupEnsembleForm()
    router.initFromConfig(
      { enabled: false },
      { c0: { provider: 'deepseek', model: 'deepseek-chat' } },
      'deepseek',
      'follow_primary',
    )
    ensemble.initFromConfig({ enabled: false })
    const strategy = useSetupModelStrategyForm(router, ensemble, computed(() => 'deepseek'))

    strategy.setStrategy('router')

    expect(router.mode.value).toBe('recommended')
    expect(router.payload()).toMatchObject({
      mode: 'recommended',
      tiers: { c0: { provider: 'deepseek', model: 'deepseek-chat' } },
    })
  })

  it('selecting model router coerces openrouter mix to a custom editable table', () => {
    const router = useSetupRouterForm()
    const ensemble = useSetupEnsembleForm()
    router.initFromConfig({ enabled: true, tier_profile: null }, {}, 'openrouter')
    ensemble.initFromConfig({ enabled: true })
    const strategy = useSetupModelStrategyForm(router, ensemble)

    expect(router.mode.value).toBe('openrouter-mix')

    strategy.setStrategy('router')

    expect(ensemble.enabled.value).toBe(false)
    expect(router.mode.value).toBe('custom')
    expect(strategy.activeStrategy.value).toBe('router')
  })

  it('selecting model ensemble migrates a hidden legacy plan to an explicit custom lineup', () => {
    const { router, ensemble, strategy } = makeForm()
    ensemble.initFromConfig({ enabled: false, selection_mode: 'router_dynamic' })

    strategy.setStrategy('ensemble')

    expect(router.mode.value).toBe('disabled')
    expect(ensemble.enabled.value).toBe(true)
    // Never the hidden legacy dynamic mode — an explicit custom lineup keeps
    // the edited pool effective at runtime.
    expect(ensemble.selectionMode.value).toBe('custom_b5')
    expect(strategy.activeStrategy.value).toBe('ensemble')
  })

  it('selecting model ensemble seeds a legacy custom lineup from the router tiers', () => {
    const router = useSetupRouterForm()
    const ensemble = useSetupEnsembleForm()
    router.initFromConfig({ enabled: true, tier_profile: 'openai' }, {}, 'openai')
    ensemble.initFromConfig({ enabled: false, selection_mode: 'router_dynamic' })
    const strategy = useSetupModelStrategyForm(
      router,
      ensemble,
      computed(() => 'openai'),
      computed(() => [
        { provider: 'openai', model: 'gpt-5.5', tier: 'c3' },
        { provider: 'openai', model: 'gpt-5.4-mini', tier: 'c0' },
      ]),
    )

    strategy.setStrategy('ensemble')

    expect(ensemble.selectionMode.value).toBe('custom_b5')
    expect(ensemble.candidates.value.map(c => c.model)).toEqual(['gpt-5.5', 'gpt-5.4-mini'])
  })

  it('selecting model ensemble preserves the current OpenRouter preset plan', () => {
    const { router, ensemble, strategy } = makeForm('openrouter')

    strategy.setStrategy('ensemble')

    expect(router.mode.value).toBe('disabled')
    expect(ensemble.enabled.value).toBe(true)
    expect(ensemble.selectionMode.value).toBe('static_openrouter_b5')
    expect(ensemble.candidates.value.length).toBe(0)
  })

  it('selecting model ensemble preserves the current TokenRhythm preset plan', () => {
    const { router, ensemble, strategy } = makeForm('tokenrhythm')
    ensemble.initFromConfig({ enabled: false, selection_mode: 'static_tokenrhythm_b5' })

    strategy.setStrategy('ensemble')

    expect(router.mode.value).toBe('disabled')
    expect(ensemble.enabled.value).toBe(true)
    expect(ensemble.selectionMode.value).toBe('static_tokenrhythm_b5')
    expect(ensemble.candidates.value.length).toBe(0)
  })

  it('builds the routing choices in progressive order with guidance badges', () => {
    const { router, ensemble, strategy } = makeForm()
    const routerPanel = router.createPanel({
      routerSummary: computed(() => ''),
      ensembleProfileActive: computed(() => false),
      hasSavedProvider: computed(() => true),
      isOpenrouter: computed(() => false),
      textTiers: [],
      tierLabel: tier => tier,
    })
    const ensemblePanel = ensemble.createPanel({
      statusText: computed(() => ''),
      activeProvider: computed(() => 'openai'),
    })
    const panel = strategy.createPanel({
      hasSavedProvider: computed(() => true),
      profileSaveSupported: computed(() => true),
      providerLabel: computed(() => 'OpenAI'),
      routerPanel,
      ensemblePanel,
      routerTemplateState: computed(() => 'recommended'),
      fixedModelCatalog: computed(() => ({ models: [], source: 'none' as const })),
    })

    expect(panel.value.cards.map(card => card.id)).toEqual(['ensemble', 'router', 'single'])
    expect(panel.value.cards.map(card => card.badgeKey || '')).toEqual([
      'setup.modelStrategy.cards.ensemble.badge',
      'setup.modelStrategy.cards.router.badge',
      'setup.modelStrategy.cards.single.badge',
    ])
  })
})
