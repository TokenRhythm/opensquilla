import { computed, ref, type ComputedRef } from 'vue'
import type { useSetupRouterForm } from '@/composables/setup/useSetupRouterForm'
import type { useSetupEnsembleForm } from '@/composables/setup/useSetupEnsembleForm'
import type { DiscoveredModelCatalog } from '@/composables/setup/useSetupProviderForm'

export type ModelStrategy = 'router' | 'ensemble' | 'single'

type RouterForm = ReturnType<typeof useSetupRouterForm>
type EnsembleForm = ReturnType<typeof useSetupEnsembleForm>
type ComputedValue<T> = T extends ComputedRef<infer Value> ? Value : never
type RouterPanel = ComputedValue<ReturnType<RouterForm['createPanel']>>
type EnsemblePanel = ComputedValue<ReturnType<EnsembleForm['createPanel']>>

interface EnsembleTierCandidate {
  provider: string
  model: string
  tier?: string
}

interface ModelStrategyPanelContext {
  hasSavedProvider: ComputedRef<boolean>
  profileSaveSupported: ComputedRef<boolean>
  providerLabel: ComputedRef<string>
  routerPanel: ComputedRef<RouterPanel>
  ensemblePanel: ComputedRef<EnsemblePanel>
  routerTemplateState: ComputedRef<string>
  fixedModelCatalog: ComputedRef<DiscoveredModelCatalog>
}

export function useSetupModelStrategyForm(
  routerForm: RouterForm,
  ensembleForm: EnsembleForm,
  activeProvider?: ComputedRef<string>,
  tierCandidates?: ComputedRef<EnsembleTierCandidate[]>,
  activeModel?: ComputedRef<string>,
) {
  const fixedProvider = ref('')
  const fixedProviderBaseline = ref('')
  const fixedModel = ref('')
  const fixedModelBaseline = ref('')

  const activeStrategy = computed<ModelStrategy>(() => {
    if (ensembleForm.enabled.value) return 'ensemble'
    return routerForm.mode.value === 'disabled' ? 'single' : 'router'
  })

  const fixedProviderDirty = computed(() => fixedProvider.value !== fixedProviderBaseline.value)
  const fixedModelDirty = computed(() => fixedModel.value !== fixedModelBaseline.value)
  const isDirty = computed(() => (
    routerForm.isDirty.value
    || ensembleForm.isDirty.value
    || fixedProviderDirty.value
    || fixedModelDirty.value
  ))

  function initFixedModel(
    value = activeModel?.value || '',
    provider = activeProvider?.value || '',
  ) {
    const normalizedProvider = String(provider || '').trim().toLowerCase()
    const normalized = String(value || '').trim()
    fixedProvider.value = normalizedProvider
    fixedProviderBaseline.value = normalizedProvider
    fixedModel.value = normalized
    fixedModelBaseline.value = normalized
  }

  function setFixedProvider(value: string) {
    fixedProvider.value = String(value || '').trim().toLowerCase()
  }

  function setFixedModel(value: string) {
    fixedModel.value = value
  }

  function fixedModelPatches(): Record<string, string> {
    // Activating another stored provider persists both provider and model in
    // one secret-safe RPC. A plain config patch is only correct while the
    // active provider remains unchanged.
    if (fixedProviderDirty.value || !fixedModelDirty.value) return {}
    return { 'llm.model': fixedModel.value.trim() }
  }

  function setStrategy(next: ModelStrategy) {
    if (next === 'ensemble') {
      routerForm.setRouterMode('disabled')
      ensembleForm.setEnabled(true)
      // Activation changes where the one shared plan applies. The ensemble
      // form preserves an already configured plan and only materializes a
      // hidden legacy/empty draft when necessary.
      ensembleForm.activateForProvider(
        activeProvider?.value,
        tierCandidates?.value ?? [],
      )
      routerForm.setEnsembleContext(
        ensembleForm.selectionMode.value,
        ensembleForm.enabled.value,
      )
      return
    }
    if (next === 'router') {
      ensembleForm.setEnabled(false)
      if (routerForm.mode.value === 'disabled' || routerForm.mode.value === 'openrouter-mix') {
        routerForm.enableFromSavedBinding()
      }
      routerForm.setEnsembleContext(
        ensembleForm.selectionMode.value,
        ensembleForm.enabled.value,
      )
      return
    }
    ensembleForm.setEnabled(false)
    routerForm.setRouterMode('disabled')
    routerForm.setEnsembleContext(
      ensembleForm.selectionMode.value,
      ensembleForm.enabled.value,
    )
  }

  function createPanel(context: ModelStrategyPanelContext) {
    return computed(() => {
      const providerId = fixedProvider.value || activeProvider?.value || ''
      const providerOption = context.routerPanel.value.providerOptions.find(
        option => option.providerId === providerId,
      )
      return {
      activeStrategy: activeStrategy.value,
      hasSavedProvider: context.hasSavedProvider.value,
      profileSaveSupported: context.profileSaveSupported.value,
      providerLabel: context.providerLabel.value,
      routerTemplateState: context.routerTemplateState.value,
      router: {
        ...context.routerPanel.value,
        // Runtime readiness is a snapshot of the last saved configuration.
        // Once any routing, lineup, or fixed-fallback field changes locally,
        // the UI must fall back to conservative draft validation until save.
        tierEnsembleStatusFresh: !isDirty.value,
      },
      ensemble: context.ensemblePanel.value,
      single: {
        providerId,
        providerLabel: providerOption?.label || context.providerLabel.value,
        model: fixedModel.value,
        models: context.fixedModelCatalog.value.models,
        modelSource: context.fixedModelCatalog.value.source,
      },
      cards: [
        {
          id: 'ensemble' as const,
          enabled: activeStrategy.value === 'ensemble',
          titleKey: 'setup.modelStrategy.cards.ensemble.title',
          descKey: 'setup.modelStrategy.cards.ensemble.desc',
          badgeKey: 'setup.modelStrategy.cards.ensemble.badge',
        },
        {
          id: 'router' as const,
          enabled: activeStrategy.value === 'router',
          titleKey: 'setup.modelStrategy.cards.router.title',
          descKey: 'setup.modelStrategy.cards.router.desc',
          badgeKey: 'setup.modelStrategy.cards.router.badge',
        },
        {
          id: 'single' as const,
          enabled: activeStrategy.value === 'single',
          titleKey: 'setup.modelStrategy.cards.single.title',
          descKey: 'setup.modelStrategy.cards.single.desc',
          badgeKey: 'setup.modelStrategy.cards.single.badge',
        },
      ],
    }
    })
  }

  return {
    activeStrategy,
    fixedProvider,
    fixedProviderDirty,
    fixedModel,
    fixedModelDirty,
    isDirty,
    initFixedModel,
    setFixedProvider,
    setFixedModel,
    fixedModelPatches,
    setStrategy,
    createPanel,
  }
}
