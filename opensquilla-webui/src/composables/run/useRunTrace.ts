import { reactive } from 'vue'

/**
 * Self-contained open-state for the non-chat run-trace surfaces. Chat owns its
 * own choice store (useChatStream via ChatView); SessionInspect and Logs
 * have none, so this supplies two reactive maps plus the exact
 * isToolGroupOpen / isToolItemOpen choice + setter contract `<RunTrace>`
 * binds, so the same component props work on every surface.
 */
export function useRunTrace() {
  const openGroups = reactive(new Map<string, boolean>())
  const openItems = reactive(new Map<string, boolean>())
  return {
    isToolGroupOpen: (id: string) => openGroups.get(id),
    isToolItemOpen: (key: string) => openItems.get(key),
    toggleGroup: (id: string, open: boolean) => {
      openGroups.set(id, open)
    },
    toggleItem: (key: string, open: boolean) => {
      openItems.set(key, open)
    },
  }
}
