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
  const fixedModel = ref('')
  const fixedModelBaseline = ref('')

  const activeStrategy = computed<ModelStrategy>(() => {
    if (ensembleForm.enabled.value) return 'ensemble'
    return routerForm.mode.value === 'disabled' ? 'single' : 'router'
  })

  const fixedModelDirty = computed(() => fixedModel.value !== fixedModelBaseline.value)
  const isDirty = computed(() => (
    routerForm.isDirty.value
    || ensembleForm.isDirty.value
    || fixedModelDirty.value
  ))

  function initFixedModel(value = activeModel?.value || '') {
    const normalized = String(value || '').trim()
    fixedModel.value = normalized
    fixedModelBaseline.value = normalized
  }

  function setFixedModel(value: string) {
    fixedModel.value = value
  }

  function fixedModelPatches(): Record<string, string> {
    if (!fixedModelDirty.value) return {}
    return { 'llm.model': fixedModel.value.trim() }
  }

  function setStrategy(next: ModelStrategy) {
    if (next === 'ensemble') {
      routerForm.setRouterMode('disabled')
      ensembleForm.setEnabled(true)
      // Providers with an official preset land on it; every other provider
      // gets an explicit custom lineup (seeded from the router tiers), never
      // the hidden legacy dynamic mode.
      ensembleForm.activateForProvider(
        activeProvider?.value,
        tierCandidates?.value ?? [],
      )
      return
    }
    if (next === 'router') {
      ensembleForm.setEnabled(false)
      if (routerForm.mode.value === 'disabled' || routerForm.mode.value === 'openrouter-mix') {
        routerForm.enableFromSavedBinding()
      }
      return
    }
    ensembleForm.setEnabled(false)
    routerForm.setRouterMode('disabled')
  }

  function createPanel(context: ModelStrategyPanelContext) {
    return computed(() => ({
      activeStrategy: activeStrategy.value,
      hasSavedProvider: context.hasSavedProvider.value,
      providerLabel: context.providerLabel.value,
      routerTemplateState: context.routerTemplateState.value,
      router: context.routerPanel.value,
      ensemble: context.ensemblePanel.value,
      single: {
        providerId: activeProvider?.value || '',
        providerLabel: context.providerLabel.value,
        model: fixedModel.value,
        models: context.fixedModelCatalog.value.models,
        modelSource: context.fixedModelCatalog.value.source,
      },
      cards: [
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
        {
          id: 'ensemble' as const,
          enabled: activeStrategy.value === 'ensemble',
          titleKey: 'setup.modelStrategy.cards.ensemble.title',
          descKey: 'setup.modelStrategy.cards.ensemble.desc',
          badgeKey: 'setup.modelStrategy.cards.ensemble.badge',
        },
      ],
    }))
  }

  return {
    activeStrategy,
    fixedModel,
    fixedModelDirty,
    isDirty,
    initFixedModel,
    setFixedModel,
    fixedModelPatches,
    setStrategy,
    createPanel,
  }
}
