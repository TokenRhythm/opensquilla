import { onScopeDispose, ref } from 'vue'

interface ProjectWorkspaceEditorOptions {
  allowed: () => boolean
  rename: (workspaceId: string, name: string) => Promise<unknown>
  onError: (error: unknown) => void
}

export function useProjectWorkspaceEditor(options: ProjectWorkspaceEditorOptions) {
  const workspaceId = ref('')
  const saving = ref(false)
  const instance = ref(0)

  function close() {
    instance.value += 1
    workspaceId.value = ''
    saving.value = false
  }

  function open(id: string) {
    if (!options.allowed() || !id) return
    instance.value += 1
    workspaceId.value = id
    saving.value = false
  }

  async function save(name: string) {
    const id = workspaceId.value
    const value = name.trim()
    if (!options.allowed() || !id || !value || saving.value) return
    const currentInstance = instance.value
    saving.value = true
    try {
      await options.rename(id, value)
      // A rename can finish after Cancel and another opening of the same project.
      if (instance.value === currentInstance) close()
    } catch (error) {
      if (instance.value === currentInstance) options.onError(error)
    } finally {
      if (instance.value === currentInstance) saving.value = false
    }
  }

  onScopeDispose(close)
  return { workspaceId, saving, instance, open, close, save }
}
