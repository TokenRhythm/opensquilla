import { computed, ref, type ComputedRef } from 'vue'

interface BehaviorConfig {
  naming?: {
    enabled?: boolean
  }
}

interface BehaviorPanelContext {
  statusText: ComputedRef<string>
}

export function useSetupBehaviorForm() {
  const autoSessionTitles = ref(true)
  const baseline = ref(autoSessionTitles.value)
  const isDirty = computed(() => autoSessionTitles.value !== baseline.value)

  function initFromConfig(config: BehaviorConfig) {
    autoSessionTitles.value = config.naming?.enabled !== false
    baseline.value = autoSessionTitles.value
  }

  function setAutoSessionTitles(enabled: boolean) {
    autoSessionTitles.value = enabled
  }

  function acceptSaved(enabled: boolean) {
    baseline.value = enabled
  }

  function patches(): Record<string, unknown> {
    if (!isDirty.value) return {}
    return { 'naming.enabled': autoSessionTitles.value }
  }

  function createPanel(context: BehaviorPanelContext) {
    return computed(() => ({
      autoSessionTitles: autoSessionTitles.value,
      autoSessionTitlesDirty: isDirty.value,
      statusText: context.statusText.value,
    }))
  }

  return {
    autoSessionTitles,
    isDirty,
    initFromConfig,
    setAutoSessionTitles,
    acceptSaved,
    patches,
    createPanel,
  }
}
