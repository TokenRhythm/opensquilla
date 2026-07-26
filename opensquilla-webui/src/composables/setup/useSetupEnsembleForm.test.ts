import { describe, it, expect } from 'vitest'
import { computed } from 'vue'
import {
  CUSTOM_B5_MAX_PROPOSERS,
  CUSTOM_B5_SELECTION_MODE,
  LEGACY_OPENROUTER_MODEL_OPTIONS,
  OPENROUTER_FIXED_ENSEMBLE_AGGREGATOR,
  OPENROUTER_FIXED_ENSEMBLE_PROPOSERS,
  TOKENRHYTHM_FIXED_ENSEMBLE_AGGREGATOR,
  TOKENRHYTHM_FIXED_ENSEMBLE_PROPOSERS,
  staticB5ModeForProvider,
  useSetupEnsembleForm,
  type EnsembleConfigSlice,
} from './useSetupEnsembleForm'

// onboarding.ensemble.configure has partial-payload semantics (omitted keys
// keep their current value on the gateway), so the form's contract is: track
// dirtiness PER KEY and only ever send the keys the user actually changed.

const SAVED = {
  enabled: false,
  selection_mode: CUSTOM_B5_SELECTION_MODE,
  model_options: ['custom/model-a', 'custom/model-b'],
  candidates: [{ provider: 'deepseek', model: 'deepseek-v4-pro', source: 'custom', enabled: true }],
  min_successful_proposers: 2,
  all_failed_policy: 'error',
} satisfies EnsembleConfigSlice

function makePanel(
  form: ReturnType<typeof useSetupEnsembleForm>,
  provider = 'openrouter',
  tierCandidates: Array<{ provider: string; model: string; tier?: string }> = [],
) {
  return form.createPanel({
    statusText: computed(() => ''),
    activeProvider: computed(() => provider),
    activeModel: computed(() => 'current-model'),
    tierCandidates: computed(() => tierCandidates),
    credentialStatus: computed(() => [
      { provider: 'openrouter', available: false, source: 'missing_env', envKey: 'OPENROUTER_API_KEY' },
      { provider: 'deepseek', available: true, source: 'explicit', envKey: 'DEEPSEEK_API_KEY' },
    ]),
  })
}

describe('useSetupEnsembleForm — init + dirty tracking', () => {
  it('is pristine before and after initFromConfig', () => {
    const f = useSetupEnsembleForm()
    expect(f.isDirty.value).toBe(false)

    f.initFromConfig(SAVED)
    expect(f.isDirty.value).toBe(false)
    expect(f.enabled.value).toBe(false)
    expect(f.selectionMode.value).toBe(CUSTOM_B5_SELECTION_MODE)
    expect(f.modelOptions.value).toEqual(['custom/model-a', 'custom/model-b'])
    expect(f.candidates.value).toEqual([
      { provider: 'deepseek', model: 'deepseek-v4-pro', source: 'custom', enabled: true, role: '' },
    ])
    expect(f.minSuccessfulProposers.value).toBe(2)
    expect(f.allFailedPolicy.value).toBe('error')
  })

  it('keeps a stored legacy router_dynamic mode readable', () => {
    const f = useSetupEnsembleForm()
    f.initFromConfig({ ...SAVED, selection_mode: 'router_dynamic' })
    expect(f.selectionMode.value).toBe('router_dynamic')
    expect(f.isDirty.value).toBe(false)
  })

  it('falls back to the shipped defaults for an empty or invalid config slice', () => {
    const f = useSetupEnsembleForm()
    f.initFromConfig({ selection_mode: 'bogus', all_failed_policy: 'bogus', min_successful_proposers: -3 })

    expect(f.enabled.value).toBe(false)
    expect(f.selectionMode.value).toBe('static_openrouter_b5')
    expect(f.modelOptions.value).toEqual([])
    expect(f.candidates.value).toEqual([])
    expect(f.minSuccessfulProposers.value).toBe(1)
    expect(f.allFailedPolicy.value).toBe('fallback_single')
    expect(f.isDirty.value).toBe(false)
  })

  it('reverting an edit back to the baseline clears the dirty state', () => {
    const f = useSetupEnsembleForm()
    f.initFromConfig(SAVED)

    f.setEnabled(true)
    expect(f.isDirty.value).toBe(true)
    f.setEnabled(false)
    expect(f.isDirty.value).toBe(false)

    f.removeModelOption('custom/model-b')
    expect(f.isDirty.value).toBe(true)
    f.addModelOption('custom/model-b')
    expect(f.isDirty.value).toBe(false)
  })
})

describe('useSetupEnsembleForm — partial payload building', () => {
  it('builds an empty payload when nothing changed', () => {
    const f = useSetupEnsembleForm()
    f.initFromConfig(SAVED)
    expect(f.payload()).toEqual({})
  })

  it('sends ONLY the changed key (enabled-only save never clobbers the rest)', () => {
    const f = useSetupEnsembleForm()
    f.initFromConfig(SAVED)

    f.setEnabled(true)
    expect(f.payload()).toEqual({ enabled: true })
  })

  it('sends only the selection mode when only it changed', () => {
    const f = useSetupEnsembleForm()
    f.initFromConfig(SAVED)

    f.setSelectionMode('static_openrouter_b5')
    expect(f.payload()).toEqual({ selectionMode: 'static_openrouter_b5' })
  })

  it('accumulates exactly the dirty keys across several edits', () => {
    const f = useSetupEnsembleForm()
    f.initFromConfig(SAVED)

    f.addModelOption('custom/model-c')
    expect(f.payload()).toEqual({
      modelOptions: ['custom/model-a', 'custom/model-b', 'custom/model-c'],
    })
  })

  it('sends structured candidates with roles when custom candidates changed', () => {
    const f = useSetupEnsembleForm()
    f.initFromConfig(SAVED)

    f.addCandidate('OpenRouter', 'qwen/qwen3.7-max', 'critic')

    expect(f.payload()).toEqual({
      candidates: [
        { provider: 'deepseek', model: 'deepseek-v4-pro', source: 'custom', enabled: true, role: '' },
        { provider: 'openrouter', model: 'qwen/qwen3.7-max', source: 'custom', enabled: true, role: 'critic' },
      ],
    })
  })

  it('sends allFailedPolicy alone when only it changed', () => {
    const f = useSetupEnsembleForm()
    f.initFromConfig(SAVED)

    f.setAllFailedPolicy('fallback_single')
    expect(f.payload()).toEqual({ allFailedPolicy: 'fallback_single' })
  })

  it('never carries candidate editor state into a static preset save', () => {
    const f = useSetupEnsembleForm()
    f.initFromConfig({
      enabled: true,
      selection_mode: 'static_openrouter_b5',
      model_options: [],
      candidates: [],
      min_successful_proposers: 1,
      all_failed_policy: 'fallback_single',
    })
    f.setScheme('custom', 'static_openrouter_b5')
    f.setScheme('preset', 'static_openrouter_b5')
    expect(f.payload()).toEqual({})
  })
})

describe('useSetupEnsembleForm — scheme switching', () => {
  it('switching to custom seeds the lineup from the preset with roles and one aggregator', () => {
    const f = useSetupEnsembleForm()
    f.initFromConfig({
      enabled: true,
      selection_mode: 'static_openrouter_b5',
      model_options: [],
      candidates: [],
      min_successful_proposers: 1,
      all_failed_policy: 'fallback_single',
    })

    f.setScheme('custom', 'static_openrouter_b5')
    expect(f.selectionMode.value).toBe(CUSTOM_B5_SELECTION_MODE)
    const aggregators = f.candidates.value.filter(c => c.role === 'aggregator')
    expect(aggregators).toHaveLength(1)
    expect(aggregators[0]!.model).toBe(OPENROUTER_FIXED_ENSEMBLE_AGGREGATOR)
    const proposers = f.candidates.value.filter(c => c.role !== 'aggregator')
    expect(proposers.map(c => c.model)).toEqual([...OPENROUTER_FIXED_ENSEMBLE_PROPOSERS])
    expect(f.payload().selectionMode).toBe(CUSTOM_B5_SELECTION_MODE)
  })

  it('switching to custom seeds from the TokenRhythm lineup for tokenrhythm', () => {
    const f = useSetupEnsembleForm()
    f.initFromConfig({
      enabled: true,
      selection_mode: 'static_tokenrhythm_b5',
      candidates: [],
    })

    f.setScheme('custom', 'static_tokenrhythm_b5')
    expect(f.selectionMode.value).toBe(CUSTOM_B5_SELECTION_MODE)
    const proposers = f.candidates.value.filter(c => c.role !== 'aggregator')
    expect(proposers.map(c => c.model)).toEqual([...TOKENRHYTHM_FIXED_ENSEMBLE_PROPOSERS])
    expect(f.candidates.value.find(c => c.role === 'aggregator')!.model)
      .toBe(TOKENRHYTHM_FIXED_ENSEMBLE_AGGREGATOR)
  })

  it('switching back to preset restores the baseline candidate inputs', () => {
    const f = useSetupEnsembleForm()
    f.initFromConfig({
      enabled: true,
      selection_mode: 'static_openrouter_b5',
      candidates: [],
    })
    f.setScheme('custom', 'static_openrouter_b5')
    expect(f.candidates.value.length).toBeGreaterThan(0)

    f.setScheme('preset', 'static_openrouter_b5')
    expect(f.selectionMode.value).toBe('static_openrouter_b5')
    expect(f.candidates.value).toEqual([])
    expect(f.isDirty.value).toBe(false)
  })

  it('activateForProvider lands on the preset for preset providers', () => {
    const f = useSetupEnsembleForm()
    f.initFromConfig({})
    f.activateForProvider('tokenrhythm')
    expect(f.selectionMode.value).toBe('static_tokenrhythm_b5')
  })

  it('activateForProvider gives other providers an explicit custom lineup seeded from tiers', () => {
    const f = useSetupEnsembleForm()
    f.initFromConfig({})
    f.activateForProvider('volcengine', [
      { provider: 'volcengine', model: 'doubao-2.0-pro', tier: 'c3' },
      { provider: 'volcengine', model: 'deepseek-v4-flash', tier: 'c0' },
    ])
    expect(f.selectionMode.value).toBe(CUSTOM_B5_SELECTION_MODE)
    expect(f.candidates.value.map(c => c.model)).toEqual(['doubao-2.0-pro', 'deepseek-v4-flash'])
    expect(f.candidates.value[0]!.role).toBe('critic')
    expect(f.candidates.value[1]!.role).toBe('fast_check')
  })

  it('activateForProvider seeds mixed-provider tiers into a custom lineup', () => {
    const f = useSetupEnsembleForm()
    f.initFromConfig({})
    f.activateForProvider('volcengine', [
      { provider: 'volcengine', model: 'doubao-2.0-pro', tier: 'c3' },
      { provider: 'openrouter', model: 'z-ai/glm-5.2', tier: 'c2' },
    ])
    expect(f.candidates.value.map(c => `${c.provider}/${c.model}`)).toEqual([
      'volcengine/doubao-2.0-pro',
      'openrouter/z-ai/glm-5.2',
    ])
  })
})

describe('useSetupEnsembleForm — custom lineup editing', () => {
  it('caps enabled proposers at the maximum', () => {
    const f = useSetupEnsembleForm()
    f.initFromConfig({ selection_mode: CUSTOM_B5_SELECTION_MODE, candidates: [] })
    for (let i = 0; i < CUSTOM_B5_MAX_PROPOSERS + 2; i += 1) {
      f.addCandidate('volcengine', `model-${i}`)
    }
    expect(f.candidates.value).toHaveLength(CUSTOM_B5_MAX_PROPOSERS)
  })

  it('still allows adding the aggregator when proposers are at the cap', () => {
    const f = useSetupEnsembleForm()
    f.initFromConfig({ selection_mode: CUSTOM_B5_SELECTION_MODE, candidates: [] })
    for (let i = 0; i < CUSTOM_B5_MAX_PROPOSERS; i += 1) {
      f.addCandidate('volcengine', `model-${i}`)
    }
    f.addCandidate('volcengine', 'fuser', 'aggregator')
    expect(f.candidates.value).toHaveLength(CUSTOM_B5_MAX_PROPOSERS + 1)
    expect(f.candidates.value.filter(c => c.role === 'aggregator')).toHaveLength(1)
  })

  it('keeps exactly one aggregator: promoting a row demotes the previous one', () => {
    const f = useSetupEnsembleForm()
    f.initFromConfig({
      selection_mode: CUSTOM_B5_SELECTION_MODE,
      candidates: [
        { provider: 'a', model: 'm1', role: 'aggregator' },
        { provider: 'a', model: 'm2' },
      ],
    })
    f.setCandidateRole({ provider: 'a', model: 'm2', source: 'custom', role: '' }, 'aggregator')
    const aggregators = f.candidates.value.filter(c => c.role === 'aggregator')
    expect(aggregators).toHaveLength(1)
    expect(aggregators[0]!.model).toBe('m2')
    expect(f.candidates.value.find(c => c.model === 'm1')!.role).toBe('')
  })

  it('deduplicates proposer deployments across sources while retaining the aggregator slot', () => {
    const f = useSetupEnsembleForm()
    f.initFromConfig({
      selection_mode: CUSTOM_B5_SELECTION_MODE,
      candidates: [
        {
          provider: ' Provider-A ',
          model: 'shared-model',
          source: 'legacy_model_options',
          enabled: false,
          role: 'critic',
        },
        {
          provider: 'provider-a',
          model: 'shared-model',
          source: 'custom',
          enabled: true,
          role: 'primary',
        },
        {
          provider: 'provider-a',
          model: 'shared-model',
          source: 'legacy_model_options',
          enabled: true,
          role: 'aggregator',
        },
      ],
    })

    expect(f.candidates.value).toEqual([
      {
        provider: 'provider-a',
        model: 'shared-model',
        source: 'custom',
        enabled: true,
        role: 'primary',
      },
      {
        provider: 'provider-a',
        model: 'shared-model',
        source: 'legacy_model_options',
        enabled: true,
        role: 'aggregator',
      },
    ])
  })

  it('replaces a proposer atomically without changing quorum or advisory roles', () => {
    const f = useSetupEnsembleForm()
    f.initFromConfig({
      selection_mode: CUSTOM_B5_SELECTION_MODE,
      min_successful_proposers: 3,
      candidates: [
        { provider: 'a', model: 'primary-model', role: 'primary' },
        { provider: 'a', model: 'critic-model', role: 'critic' },
        { provider: 'a', model: 'plain-model' },
        { provider: 'a', model: 'fuser', role: 'aggregator' },
      ],
    })

    expect(makePanel(f, 'a').value.custom.proposerCount).toBe(3)
    f.replaceCandidate(
      { provider: 'a', model: 'primary-model', source: 'custom', role: 'primary' },
      'b',
      'primary-model-next',
    )

    const expectedCandidates = [
      {
        provider: 'b',
        model: 'primary-model-next',
        source: 'custom',
        enabled: true,
        role: 'primary',
      },
      {
        provider: 'a',
        model: 'critic-model',
        source: 'custom',
        enabled: true,
        role: 'critic',
      },
      {
        provider: 'a',
        model: 'plain-model',
        source: 'custom',
        enabled: true,
        role: '',
      },
      {
        provider: 'a',
        model: 'fuser',
        source: 'custom',
        enabled: true,
        role: 'aggregator',
      },
    ]
    expect(f.candidates.value).toEqual(expectedCandidates)
    expect(makePanel(f, 'a').value.custom.proposerCount).toBe(3)
    expect(f.minSuccessfulProposers.value).toBe(3)
    expect(f.payload()).toEqual({ candidates: expectedCandidates })

    const beforeDuplicate = {
      candidates: f.candidates.value.map(candidate => ({ ...candidate })),
      proposerCount: makePanel(f, 'a').value.custom.proposerCount,
      minSuccessfulProposers: f.minSuccessfulProposers.value,
      payload: f.payload(),
    }
    f.replaceCandidate(
      { provider: 'b', model: 'primary-model-next', source: 'custom', role: 'primary' },
      'a',
      'critic-model',
    )
    expect({
      candidates: f.candidates.value,
      proposerCount: makePanel(f, 'a').value.custom.proposerCount,
      minSuccessfulProposers: f.minSuccessfulProposers.value,
      payload: f.payload(),
    }).toEqual(beforeDuplicate)
  })

  it('uses a proposer as aggregator without removing it or its advisory roles', () => {
    const f = useSetupEnsembleForm()
    f.initFromConfig({
      selection_mode: CUSTOM_B5_SELECTION_MODE,
      candidates: [
        { provider: 'a', model: 'old-aggregator', role: 'aggregator' },
        { provider: 'a', model: 'shared-model', role: 'primary' },
        { provider: 'a', model: 'other-proposer', role: 'critic' },
      ],
    })

    f.setAggregator('a', 'shared-model')

    const expectedCandidates = [
      {
        provider: 'a',
        model: 'shared-model',
        source: 'custom',
        enabled: true,
        role: 'primary',
      },
      {
        provider: 'a',
        model: 'other-proposer',
        source: 'custom',
        enabled: true,
        role: 'critic',
      },
      {
        provider: 'a',
        model: 'shared-model',
        source: 'custom',
        enabled: true,
        role: 'aggregator',
      },
    ]
    expect(f.candidates.value).toEqual(expectedCandidates)
    expect(makePanel(f, 'a').value.custom.proposerCount).toBe(2)
    expect(f.payload()).toEqual({ candidates: expectedCandidates })
  })

  it('preserves aggregator source metadata when replacing its deployment', () => {
    const f = useSetupEnsembleForm()
    f.initFromConfig({
      selection_mode: CUSTOM_B5_SELECTION_MODE,
      candidates: [
        { provider: 'a', model: 'draft', source: 'custom', role: 'primary' },
        {
          provider: 'a',
          model: 'old-fuser',
          source: 'legacy_model_options',
          role: 'aggregator',
        },
      ],
    })

    f.setAggregator('b', 'new-fuser')

    expect(f.candidates.value.find(candidate => candidate.role === 'aggregator')).toEqual({
      provider: 'b',
      model: 'new-fuser',
      source: 'legacy_model_options',
      enabled: true,
      role: 'aggregator',
    })
  })

  it('keeps all six proposers when one also fills the aggregator slot', () => {
    const f = useSetupEnsembleForm()
    f.initFromConfig({
      selection_mode: CUSTOM_B5_SELECTION_MODE,
      candidates: [
        ...Array.from({ length: CUSTOM_B5_MAX_PROPOSERS }, (_, index) => ({
          provider: 'a',
          model: `model-${index}`,
        })),
        { provider: 'a', model: 'old-aggregator', role: 'aggregator' },
      ],
    })

    f.setAggregator('a', 'model-0')

    expect(f.candidates.value.filter(candidate => candidate.role !== 'aggregator'))
      .toHaveLength(CUSTOM_B5_MAX_PROPOSERS)
    expect(f.candidates.value.filter(candidate => candidate.role === 'aggregator'))
      .toEqual([
        {
          provider: 'a',
          model: 'model-0',
          source: 'custom',
          enabled: true,
          role: 'aggregator',
        },
      ])
  })

  it('editing the lineup pins the mode to custom_b5 (no ineffective-pool trap)', () => {
    const f = useSetupEnsembleForm()
    f.initFromConfig({
      enabled: true,
      selection_mode: 'static_openrouter_b5',
      candidates: [],
    })
    f.addCandidate('volcengine', 'doubao-2.0-pro')
    expect(f.selectionMode.value).toBe(CUSTOM_B5_SELECTION_MODE)
    expect(f.payload().selectionMode).toBe(CUSTOM_B5_SELECTION_MODE)
  })

  it('clamps an explicit quorum to the proposer count when the lineup shrinks', () => {
    const f = useSetupEnsembleForm()
    f.initFromConfig({
      selection_mode: CUSTOM_B5_SELECTION_MODE,
      min_successful_proposers: 3,
      candidates: [
        { provider: 'a', model: 'm1' },
        { provider: 'a', model: 'm2' },
        { provider: 'a', model: 'm3' },
      ],
    })
    f.removeCandidate({ provider: 'a', model: 'm3', source: 'custom' })
    expect(f.minSuccessfulProposers.value).toBe(2)
  })

  it('importTierCandidates merges tier rows without duplicates and respects the cap', () => {
    const f = useSetupEnsembleForm()
    f.initFromConfig({
      selection_mode: CUSTOM_B5_SELECTION_MODE,
      candidates: [{ provider: 'volcengine', model: 'doubao-2.0-pro' }],
    })
    f.importTierCandidates([
      { provider: 'volcengine', model: 'doubao-2.0-pro', tier: 'c3' },
      { provider: 'volcengine', model: 'deepseek-v4-flash', tier: 'c0' },
    ])
    expect(f.candidates.value.map(c => c.model)).toEqual(['doubao-2.0-pro', 'deepseek-v4-flash'])
  })

  it('importTierCandidates re-enables a matching disabled deployment', () => {
    const f = useSetupEnsembleForm()
    f.initFromConfig({
      selection_mode: CUSTOM_B5_SELECTION_MODE,
      candidates: [{
        provider: 'volcengine',
        model: 'deepseek-v4-flash',
        source: 'custom',
        enabled: false,
        role: 'critic',
      }],
    })

    f.importTierCandidates([
      { provider: 'VOLCENGINE', model: 'deepseek-v4-flash', tier: 'c0' },
    ])

    expect(f.candidates.value).toEqual([{
      provider: 'volcengine',
      model: 'deepseek-v4-flash',
      source: 'custom',
      enabled: true,
      role: 'fast_check',
    }])
  })

  it('replacement wins over a hidden disabled row with the target identity', () => {
    const f = useSetupEnsembleForm()
    f.initFromConfig({
      selection_mode: CUSTOM_B5_SELECTION_MODE,
      candidates: [
        {
          provider: 'provider-b',
          model: 'target-model',
          source: 'legacy_model_options',
          enabled: false,
          role: 'critic',
        },
        {
          provider: 'provider-a',
          model: 'current-model',
          source: 'custom',
          enabled: true,
          role: 'primary',
        },
        { provider: 'provider-a', model: 'other-model' },
      ],
    })

    f.replaceCandidate(
      { provider: 'provider-a', model: 'current-model', source: 'custom', role: 'primary' },
      'provider-b',
      'target-model',
    )

    expect(f.candidates.value).toEqual([
      {
        provider: 'provider-b',
        model: 'target-model',
        source: 'custom',
        enabled: true,
        role: 'primary',
      },
      {
        provider: 'provider-a',
        model: 'other-model',
        source: 'custom',
        enabled: true,
        role: '',
      },
    ])
  })

  it('importTierCandidates can restrict new rows to the current provider', () => {
    const f = useSetupEnsembleForm()
    f.initFromConfig({
      selection_mode: CUSTOM_B5_SELECTION_MODE,
      candidates: [{ provider: 'openrouter', model: 'existing-cross-provider' }],
    })
    f.importTierCandidates([
      { provider: 'volcengine', model: 'doubao-2.0-pro', tier: 'c3' },
      { provider: 'openrouter', model: 'z-ai/glm-5.2', tier: 'c2' },
    ], 'volcengine')
    expect(f.candidates.value.map(c => `${c.provider}/${c.model}`)).toEqual([
      'openrouter/existing-cross-provider',
      'volcengine/doubao-2.0-pro',
    ])
  })

  it('migrateLegacyToCustom folds legacy inputs into a capped custom lineup', () => {
    const f = useSetupEnsembleForm()
    f.initFromConfig({
      enabled: true,
      selection_mode: 'router_dynamic',
      model_options: ['deepseek/deepseek-v4-flash'],
      candidates: [{ provider: 'volcengine', model: 'doubao-2.0-pro' }],
    })
    f.migrateLegacyToCustom([{ provider: 'volcengine', model: 'kimi-k2.6', tier: 'c2' }])
    expect(f.selectionMode.value).toBe(CUSTOM_B5_SELECTION_MODE)
    expect(f.modelOptions.value).toEqual([])
    expect(f.candidates.value.map(c => c.model)).toEqual([
      'doubao-2.0-pro',
      'deepseek/deepseek-v4-flash',
      'kimi-k2.6',
    ])
  })

  it('migrates a bare legacy model id through the active provider', () => {
    const f = useSetupEnsembleForm()
    f.initFromConfig({
      enabled: true,
      selection_mode: 'router_dynamic',
      model_options: ['deepseek-chat'],
    })

    f.migrateLegacyToCustom([], 'deepseek')

    expect(f.candidates.value).toEqual([
      {
        provider: 'deepseek',
        model: 'deepseek-chat',
        source: 'custom',
        enabled: true,
        role: '',
      },
    ])
  })

  it('migrates all six proposers plus the structurally separate aggregator', () => {
    const f = useSetupEnsembleForm()
    f.initFromConfig({
      enabled: true,
      selection_mode: 'router_dynamic',
      candidates: [
        ...Array.from({ length: CUSTOM_B5_MAX_PROPOSERS }, (_, index) => ({
          provider: 'provider-a',
          model: `draft-${index}`,
        })),
        { provider: 'provider-b', model: 'fuser', role: 'aggregator' },
      ],
    })

    f.migrateLegacyToCustom([], 'provider-a')

    expect(f.candidates.value.filter(candidate => candidate.role !== 'aggregator'))
      .toHaveLength(CUSTOM_B5_MAX_PROPOSERS)
    expect(f.candidates.value.filter(candidate => candidate.role === 'aggregator'))
      .toEqual([
        {
          provider: 'provider-b',
          model: 'fuser',
          source: 'custom',
          enabled: true,
          role: 'aggregator',
        },
      ])
  })

  it('keeps the same provider-model identity in proposer and aggregator roles', () => {
    const f = useSetupEnsembleForm()
    f.initFromConfig({
      enabled: true,
      selection_mode: 'router_dynamic',
      candidates: [
        { provider: 'provider-a', model: 'shared' },
        { provider: 'provider-a', model: 'shared', role: 'aggregator' },
      ],
    })

    f.migrateLegacyToCustom([], 'provider-a')

    expect(f.candidates.value.map(candidate => candidate.role)).toEqual(['', 'aggregator'])
    expect(f.candidates.value.map(candidate => `${candidate.provider}/${candidate.model}`))
      .toEqual(['provider-a/shared', 'provider-a/shared'])
  })

  it('migrateLegacyToCustom drops the untouched legacy OpenRouter template', () => {
    const f = useSetupEnsembleForm()
    f.initFromConfig({
      enabled: true,
      selection_mode: 'router_dynamic',
      model_options: [...LEGACY_OPENROUTER_MODEL_OPTIONS],
      candidates: [],
    })
    f.migrateLegacyToCustom([{ provider: 'deepseek', model: 'deepseek-v4-pro', tier: 'c2' }])
    expect(f.candidates.value.map(c => c.model)).toEqual(['deepseek-v4-pro'])
  })
})

describe('useSetupEnsembleForm — model option edits', () => {
  it('trims, ignores empties, and deduplicates on add', () => {
    const f = useSetupEnsembleForm()
    f.initFromConfig(SAVED)

    f.addModelOption('  custom/model-a  ')
    f.addModelOption('')
    f.addModelOption('custom/model-c')
    expect(f.modelOptions.value).toEqual(['custom/model-a', 'custom/model-b', 'custom/model-c'])
  })

  it('removes exactly the named option', () => {
    const f = useSetupEnsembleForm()
    f.initFromConfig(SAVED)

    f.removeModelOption('custom/model-a')
    expect(f.modelOptions.value).toEqual(['custom/model-b'])
  })

  it('clamps min successful proposers to a positive integer', () => {
    const f = useSetupEnsembleForm()
    f.initFromConfig(SAVED)

    f.setMinSuccessfulProposers(0)
    expect(f.minSuccessfulProposers.value).toBe(1)
    f.setMinSuccessfulProposers(2.9)
    expect(f.minSuccessfulProposers.value).toBe(1)
  })
})

describe('useSetupEnsembleForm — panel contract', () => {
  it('reports the preset scheme for a static selection on its own provider', () => {
    const f = useSetupEnsembleForm()
    f.initFromConfig({ enabled: true, selection_mode: 'static_openrouter_b5' })
    const panel = makePanel(f, 'openrouter')
    expect(panel.value.scheme).toBe('preset')
    expect(panel.value.activeProvider).toBe('openrouter')
    expect(panel.value.activeModel).toBe('current-model')
    expect(panel.value.schemeCardsAvailable).toBe(true)
    expect(panel.value.fixedProfile).not.toBeNull()
    expect(panel.value.fixedProfile!.proposers.map(c => c.model))
      .toEqual([...OPENROUTER_FIXED_ENSEMBLE_PROPOSERS])
    expect(panel.value.fixedProfile!.aggregator.model).toBe(OPENROUTER_FIXED_ENSEMBLE_AGGREGATOR)
    expect(panel.value.fixedProfile!.aggregator.role).toBe('aggregator')
  })

  it('uses the TokenRhythm 4+1 profile for the tokenrhythm static selection', () => {
    const f = useSetupEnsembleForm()
    f.initFromConfig({ enabled: true, selection_mode: 'static_tokenrhythm_b5' })
    const panel = makePanel(f, 'tokenrhythm')
    expect(panel.value.scheme).toBe('preset')
    expect(panel.value.fixedProfile!.providerLabel).toBe('TokenRhythm')
    expect(panel.value.fixedProfile!.proposers.map(c => c.model))
      .toEqual([...TOKENRHYTHM_FIXED_ENSEMBLE_PROPOSERS])
    expect(panel.value.fixedProfile!.aggregator.model).toBe(TOKENRHYTHM_FIXED_ENSEMBLE_AGGREGATOR)
    expect(panel.value.presetProviderMismatch).toBe(false)
  })

  it('renders the stored preset lineup with a mismatch flag when it differs from the active provider', () => {
    const f = useSetupEnsembleForm()
    // Stored TokenRhythm preset; the user later switched the active provider
    // to OpenRouter. The runtime builder keys off the stored mode, so the
    // TokenRhythm lineup still runs (and bills) — the card must show it.
    f.initFromConfig({ enabled: true, selection_mode: 'static_tokenrhythm_b5' })
    const panel = makePanel(f, 'openrouter')
    expect(panel.value.scheme).toBe('preset')
    expect(panel.value.fixedProfile!.providerLabel).toBe('TokenRhythm')
    expect(panel.value.fixedProfile!.proposers.map(c => c.model))
      .toEqual([...TOKENRHYTHM_FIXED_ENSEMBLE_PROPOSERS])
    expect(panel.value.fixedProfile!.aggregator.model).toBe(TOKENRHYTHM_FIXED_ENSEMBLE_AGGREGATOR)
    expect(panel.value.presetProviderMismatch).toBe(true)
  })

  it('reports no preset mismatch when the stored preset belongs to the active provider', () => {
    const f = useSetupEnsembleForm()
    f.initFromConfig({ enabled: true, selection_mode: 'static_openrouter_b5' })
    const panel = makePanel(f, 'openrouter')
    expect(panel.value.presetProviderMismatch).toBe(false)
    expect(panel.value.fixedProfile!.providerLabel).toBe('OpenRouter')
  })

  it('reports the custom scheme (no preset cards) for non-preset providers even with a stored static mode', () => {
    const f = useSetupEnsembleForm()
    f.initFromConfig({ enabled: true, selection_mode: 'static_openrouter_b5' })
    const panel = makePanel(f, 'volcengine')
    expect(panel.value.scheme).toBe('custom')
    expect(panel.value.schemeCardsAvailable).toBe(false)
    expect(panel.value.fixedProfile).toBeNull()
    expect(panel.value.showCandidateEditor).toBe(true)
  })

  it('reports the legacy scheme for a stored router_dynamic mode', () => {
    const f = useSetupEnsembleForm()
    f.initFromConfig({ enabled: true, selection_mode: 'router_dynamic' })
    const panel = makePanel(f, 'openrouter')
    expect(panel.value.scheme).toBe('legacy')
  })

  it('splits the custom lineup into aggregator and role-labelled proposers', () => {
    const f = useSetupEnsembleForm()
    f.initFromConfig({
      enabled: true,
      selection_mode: CUSTOM_B5_SELECTION_MODE,
      candidates: [
        { provider: 'deepseek', model: 'deepseek-v4-pro', role: 'primary' },
        { provider: 'openrouter', model: 'z-ai/glm-5.2', role: 'contrast' },
        { provider: 'deepseek', model: 'deepseek-v4-flash', role: 'aggregator' },
      ],
    })
    const panel = makePanel(f, 'deepseek')
    expect(panel.value.custom.proposers.map(c => c.model))
      .toEqual(['deepseek-v4-pro', 'z-ai/glm-5.2'])
    expect(panel.value.custom.proposers[0]!.role).toBe('primary')
    expect(panel.value.custom.aggregator!.model).toBe('deepseek-v4-flash')
    expect(panel.value.custom.aggregatorInherited).toBe(false)
    expect(panel.value.custom.facts.perTurnCalls).toBe(3)
    // quorum auto (stored default 1) -> N-1 = 1
    expect(panel.value.custom.facts.quorum).toBe(1)
  })

  it('falls back to the inherited chat model when no aggregator is assigned', () => {
    const f = useSetupEnsembleForm()
    f.initFromConfig({
      enabled: true,
      selection_mode: CUSTOM_B5_SELECTION_MODE,
      candidates: [
        { provider: 'deepseek', model: 'a' },
        { provider: 'deepseek', model: 'b' },
      ],
    })
    const panel = makePanel(f, 'deepseek')
    expect(panel.value.custom.aggregator).toBeNull()
    expect(panel.value.custom.aggregatorInherited).toBe(true)
    expect(panel.value.custom.inheritedAggregatorModel).toBe('current-model')
  })

  it('flags capacity and diversity states', () => {
    const f = useSetupEnsembleForm()
    f.initFromConfig({
      enabled: true,
      selection_mode: CUSTOM_B5_SELECTION_MODE,
      candidates: [
        { provider: 'a', model: 'deepseek-v4-pro' },
        { provider: 'a', model: 'deepseek-v4-flash' },
      ],
    })
    const panel = makePanel(f, 'volcengine')
    // deepseek-v4 family shared by both proposers.
    expect(panel.value.custom.diversityWarning).toBe(true)
    expect(panel.value.custom.capacity).toBe('ok')

    for (let i = 0; i < 4; i += 1) f.addCandidate('a', `other-model-${i}`)
    expect(makePanel(f, 'volcengine').value.custom.capacity).toBe('full')
    expect(makePanel(f, 'volcengine').value.custom.canAddProposer).toBe(false)
  })

  it('surfaces the effective preset facts (quorum 3/4, 300/480s, 10s grace)', () => {
    const f = useSetupEnsembleForm()
    f.initFromConfig({ enabled: true, selection_mode: 'static_openrouter_b5' })
    const facts = makePanel(f, 'openrouter').value.presetFacts
    expect(facts).toEqual({
      perTurnCalls: 5,
      quorum: 3,
      proposerCount: 4,
      proposerTimeoutSeconds: 300,
      aggregatorTimeoutSeconds: 480,
      quorumGraceSeconds: 10,
    })
  })

  it('materializes non-default legacy options as legacy candidates with provider credentials', () => {
    const f = useSetupEnsembleForm()
    f.initFromConfig({
      enabled: true,
      selection_mode: 'router_dynamic',
      model_options: ['deepseek/deepseek-v4-pro', 'bare-model'],
    })
    const panel = makePanel(f, 'deepseek')
    const legacy = panel.value.customCandidates
    expect(legacy.map(c => `${c.provider}:${c.model}`))
      .toEqual(['openrouter:deepseek/deepseek-v4-pro', 'deepseek:bare-model'])
  })

  it('keeps one legacy deployment visible in both proposer and aggregator slots', () => {
    const f = useSetupEnsembleForm()
    f.initFromConfig({
      enabled: true,
      selection_mode: 'router_dynamic',
      candidates: [
        { provider: 'deepseek', model: 'shared-model' },
        { provider: 'deepseek', model: 'shared-model', role: 'aggregator' },
      ],
    })

    const legacy = makePanel(f, 'deepseek').value.customCandidates
    expect(legacy.map(candidate => candidate.role)).toEqual(['', 'aggregator'])
    expect(legacy.map(candidate => `${candidate.provider}:${candidate.model}`))
      .toEqual(['deepseek:shared-model', 'deepseek:shared-model'])
  })

  it('hides the untouched legacy OpenRouter template from the candidate list', () => {
    const f = useSetupEnsembleForm()
    f.initFromConfig({
      enabled: true,
      selection_mode: 'router_dynamic',
      model_options: [...LEGACY_OPENROUTER_MODEL_OPTIONS],
    })
    const panel = makePanel(f, 'deepseek')
    expect(panel.value.customCandidates).toEqual([])
  })
})

describe('useSetupEnsembleForm — effective timeout facts', () => {
  it('surfaces explicit operator timeout overrides instead of the static defaults', () => {
    const f = useSetupEnsembleForm()
    f.initFromConfig({
      enabled: true,
      selection_mode: 'static_openrouter_b5',
      proposer_timeout_seconds: 600,
      aggregator_timeout_seconds: 900,
    })
    const facts = makePanel(f, 'openrouter').value.presetFacts
    expect(facts.proposerTimeoutSeconds).toBe(600)
    expect(facts.aggregatorTimeoutSeconds).toBe(900)
    expect(facts.quorumGraceSeconds).toBe(10)
    // The stored timeouts are read-only facts, never a pending edit.
    expect(f.isDirty.value).toBe(false)
    expect(f.payload()).toEqual({})
  })

  it('keeps the static defaults for legacy-default and absent stored values', () => {
    const explicitLegacy = useSetupEnsembleForm()
    explicitLegacy.initFromConfig({
      enabled: true,
      selection_mode: 'static_openrouter_b5',
      proposer_timeout_seconds: 3600,
      aggregator_timeout_seconds: 3600,
    })
    const legacyFacts = makePanel(explicitLegacy, 'openrouter').value.presetFacts
    expect(legacyFacts.proposerTimeoutSeconds).toBe(300)
    expect(legacyFacts.aggregatorTimeoutSeconds).toBe(480)

    // Older gateways may omit the keys from the config slice entirely.
    const absent = useSetupEnsembleForm()
    absent.initFromConfig({ enabled: true, selection_mode: 'static_openrouter_b5' })
    const absentFacts = makePanel(absent, 'openrouter').value.presetFacts
    expect(absentFacts.proposerTimeoutSeconds).toBe(300)
    expect(absentFacts.aggregatorTimeoutSeconds).toBe(480)
  })

  it('applies a partial override to the custom lineup facts', () => {
    const f = useSetupEnsembleForm()
    f.initFromConfig({
      enabled: true,
      selection_mode: CUSTOM_B5_SELECTION_MODE,
      candidates: [
        { provider: 'deepseek', model: 'a' },
        { provider: 'deepseek', model: 'b' },
      ],
      proposer_timeout_seconds: 720,
    })
    const facts = makePanel(f, 'deepseek').value.custom.facts
    expect(facts.proposerTimeoutSeconds).toBe(720)
    expect(facts.aggregatorTimeoutSeconds).toBe(480)
    expect(facts.quorumGraceSeconds).toBe(10)
  })

  it('reports raw stored timeouts and no grace for the legacy router_dynamic mode', () => {
    const f = useSetupEnsembleForm()
    f.initFromConfig({
      enabled: true,
      selection_mode: 'router_dynamic',
      candidates: [{ provider: 'deepseek', model: 'a' }],
    })
    const facts = makePanel(f, 'deepseek').value.custom.facts
    expect(facts.proposerTimeoutSeconds).toBe(3600)
    expect(facts.aggregatorTimeoutSeconds).toBe(3600)
    expect(facts.quorumGraceSeconds).toBe(0)
  })
})

describe('staticB5ModeForProvider', () => {
  it('maps preset providers to their static mode and everything else to null', () => {
    expect(staticB5ModeForProvider('openrouter')).toBe('static_openrouter_b5')
    expect(staticB5ModeForProvider('OpenRouter')).toBe('static_openrouter_b5')
    expect(staticB5ModeForProvider('tokenrhythm')).toBe('static_tokenrhythm_b5')
    expect(staticB5ModeForProvider('deepseek')).toBeNull()
    expect(staticB5ModeForProvider('')).toBeNull()
    expect(staticB5ModeForProvider(undefined)).toBeNull()
  })
})
