import { onScopeDispose, ref, watch, type Ref } from 'vue'
import type { SessionDirectory } from '@/modules/sessionDirectory'

interface ChatSessionModelOptions {
  directory: Pick<SessionDirectory, 'resolve'>
  sessionKey: Readonly<Ref<string>>
  isDraft: () => boolean
  available: Readonly<Ref<boolean>>
  connectionEpoch: Readonly<Ref<unknown>>
}

/** Reads the stored session model, never the model used by its last routed turn. */
export function useChatSessionModel(options: ChatSessionModelOptions) {
  const modelName = ref<string | null>(null)
  let generation = 0
  let controller: AbortController | null = null
  let disposed = false

  async function refresh(): Promise<void> {
    const requestGeneration = ++generation
    controller?.abort()
    controller = null
    const key = options.sessionKey.value
    if (disposed || !key || options.isDraft() || !options.available.value) {
      modelName.value = null
      return
    }

    const requestController = new AbortController()
    controller = requestController
    try {
      const session = await options.directory.resolve({ key, signal: requestController.signal })
      if (
        disposed || requestGeneration !== generation || requestController.signal.aborted
        || key !== options.sessionKey.value || options.isDraft() || !options.available.value
      ) return
      modelName.value = typeof session.model === 'string' ? session.model.trim() || null : null
    } catch {
      // Older or unavailable gateways leave the generic single-model label.
      if (requestGeneration === generation) modelName.value = null
    } finally {
      if (controller === requestController) controller = null
    }
  }

  watch([
    options.sessionKey,
    options.isDraft,
    options.available,
    options.connectionEpoch,
  ], () => {
    modelName.value = null
    void refresh()
  }, { immediate: true, flush: 'sync' })

  onScopeDispose(() => {
    disposed = true
    generation += 1
    controller?.abort()
    controller = null
    modelName.value = null
  })

  return { modelName, refresh }
}
