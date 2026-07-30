import { describe, it, expect } from 'vitest'
import { computed } from 'vue'
import { useSetupRouterForm } from './useSetupRouterForm'

// openrouter-mix is backend-supported but was unreachable in the WebUI. The
// round-trip is subtle: it is the only enabled mode whose tier_profile is null,
// and only valid for the openrouter provider — a default config for another
// provider must NOT be mistaken for it.

function makePanel(form: ReturnType<typeof useSetupRouterForm>, isOpenrouter: boolean, ensembleProfileActive = false) {
  return form.createPanel({
    routerSummary: computed(() => ''),
    ensembleProfileActive: computed(() => ensembleProfileActive),
    hasSavedProvider: computed(() => true),
    isOpenrouter: computed(() => isOpenrouter),
    textTiers: ['c0'],
    tierLabel: (t) => t,
  })
}

describe('useSetupRouterForm — openrouter-mix round-trip', () => {
  it('classifies legacy openrouter mix internally but saves canonical custom mode', () => {
    const f = useSetupRouterForm()
    f.initFromConfig({ enabled: true, tier_profile: null }, {}, 'openrouter')
    expect(f.mode.value).toBe('openrouter-mix')
    expect(f.payload().mode).toBe('custom')
  })

  it('classifies an explicit follow-primary binding as recommended', () => {
    const f = useSetupRouterForm()
    f.initFromConfig({ enabled: true, tier_profile: 'openrouter' }, {}, 'openrouter', 'follow_primary')
    expect(f.mode.value).toBe('recommended')
  })

  it('uses the active provider preset instead of materialized defaults for follow-primary', () => {
    const f = useSetupRouterForm()
    f.initFromConfig(
      {
        enabled: false,
        tiers: {
          c0: { provider: 'openrouter', model: 'materialized-default' },
        },
      },
      {
        c0: { provider: 'deepseek', model: 'deepseek-chat' },
      },
      'deepseek',
      'follow_primary',
    )

    f.enableFromSavedBinding()

    expect(f.mode.value).toBe('recommended')
    expect(f.payload()).toMatchObject({
      mode: 'recommended',
      tiers: {
        c0: { provider: 'deepseek', model: 'deepseek-chat' },
      },
    })
  })

  it('preserves explicit historical tiers when re-enabling a legacy router', () => {
    const f = useSetupRouterForm()
    f.initFromConfig(
      {
        enabled: false,
        tiers: {
          c0: { provider: 'openrouter', model: 'legacy-model' },
        },
      },
      {
        c0: { provider: 'deepseek', model: 'deepseek-chat' },
      },
      'deepseek',
      'legacy',
    )

    f.enableFromSavedBinding()

    expect(f.mode.value).toBe('custom')
    expect(f.payload()).toMatchObject({
      mode: 'custom',
      tiers: {
        c0: { provider: 'openrouter', model: 'legacy-model' },
      },
    })
  })

  it('round-trips a legacy non-OpenRouter ladder conservatively as custom', () => {
    const f = useSetupRouterForm()
    f.initFromConfig({ enabled: true, tier_profile: null }, {}, 'openai', 'legacy')
    expect(f.mode.value).toBe('custom')
    expect(f.payload().mode).toBe('custom')
  })

  it('keeps an explicit custom binding custom even when its shape resembles a preset', () => {
    const f = useSetupRouterForm()
    f.initFromConfig(
      { enabled: true, tier_profile: 'openai' },
      {},
      'openai',
      'custom',
    )
    expect(f.mode.value).toBe('custom')
    expect(f.payload().mode).toBe('custom')
  })

  it('classifies a disabled config as disabled regardless of provider', () => {
    const f = useSetupRouterForm()
    f.initFromConfig({ enabled: false }, {}, 'openrouter')
    expect(f.mode.value).toBe('disabled')
  })

  it('does not expose an OpenRouter mix UI option for any provider', () => {
    const f = useSetupRouterForm()
    f.initFromConfig({ enabled: true, tier_profile: 'openai' }, {}, 'openai')
    expect(Object.prototype.hasOwnProperty.call(makePanel(f, false).value, 'canUseOpenrouterMix')).toBe(false)
    expect(Object.prototype.hasOwnProperty.call(makePanel(f, true).value, 'canUseOpenrouterMix')).toBe(false)
  })

  it('passes the LLM ensemble profile state to the router panel', () => {
    const f = useSetupRouterForm()
    f.initFromConfig({ enabled: true, tier_profile: 'openai' }, {}, 'openai')

    expect(makePanel(f, false, true).value.ensembleProfileActive).toBe(true)
  })

  it('keeps tier provider values in the save payload', () => {
    const f = useSetupRouterForm()
    f.initFromConfig({
      enabled: true,
      tier_profile: null,
      tiers: {
        c0: {
          provider: 'openrouter',
          model: 'deepseek/deepseek-v4-flash',
          thinking_level: 'high',
          supports_image: false,
        },
      },
    }, {}, 'openrouter')

    expect(f.payload()).toMatchObject({
      mode: 'custom',
      tiers: {
        c0: {
          provider: 'openrouter',
          model: 'deepseek/deepseek-v4-flash',
          thinkingLevel: 'high',
          supportsImage: false,
        },
      },
    })
  })

  it('keeps openrouter-mix internally while exposing the layered UI choice', () => {
    const f = useSetupRouterForm()
    f.initFromConfig({ enabled: true, tier_profile: null }, {}, 'openrouter')

    const panel = makePanel(f, true)
    expect(panel.value.routerMode).toBe('openrouter-mix')
    expect(panel.value.routerModeChoice).toBe('recommended')
    expect(Object.prototype.hasOwnProperty.call(panel.value, 'canUseOpenrouterMix')).toBe(false)
    expect(panel.value.routerConfigDisabled).toBe(false)
    expect(f.visibleModeChoice.value).toBe('router')
    expect(f.tierTemplateState.value).toBe('custom')
    expect(f.payload().mode).toBe('custom')
  })

  it('coerces a stored openrouter-mix mode back to recommended and marks the form dirty', () => {
    const f = useSetupRouterForm()
    f.initFromConfig({ enabled: true, tier_profile: null }, {}, 'openrouter')
    expect(f.mode.value).toBe('openrouter-mix')

    f.setRouterMode('recommended')
    expect(f.mode.value).toBe('recommended')
    expect(f.routingDirty.value).toBe(true)
  })

  it('maps disabled router config to the single-model UI choice', () => {
    const f = useSetupRouterForm()
    f.initFromConfig({ enabled: false }, {}, 'openrouter')

    const panel = makePanel(f, true)
    expect(panel.value.routerMode).toBe('disabled')
    expect(panel.value.routerModeChoice).toBe('disabled')
    expect(panel.value.routerConfigDisabled).toBe(true)
  })

  it('marks standard router configuration read-only while LLM ensemble routing is active', () => {
    const f = useSetupRouterForm()
    f.initFromConfig(
      { enabled: true, tier_profile: 'openai' },
      {},
      'openai',
      'follow_primary',
    )

    const panel = makePanel(f, false, true)
    expect(panel.value.routerMode).toBe('recommended')
    expect(panel.value.routerModeChoice).toBe('recommended')
    expect(panel.value.ensembleProfileActive).toBe(true)
    expect(panel.value.routerConfigDisabled).toBe(true)
    expect(panel.value.routerConfigDisabledReason).toBe('ensemble')
    expect(f.payload().mode).toBe('recommended')
  })

  it('uses the ensemble disabled reason when ensemble routing is active over single-model settings', () => {
    const f = useSetupRouterForm()
    f.initFromConfig({ enabled: false }, {}, 'openrouter')

    const panel = makePanel(f, true, true)
    expect(panel.value.routerMode).toBe('disabled')
    expect(panel.value.routerModeChoice).toBe('disabled')
    expect(panel.value.routerConfigDisabled).toBe(true)
    expect(panel.value.routerConfigDisabledReason).toBe('ensemble')
  })

  it('uses the single-model disabled reason when model routing is disabled and ensemble routing is inactive', () => {
    const f = useSetupRouterForm()
    f.initFromConfig({ enabled: false }, {}, 'openrouter')

    const panel = makePanel(f, true, false)
    expect(panel.value.routerMode).toBe('disabled')
    expect(panel.value.routerModeChoice).toBe('disabled')
    expect(panel.value.routerConfigDisabled).toBe(true)
    expect(panel.value.routerConfigDisabledReason).toBe('single-model')
  })
})

describe('useSetupRouterForm - model strategy semantics', () => {
  it('keeps openrouter-mix internal and exposes it as custom tier state', () => {
    const f = useSetupRouterForm()
    f.initFromConfig({ enabled: true, tier_profile: null }, {}, 'openrouter')

    expect(f.mode.value).toBe('openrouter-mix')
    expect(f.tierTemplateState.value).toBe('custom')
    expect(f.visibleModeChoice.value).toBe('router')
  })

  it('saves an edited legacy openrouter-mix table as custom mode', () => {
    const f = useSetupRouterForm()
    f.initFromConfig({
      enabled: true,
      tier_profile: null,
      tiers: {
        c0: { provider: 'openrouter', model: 'deepseek/deepseek-v4-flash' },
        c1: { provider: 'openrouter', model: 'deepseek/deepseek-v4-pro' },
        c2: { provider: 'openrouter', model: 'z-ai/glm-5.2' },
        c3: { provider: 'openrouter', model: 'z-ai/glm-5.2' },
      },
    }, {}, 'openrouter')

    f.updateTierField('c3', 'model', 'anthropic/claude-opus-4.8')

    expect(f.payload().mode).toBe('custom')
  })

  it('adds cross-provider router fields when tier providers differ', () => {
    const f = useSetupRouterForm()
    f.initFromConfig({
      enabled: true,
      tier_profile: null,
      tiers: {
        c0: { provider: 'openai', model: 'gpt-5.4-mini' },
        c1: { provider: 'openrouter', model: 'deepseek/deepseek-v4-pro' },
        c2: { provider: 'openrouter', model: 'z-ai/glm-5.2' },
        c3: { provider: 'openai', model: 'gpt-5.5' },
      },
    }, {}, 'openai')

    expect(f.hasMixedTierProviders.value).toBe(true)
    expect(f.tierTemplateState.value).toBe('custom')
    expect(f.payload()).toMatchObject({
      mode: 'custom',
      crossProviderTiers: true,
      tierProviderMismatch: 'veto',
    })
  })

  it('atomically clears a provider-scoped model when the tier provider changes', () => {
    const f = useSetupRouterForm()
    f.initFromConfig({
      enabled: true,
      tier_profile: 'openrouter',
      tiers: {
        c0: { provider: 'openrouter', model: 'deepseek/deepseek-v4-flash' },
      },
    }, {}, 'openrouter')

    f.updateTierField('c0', 'provider', ' DeepSeek ')

    expect(f.payload()).toMatchObject({
      mode: 'custom',
      crossProviderTiers: true,
      tierProviderMismatch: 'veto',
      tiers: {
        c0: { provider: 'deepseek', model: '' },
      },
    })
  })

  it('exposes mixed-provider tier state through createPanel', () => {
    const f = useSetupRouterForm()
    f.initFromConfig({
      enabled: true,
      tier_profile: null,
      tiers: {
        c0: { provider: 'openai', model: 'gpt-5.4-mini' },
        c1: { provider: 'openrouter', model: 'deepseek/deepseek-v4-pro' },
      },
    }, {}, 'openai')

    expect(makePanel(f, false).value.hasMixedTierProviders).toBe(true)
  })
})
