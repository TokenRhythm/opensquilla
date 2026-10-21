import { computed, inject, provide, type ComputedRef, type InjectionKey, type Ref } from 'vue'

const activityDisclosureVisibilityKey: InjectionKey<ComputedRef<boolean>> = Symbol('activity-disclosure-visibility')
const visibleWithoutDisclosure = computed(() => true)

/** Whether the enclosing activity bodies are visible; this does not change a
 * child's own disclosure choice. Hidden details can release their content while
 * the small row component keeps its manual open/closed state. */
export function useActivityDisclosureVisibility(): ComputedRef<boolean> {
  return inject(activityDisclosureVisibilityKey, visibleWithoutDisclosure)
}

export function provideActivityDisclosureVisibility(open: Readonly<Ref<boolean>>): ComputedRef<boolean> {
  const parentVisible = useActivityDisclosureVisibility()
  const visible = computed(() => parentVisible.value && open.value)
  provide(activityDisclosureVisibilityKey, visible)
  return visible
}
