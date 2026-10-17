import { onBeforeUnmount, provide, ref, type InjectionKey, type Ref } from 'vue'

/** Keep an expanded canvas's existing message row alive in the virtual list. */
export interface VisualizeMessagePins {
  pin(element: HTMLElement): () => void
}
export const VISUALIZE_MESSAGE_PIN_KEY: InjectionKey<VisualizeMessagePins> = Symbol('VisualizeMessagePins')

export function provideVisualizeMessagePins(root: Ref<HTMLElement | null>) {
  const keys = ref<ReadonlySet<string>>(new Set())
  const owners = new Map<string, Set<symbol>>()
  provide(VISUALIZE_MESSAGE_PIN_KEY, {
    pin(element) {
      const key = root.value?.contains(element) ? element.closest<HTMLElement>('[data-chat-message-key]')?.dataset.chatMessageKey : undefined
      if (!key) return () => {}
      const token = Symbol('visualize-pin')
      const leases = owners.get(key) ?? new Set<symbol>()
      leases.add(token); owners.set(key, leases); keys.value = new Set(owners.keys())
      return () => {
        if (!leases.delete(token)) return
        if (!leases.size) owners.delete(key)
        keys.value = new Set(owners.keys())
      }
    },
  })
  onBeforeUnmount(() => { owners.clear(); keys.value = new Set() })
  return keys
}
