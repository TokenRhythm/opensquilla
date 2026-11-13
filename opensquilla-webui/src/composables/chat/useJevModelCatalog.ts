import { getCurrentScope, onScopeDispose, ref, watch, type Ref } from 'vue'
import type { ProviderConfiguration } from '@/modules/providerConfiguration'
import type { SetupDiscoveryResult, SetupWorkflow } from '@/modules/setupWorkflow'
import {
  catalogNeedsRefresh,
  normalizeCatalogSyncStatus,
  updateDiscoveredCatalog,
  type DiscoveredModelCatalog,
} from '@/composables/setup/useSetupProviderForm'

export function useJevModelCatalog(options: {
  workflow: SetupWorkflow | null
  providers: ProviderConfiguration | null
  provider: Readonly<Ref<string>>
  active: Readonly<Ref<boolean>>
  connectionEpoch?: Readonly<Ref<unknown>>
}) {
  const empty = (): DiscoveredModelCatalog => ({ models: [], source: 'none' })
  const catalog = ref<DiscoveredModelCatalog>(empty())
  let generation = 0
  let controller: AbortController | null = null
  let inFlight: Promise<void> | null = null
  let disposed = false

  function load(forceRefresh = false): Promise<void> {
    const provider = options.provider.value.trim().toLowerCase()
    if (disposed || !options.active.value || !provider || !options.workflow || !options.providers) {
      return Promise.resolve()
    }
    if (inFlight) return inFlight
    const workflow = options.workflow
    const providers = options.providers
    const owner = generation
    controller = new AbortController()
    const signal = controller.signal
    const current = () => !disposed && owner === generation && options.active.value
      && provider === options.provider.value.trim().toLowerCase()
    catalog.value = { ...catalog.value, discovering: true, discoverError: '', discoverFailureKind: '' }
    const request = (async () => {
      try {
        const status = await providers.status({ probeModels: false, signal })
        if (!current()) return
        const primary = status.activeProvider?.trim().toLowerCase() === provider
        const discover = (cacheOnly = false) => {
          const params = {
            providerId: provider,
            ...(cacheOnly ? { cacheOnly: true } : {}),
            ...(forceRefresh ? { forceRefresh: true } : {}),
          }
          return primary
            ? workflow.provider.discoverPrimaryModels(params, { signal })
            : workflow.profile.discoverProfileModels(params, { signal })
        }
        const publish = (result: SetupDiscoveryResult) => {
          if (!current()) return
          if (result.providerId && result.providerId.trim().toLowerCase() !== provider) {
            throw new Error('Provider model catalog did not match the requested provider')
          }
          catalog.value = updateDiscoveredCatalog(catalog.value, result)
        }
        if (!forceRefresh) {
          const cached = await discover(true)
          if (!current()) return
          if (normalizeCatalogSyncStatus(cached.catalog)?.cacheHit !== false) publish(cached)
          if (!catalogNeedsRefresh(cached)) return
        }
        const result = await discover()
        publish(result)
      } catch {
        if (current()) catalog.value = { ...catalog.value, discoverError: 'unavailable' }
      } finally {
        if (current()) catalog.value = { ...catalog.value, discovering: false }
      }
    })()
    const tracked = request.finally(() => { if (inFlight === tracked) inFlight = null })
    inFlight = tracked
    return tracked
  }

  const stop = watch([
    options.provider,
    options.active,
    () => options.connectionEpoch?.value,
  ], () => {
    generation += 1
    controller?.abort()
    inFlight = null
    catalog.value = empty()
    if (options.active.value) void load()
  }, { immediate: true })

  function dispose() {
    if (disposed) return
    disposed = true
    generation += 1
    controller?.abort()
    stop()
  }
  if (getCurrentScope()) onScopeDispose(dispose)
  return { catalog, load, dispose }
}
