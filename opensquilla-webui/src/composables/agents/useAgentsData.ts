import { onActivated, onDeactivated, onUnmounted, ref } from 'vue'
import i18n from '@/i18n'
import type { Agent } from '@/types/agents'
import type { AgentCatalog } from '@/modules/agentCatalog'

const POLL_MS = 30000

export function useAgentsData(catalog: AgentCatalog) {
  const agents = ref<Agent[]>([])
  const loading = ref(false)
  const error = ref<string | null>(null)
  let requestGeneration = 0

  async function load(showLoading: boolean): Promise<void> {
    const generation = ++requestGeneration
    if (showLoading) loading.value = true
    try {
      const listed = await catalog.list()
      if (generation !== requestGeneration) return
      agents.value = [...listed]
      error.value = null
    } catch (cause) {
      if (generation !== requestGeneration) return
      error.value = cause instanceof Error
        ? cause.message
        : i18n.global.t('console.agents.loadFailed')
    } finally {
      if (showLoading && generation === requestGeneration) loading.value = false
    }
  }

  const execute = () => load(true)
  const refresh = () => load(false)

  // The consuming view is kept-alive (route meta.keepAlive), so the poll must
  // bind on activation and release on deactivation — it must not keep firing
  // while the view is cached and off-screen. onActivated also runs on first
  // display; we silently re-fetch on every (re)entry so a keep-alive revisit
  // refreshes without flashing the loading state. onUnmounted is a final safety
  // net for the rare case the KeepAlive cache evicts this instance.
  let pollInterval: ReturnType<typeof setInterval> | null = null
  function teardownLive() {
    if (pollInterval) {
      clearInterval(pollInterval)
      pollInterval = null
    }
  }
  onActivated(() => {
    // First visit (empty) shows the spinner via execute(); revisits refresh
    // silently so the cached list never flashes its loading state.
    void (agents.value.length === 0 ? execute() : refresh())
    pollInterval = setInterval(() => { void refresh() }, POLL_MS)
  })
  onDeactivated(teardownLive)
  onUnmounted(teardownLive)

  // `loadData` is the manual refresh (toolbar button + post-mutation reload):
  // a silent re-fetch so the populated list never flashes its loading state.
  return { agents, loading, error, loadData: refresh }
}
