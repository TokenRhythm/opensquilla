import {
  inject,
  onUnmounted,
  provide,
  shallowReadonly,
  shallowRef,
  watch,
  type InjectionKey,
  type Ref,
  type ShallowRef,
} from 'vue'

export type ChatTopbarPopoverId =
  | 'session-actions'
  | 'system-status'
  | 'language'
  | 'theme'
  | 'bgm'
  | 'desktop-update'

export interface ChatTopbarPopoverCoordinator {
  enabled: Readonly<Ref<boolean>>
  activeId: Readonly<ShallowRef<ChatTopbarPopoverId | null>>
  activate(id: ChatTopbarPopoverId): void
  deactivate(id: ChatTopbarPopoverId): void
}

const chatTopbarPopoverCoordinatorKey: InjectionKey<ChatTopbarPopoverCoordinator> =
  Symbol('chat-topbar-popover-coordinator')

/**
 * Arbitrate only the transient controls in the chat topbar.
 *
 * The existing component-local refs remain the render source of truth. This
 * controller supplies one shared active identity while the chat route is
 * active, without changing independent menu behavior on every other route.
 */
export function provideChatTopbarPopoverCoordinator(
  enabled: Readonly<Ref<boolean>>,
): ChatTopbarPopoverCoordinator {
  const activeId = shallowRef<ChatTopbarPopoverId | null>(null)
  const controller: ChatTopbarPopoverCoordinator = {
    enabled,
    activeId: shallowReadonly(activeId),
    activate(id) {
      if (enabled.value) activeId.value = id
    },
    deactivate(id) {
      // Closing the previous popover can run synchronously after the next one
      // opens. Compare before clearing so that stale close cannot erase the
      // new active owner.
      if (activeId.value === id) activeId.value = null
    },
  }

  // A route boundary closes transient chat chrome. Registrations also clear
  // their local refs when enablement changes, preventing a non-chat menu from
  // carrying into chat (or vice versa) as an uncoordinated ghost popover.
  watch(enabled, () => {
    activeId.value = null
  }, { flush: 'sync' })

  provide(chatTopbarPopoverCoordinatorKey, controller)
  return controller
}

/**
 * Register a component-owned open ref with the chat-only coordinator.
 *
 * `controllerOverride` is used by App for its theme menu because a component
 * cannot inject a value that it provides to its own descendants. Standalone
 * component mounts intentionally fall back to their existing local behavior.
 */
export function useChatTopbarPopoverCoordination(
  id: ChatTopbarPopoverId,
  open: Ref<boolean>,
  controllerOverride?: ChatTopbarPopoverCoordinator,
): void {
  const controller = controllerOverride
    ?? inject(chatTopbarPopoverCoordinatorKey, null)
  if (!controller) return

  watch(open, value => {
    if (!controller.enabled.value) return
    if (value) controller.activate(id)
    else controller.deactivate(id)
  }, { flush: 'sync' })

  watch([controller.enabled, controller.activeId], ([enabled, activeId]) => {
    if (open.value && (!enabled || activeId !== id)) open.value = false
  }, { flush: 'sync' })

  onUnmounted(() => controller.deactivate(id))
}
