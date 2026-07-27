import { ref } from 'vue'
import { useRpcStore } from '@/stores/rpc'
import { normalizeAgentId } from '@/utils/chat/sessionKeys'
import type { AgentOption, AgentsListResponse } from '@/types/rpc'

/** The implicit default agent every chat surface can always start against. */
const MAIN_AGENT: AgentOption = { id: 'main', name: 'Main Agent' }

// Module-level singleton state so sidebar session metadata shares one agents
// list and one fetch. Mirrors the singleton pattern in useConfirm.
const agents = ref<AgentOption[]>([])
const agentListError = ref(false)
// Dedupes concurrent loadAgents() calls onto a single in-flight request; cleared
// once it settles so a later call can refresh.
let loadPromise: Promise<void> | null = null

/**
 * Shared `agents.list` fetch for sidebar session metadata. IDs are normalized
 * once here so every sidebar consumer sees the same canonical value.
 */
export function useAgentOptions() {
  const rpc = useRpcStore()

  function loadAgents(): Promise<void> {
    if (loadPromise) return loadPromise
    loadPromise = (async () => {
      agentListError.value = false
      try {
        await rpc.waitForConnection()
        const data = await rpc.call<AgentsListResponse>('agents.list')
        agents.value = (data?.agents || [])
          .map(a => ({
            id: normalizeAgentId(a.id || a.agentId || a.name || ''),
            name: a.name || a.id || a.agentId || 'Agent',
            model: a.model || '',
          }))
          .filter((a: AgentOption) => !!a.id)
      } catch (err: unknown) {
        console.warn('[useAgentOptions] agents.list error:', err instanceof Error ? err.message : err)
        agentListError.value = true
        if (!agents.value.length) agents.value = [{ ...MAIN_AGENT }]
      } finally {
        loadPromise = null
      }
    })()
    return loadPromise
  }

  return { agents, agentListError, loadAgents }
}
