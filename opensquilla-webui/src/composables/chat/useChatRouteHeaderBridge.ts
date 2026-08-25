import {
  computed,
  inject,
  provide,
  shallowRef,
  watch,
  type ComputedRef,
  type InjectionKey,
  type Ref,
} from 'vue'
import type { IconName } from '@/utils/icons'

export type ChatRouteHeaderAction =
  | 'deliverables'
  | 'share'
  | 'copy-session-key'

export interface ChatRouteHeaderModel {
  visible: Readonly<Ref<boolean>>
  title: Readonly<Ref<string>>
  copyState: Readonly<Ref<string | null>>
  copyIcon: Readonly<Ref<IconName>>
  copyLiveText: Readonly<Ref<string>>
  deliverableCount: Readonly<Ref<number>>
  hasNewDeliverable: Readonly<Ref<boolean>>
  shareMode: Readonly<Ref<boolean>>
  shareableMessageCount: Readonly<Ref<number>>
}

export interface ChatRouteHeaderCommands {
  openDeliverables: () => void
  startShare: () => void
  copySessionKey: () => void
  restoreComposerFocus: () => void
}

export interface ChatRouteHeaderHostHandle {
  focusAction: (action: ChatRouteHeaderAction) => boolean
  closeMenu: (restoreFocus?: boolean) => void
}

export interface ChatRouteHeaderRegistration {
  readonly ownerToken: number
  focusAction: (action: ChatRouteHeaderAction) => boolean
  release: () => boolean
}

export interface ChatRouteHeaderBridge {
  model: {
    visible: ComputedRef<boolean>
    title: ComputedRef<string>
    copyState: ComputedRef<string | null>
    copyIcon: ComputedRef<IconName>
    copyLiveText: ComputedRef<string>
    deliverableCount: ComputedRef<number>
    hasNewDeliverable: ComputedRef<boolean>
    shareMode: ComputedRef<boolean>
    shareableMessageCount: ComputedRef<number>
  }
  register: (
    model: ChatRouteHeaderModel,
    commands: ChatRouteHeaderCommands,
  ) => ChatRouteHeaderRegistration
  clear: () => boolean
  setHost: (host: ChatRouteHeaderHostHandle | null) => void
  focusAction: (action: ChatRouteHeaderAction) => boolean
  closeMenu: () => void
  invoke: (command: keyof ChatRouteHeaderCommands) => void
}

interface ChatRouteHeaderOwner {
  token: number
  model: ChatRouteHeaderModel
  commands: ChatRouteHeaderCommands
}

const CHAT_ROUTE_HEADER_BRIDGE_KEY: InjectionKey<ChatRouteHeaderBridge> = Symbol(
  'opensquilla-chat-route-header-bridge',
)

const DEFAULT_COPY_ICON: IconName = 'copy'

/**
 * Owns the only Chat route-header registration in App. The numeric owner token
 * prevents an old ChatView's delayed teardown from clearing a newer instance.
 */
export function provideChatRouteHeaderBridge(): ChatRouteHeaderBridge {
  const activeOwner = shallowRef<ChatRouteHeaderOwner | null>(null)
  let nextOwnerToken = 0
  let host: ChatRouteHeaderHostHandle | null = null

  function ownerValue<K extends keyof ChatRouteHeaderModel>(
    key: K,
    fallback: ChatRouteHeaderModel[K]['value'],
  ): ChatRouteHeaderModel[K]['value'] {
    return activeOwner.value?.model[key].value ?? fallback
  }

  function closeMenu() {
    host?.closeMenu(false)
  }

  function clearOwner(ownerToken?: number): boolean {
    const owner = activeOwner.value
    if (!owner || (ownerToken !== undefined && owner.token !== ownerToken)) return false
    closeMenu()
    activeOwner.value = null
    return true
  }

  function focusActionForOwner(
    action: ChatRouteHeaderAction,
    ownerToken?: number,
  ): boolean {
    const owner = activeOwner.value
    if (
      !owner
      || !owner.model.visible.value
      || (ownerToken !== undefined && owner.token !== ownerToken)
    ) return false
    return host?.focusAction(action) ?? false
  }

  const bridge: ChatRouteHeaderBridge = {
    model: {
      visible: computed(() => ownerValue('visible', false)),
      title: computed(() => ownerValue('title', '')),
      copyState: computed(() => ownerValue('copyState', null)),
      copyIcon: computed(() => ownerValue('copyIcon', DEFAULT_COPY_ICON)),
      copyLiveText: computed(() => ownerValue('copyLiveText', '')),
      deliverableCount: computed(() => ownerValue('deliverableCount', 0)),
      hasNewDeliverable: computed(() => ownerValue('hasNewDeliverable', false)),
      shareMode: computed(() => ownerValue('shareMode', false)),
      shareableMessageCount: computed(() => ownerValue('shareableMessageCount', 0)),
    },
    register(model, commands) {
      closeMenu()
      const ownerToken = ++nextOwnerToken
      activeOwner.value = { token: ownerToken, model, commands }
      return {
        ownerToken,
        focusAction: action => focusActionForOwner(action, ownerToken),
        release: () => clearOwner(ownerToken),
      }
    },
    clear: () => clearOwner(),
    setHost(nextHost) {
      if (host === nextHost) return
      closeMenu()
      host = nextHost
    },
    focusAction: action => focusActionForOwner(action),
    closeMenu,
    invoke(command) {
      activeOwner.value?.commands[command]()
    },
  }

  // Landing transitions keep the host component mounted with v-show. Close
  // any compact menu synchronously before it becomes hidden so it cannot
  // reopen with stale focus when the next session appears.
  watch(
    () => activeOwner.value?.model.visible.value ?? false,
    visible => {
      if (!visible) closeMenu()
    },
    { flush: 'sync' },
  )

  provide(CHAT_ROUTE_HEADER_BRIDGE_KEY, bridge)
  return bridge
}

export function useChatRouteHeaderBridge(): ChatRouteHeaderBridge {
  const bridge = inject(CHAT_ROUTE_HEADER_BRIDGE_KEY)
  if (!bridge) {
    throw new Error('Chat route header bridge is not provided')
  }
  return bridge
}
