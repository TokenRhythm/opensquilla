import { describe, it, expect } from 'vitest'
import { computed } from 'vue'
import { routerTierProviderParticipates, useSetupRouterForm } from './useSetupRouterForm'

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

  it('round-trips a tier-managed ensemble profile from snake case', () => {
    const f = useSetupRouterForm()
    f.initFromConfig({
      enabled: true,
      tier_profile: 'tokenrhythm',
      tiers: {
        c3: {
          provider: 'tokenrhythm',
          model: 'glm-5.2',
          ensemble_selection_mode: 'static_tokenrhythm_b5',
        },
      },
    }, {}, 'tokenrhythm', 'custom')

    expect(f.payload()).toMatchObject({
      mode: 'custom',
      tiers: {
        c3: {
          provider: 'tokenrhythm',
          model: 'glm-5.2',
          ensembleSelectionMode: 'static_tokenrhythm_b5',
        },
      },
    })
  })

  it('round-trips the shared C3 plan without storing an internal profile', () => {
    const f = useSetupRouterForm()
    f.initFromConfig({
      enabled: true,
      tiers: {
        c3: {
          provider: 'tokenrhythm',
          model: 'glm-5.2',
          ensemble_enabled: true,
        },
      },
    }, {}, 'tokenrhythm', 'follow_primary')

    expect(f.payload()).toMatchObject({
      tiers: {
        c3: {
          ensembleEnabled: true,
          ensembleSelectionMode: '',
        },
      },
    })
  })

  it('does not send the shared ensemble flag for C0 through C2', () => {
    const f = useSetupRouterForm()
    f.initFromConfig({
      enabled: true,
      tiers: {
        c0: {
          provider: 'tokenrhythm',
          model: 'qwen3.7-flash',
          ensemble_enabled: true,
        },
        c3: {
          provider: 'tokenrhythm',
          model: 'glm-5.2',
          ensemble_enabled: true,
        },
      },
    }, {}, 'tokenrhythm', 'custom')

    expect(f.payload()).not.toHaveProperty('tiers.c0.ensembleEnabled')
    expect(f.payload()).toHaveProperty('tiers.c3.ensembleEnabled', true)
  })

  it('sends an explicit false when the user switches C3 back to one model', () => {
    const f = useSetupRouterForm()
    f.initFromConfig({
      enabled: true,
      tiers: {
        c3: {
          provider: 'tokenrhythm',
          model: 'glm-5.2',
          ensemble_enabled: true,
        },
      },
    }, {}, 'tokenrhythm')

    f.updateTierField('c3', 'ensembleEnabled', false)
    f.updateTierField('c3', 'ensembleSelectionMode', '')
    f.updateTierField('c3', 'model', 'deepseek-v4-pro')

    expect(f.payload()).toMatchObject({
      tiers: {
        c3: {
          model: 'deepseek-v4-pro',
          ensembleEnabled: false,
          ensembleSelectionMode: '',
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

  it('ignores a server-owned dormant shared C3 provider in panel state and save policy', () => {
    const f = useSetupRouterForm()
    f.initFromConfig({
      enabled: true,
      tier_profile: null,
      tiers: {
        c0: { provider: 'openrouter', model: 'deepseek/deepseek-v4-flash' },
        c3: {
          provider: 'tokenrhythm',
          model: 'sleeping-single-model-draft',
          ensemble_enabled: true,
        },
      },
    }, {}, 'openrouter', 'legacy', { c3: 'dormant_draft' })

    expect(f.hasMixedTierProviders.value).toBe(false)
    expect(makePanel(f, true).value.hasMixedTierProviders).toBe(false)
    expect(f.payload()).toMatchObject({
      tiers: {
        c3: {
          provider: 'tokenrhythm',
          model: 'sleeping-single-model-draft',
          ensembleEnabled: true,
        },
      },
    })
    expect(f.payload()).not.toHaveProperty('crossProviderTiers')
    expect(f.payload()).not.toHaveProperty('tierProviderMismatch')
  })

  it('treats a shared C3 provider as direct when an older Gateway omits ownership', () => {
    const f = useSetupRouterForm()
    f.initFromConfig({
      enabled: true,
      tiers: {
        c0: { provider: 'openrouter', model: 'fast-model' },
        c3: {
          provider: 'tokenrhythm',
          model: 'saved-c3-draft',
          ensemble_enabled: true,
        },
      },
    }, {}, 'openrouter')

    expect(f.routerProviderRoles.value).toEqual({})
    expect(f.hasMixedTierProviders.value).toBe(true)
    expect(f.payload()).toMatchObject({
      crossProviderTiers: true,
      tierProviderMismatch: 'veto',
    })
    expect(f.tierEnsembleStatus.value).toBeNull()
  })

  it('normalizes additive saved tier-fusion runtime status from the Router card', () => {
    const f = useSetupRouterForm()
    f.initFromConfig({
      enabled: true,
      tiers: {
        c3: {
          provider: 'openrouter',
          model: 'quality-model',
          ensemble_enabled: true,
        },
      },
    }, {}, 'openrouter', 'custom', { c3: 'dynamic_member' }, 'router_dynamic', false, {
      selectionMode: 'router_dynamic',
      activationTiers: ['C3'],
      tierSelectionModes: { C3: 'router_dynamic' },
      runtimeStatus: 'conditional',
      configurationReady: null,
      blockedReason: null,
      blockedTierCandidates: [{ source: 'router_tier:c0', reason: 'cross_provider_veto' }],
      fixedFallbackReady: true,
      fixedFallbackBlockedReason: null,
      proposerCount: 4,
      configuredMinSuccessfulProposers: 1,
      effectiveMinSuccessfulProposers: 1,
      configuredProposerMaxRetries: 0,
      effectiveProposerMaxRetries: 1,
      proposerMaxRetriesSource: 'c3_default',
    })

    expect(f.tierEnsembleStatus.value).toEqual({
      selectionMode: 'router_dynamic',
      activationTiers: ['c3'],
      tierSelectionModes: { c3: 'router_dynamic' },
      runtimeStatus: 'conditional',
      configurationReady: null,
      blockedReason: '',
      blockedTierCandidates: [{ source: 'router_tier:c0', reason: 'cross_provider_veto' }],
      fixedFallbackReady: true,
      fixedFallbackBlockedReason: '',
      proposerCount: 4,
      configuredMinSuccessfulProposers: 1,
      effectiveMinSuccessfulProposers: 1,
      configuredProposerMaxRetries: 0,
      effectiveProposerMaxRetries: 1,
      proposerMaxRetriesSource: 'c3_default',
    })
    expect(makePanel(f, true).value.tierEnsembleStatus?.runtimeStatus).toBe('conditional')
  })

  it('includes a router_dynamic C3 member while accepting richer role objects', () => {
    const f = useSetupRouterForm()
    f.initFromConfig({
      enabled: true,
      tiers: {
        c0: { provider: 'openrouter', model: 'fast-model' },
        c3: {
          provider: 'tokenrhythm',
          model: 'dynamic-member',
          ensemble_enabled: true,
        },
      },
    }, {}, 'openrouter', 'custom', {
      c3: { role: 'dynamic_member' },
    })

    expect(f.routerProviderRoles.value).toEqual({ c3: 'dynamic_member' })
    expect(f.hasMixedTierProviders.value).toBe(true)
    expect(makePanel(f, true).value.routerProviderRoles).toEqual({ c3: 'dynamic_member' })
  })

  it('recomputes a stale dormant C3 role when the local draft switches to one model', () => {
    const f = useSetupRouterForm()
    f.initFromConfig({
      enabled: true,
      tiers: {
        c0: { provider: 'openrouter', model: 'fast-model' },
        c3: {
          provider: 'tokenrhythm',
          model: 'saved-c3-draft',
          ensemble_enabled: true,
        },
      },
    }, {}, 'openrouter', 'custom', {
      c0: 'direct',
      c3: 'dormant_draft',
    }, 'custom_b5', false)

    expect(f.hasMixedTierProviders.value).toBe(false)

    f.updateTierField('c3', 'ensembleEnabled', false)
    f.updateTierField('c3', 'ensembleSelectionMode', '')

    expect(f.routerProviderRoles.value).toEqual({
      c0: 'direct',
      c3: 'direct',
    })
    expect(f.hasMixedTierProviders.value).toBe(true)
    expect(f.payload()).toMatchObject({
      crossProviderTiers: true,
      tierProviderMismatch: 'veto',
    })
  })

  it('keeps authoritative server roles when the local ensemble context did not change', () => {
    const f = useSetupRouterForm()
    f.initFromConfig({
      enabled: true,
      tiers: {
        c3: { provider: 'openrouter', model: 'quality-model', ensemble_enabled: true },
      },
    }, {}, 'openrouter', 'custom', { c3: 'blocked' }, 'custom_b5', false)

    f.setEnsembleContext('custom_b5', false)

    expect(f.routerProviderRoles.value).toEqual({ c3: 'blocked' })
  })

  it('recomputes a stale direct C3 role when the local draft selects shared fusion', () => {
    const f = useSetupRouterForm()
    f.initFromConfig({
      enabled: true,
      tiers: {
        c0: { provider: 'openrouter', model: 'fast-model' },
        c3: {
          provider: 'tokenrhythm',
          model: 'saved-c3-draft',
          ensemble_enabled: false,
        },
      },
    }, {}, 'openrouter', 'custom', {
      c0: 'direct',
      c3: 'direct',
    }, 'custom_b5', false)

    expect(f.hasMixedTierProviders.value).toBe(true)

    f.updateTierField('c3', 'ensembleEnabled', true)
    f.updateTierField('c3', 'ensembleSelectionMode', '')

    expect(f.routerProviderRoles.value).toEqual({
      c0: 'direct',
      c3: 'dormant_draft',
    })
    expect(f.hasMixedTierProviders.value).toBe(false)
    expect(f.payload()).not.toHaveProperty('crossProviderTiers')
    expect(f.payload()).not.toHaveProperty('tierProviderMismatch')
  })

  it('derives all text tiers as dynamic members for an active router_dynamic plan', () => {
    const f = useSetupRouterForm()
    f.initFromConfig({
      enabled: true,
      tiers: {
        c0: { provider: 'openrouter', model: 'fast-model', thinking_level: 'low' },
        c3: {
          provider: 'tokenrhythm',
          model: 'quality-model',
          thinking_level: 'high',
          ensemble_enabled: true,
        },
        image_model: { provider: 'openai', model: 'vision-model' },
      },
    }, {}, 'openrouter', 'custom', {
      c0: 'dynamic_member',
      c3: 'dynamic_member',
      image_model: 'direct',
    }, 'router_dynamic', false)

    f.setEnsembleContext('router_dynamic', false)
    f.updateTierField('c3', 'provider', 'deepseek')
    f.updateTierField('c3', 'thinkingLevel', 'xhigh')

    expect(f.routerProviderRoles.value).toEqual({
      c0: 'dynamic_member',
      c3: 'dynamic_member',
      image_model: 'direct',
    })
    expect(f.payload()).toMatchObject({
      tiers: {
        c3: {
          provider: 'deepseek',
          model: '',
          thinkingLevel: 'xhigh',
          ensembleEnabled: true,
        },
      },
    })
  })

  it('does not revive retained legacy dynamic metadata after C3 explicitly switches to one model', () => {
    const f = useSetupRouterForm()
    f.initFromConfig({
      enabled: true,
      tiers: {
        c0: { provider: 'openrouter', model: 'fast-model' },
        c3: {
          provider: 'tokenrhythm',
          model: 'quality-model',
          ensemble_selection_mode: 'router_dynamic',
        },
      },
    }, {}, 'openrouter', 'custom', undefined, 'static_openrouter_b5', false)

    f.setEnsembleContext('custom_b5', false)
    expect(f.routerProviderRoles.value.c3).toBe('dynamic_member')

    f.updateTierField('c3', 'ensembleEnabled', false)

    expect(f.routerProviderRoles.value).toEqual({
      c0: 'direct',
      c3: 'direct',
    })
  })

  it('marks every text tier dormant while global static fusion is active', () => {
    const f = useSetupRouterForm()
    f.initFromConfig({
      enabled: true,
      tiers: {
        c0: { provider: 'openrouter', model: 'fast-model' },
        c3: { provider: 'tokenrhythm', model: 'quality-model' },
        image_model: { provider: 'openai', model: 'vision-model' },
      },
    }, {}, 'openrouter', 'custom', undefined, 'static_openrouter_b5', false)

    f.setEnsembleContext('static_openrouter_b5', true)

    expect(f.routerProviderRoles.value).toEqual({
      c0: 'dormant_draft',
      c3: 'dormant_draft',
      image_model: 'direct',
    })
  })

  it('keeps a retained legacy static tier direct while global fusion is locally enabled', () => {
    const f = useSetupRouterForm()
    f.initFromConfig({
      enabled: true,
      tiers: {
        c0: { provider: 'openrouter', model: 'fast-model', thinking_level: 'low' },
        c3: {
          provider: 'tokenrhythm',
          model: 'legacy-quality-model',
          thinking_level: 'high',
          ensemble_selection_mode: 'static_tokenrhythm_b5',
        },
      },
    }, {}, 'openrouter', 'custom', {
      c0: 'direct',
      c3: 'direct',
    }, 'static_openrouter_b5', false)

    f.setEnsembleContext('custom_b5', true)

    expect(f.routerProviderRoles.value).toEqual({
      c0: 'dormant_draft',
      c3: 'direct',
    })
    expect(f.hasMixedTierProviders.value).toBe(true)
    expect(f.payload()).toMatchObject({
      tiers: {
        c3: {
          provider: 'tokenrhythm',
          thinkingLevel: 'high',
          ensembleSelectionMode: 'static_tokenrhythm_b5',
        },
      },
    })
  })

  it('uses server roles for every tier and defaults missing older-Gateway entries to direct', () => {
    const tier = {
      provider: 'openrouter',
      model: 'saved-model',
      thinkingLevel: 'high',
      supportsImage: false,
    }

    expect(routerTierProviderParticipates('c0', tier, { c0: 'blocked' })).toBe(false)
    expect(routerTierProviderParticipates('c0', tier, {})).toBe(true)
  })

  it.each([
    ['single-model C3', { ensemble_enabled: false }],
    ['legacy C3 fusion', { ensemble_selection_mode: 'static_tokenrhythm_b5' }],
  ])('keeps the %s provider in mixed-provider policy', (_label, c3Mode) => {
    const f = useSetupRouterForm()
    f.initFromConfig({
      enabled: true,
      tier_profile: null,
      tiers: {
        c0: { provider: 'openrouter', model: 'deepseek/deepseek-v4-flash' },
        c3: {
          provider: 'tokenrhythm',
          model: 'tier-owned-model',
          ...c3Mode,
        },
      },
    }, {}, 'openrouter')

    expect(f.hasMixedTierProviders.value).toBe(true)
    expect(makePanel(f, true).value.hasMixedTierProviders).toBe(true)
    expect(f.payload()).toMatchObject({
      crossProviderTiers: true,
      tierProviderMismatch: 'veto',
    })
  })

  it('atomically clears provider-scoped model and ensemble when provider changes', () => {
    const f = useSetupRouterForm()
    f.initFromConfig({
      enabled: true,
      tier_profile: 'openrouter',
      tiers: {
        c0: {
          provider: 'openrouter',
          model: 'deepseek/deepseek-v4-flash',
          ensembleSelectionMode: 'static_openrouter_b5',
        },
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
    expect(f.payload()).not.toHaveProperty('tiers.c0.ensembleSelectionMode')
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
