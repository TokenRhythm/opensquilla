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
  | 'copy-session-link'
  | 'find'

export interface ChatRouteHeaderFindState {
  open: boolean
  query: string
  resultQuery: string
  matchCount: number
  activeMatch: number
  status: 'idle' | 'loading' | 'ready' | 'error'
  focusSequence: number
  complete: boolean
  coverageComplete?: boolean
}

export interface ChatRouteHeaderModel {
  sessionKey: Readonly<Ref<string>>
  sessionDurable?: Readonly<Ref<boolean>>
  sessionLoading?: Readonly<Ref<boolean>>
  sessionRefreshing?: Readonly<Ref<boolean>>
  sessionContext?: Readonly<Ref<string>>
  sessionPreparable?: Readonly<Ref<boolean>>
  workspaceId?: Readonly<Ref<string | null>>
  workspaceReady?: Readonly<Ref<boolean>>
  workspaceResolved?: Readonly<Ref<boolean>>
  visible: Readonly<Ref<boolean>>
  title: Readonly<Ref<string>>
  optimisticTitle: Readonly<Ref<string>>
  copyState: Readonly<Ref<string | null>>
  copyIcon: Readonly<Ref<IconName>>
  copyLiveText: Readonly<Ref<string>>
  deliverableCount: Readonly<Ref<number>>
  hasNewDeliverable: Readonly<Ref<boolean>>
  shareMode: Readonly<Ref<boolean>>
  shareableMessageCount: Readonly<Ref<number>>
  findState?: Readonly<Ref<ChatRouteHeaderFindState>>
}

export interface ChatRouteHeaderCommands {
  prepareWorkbenchSession?: (expectedSessionKey: string) => Promise<string | null>
  openDeliverables: () => void
  startShare: () => void
  copySessionLink: () => void
  restoreComposerFocus: () => void
  openFind?: () => void
  updateFindQuery?: (query: string) => void
  searchFind?: (query: string) => void
  navigateFind?: (direction: 'previous' | 'next') => void
  closeFind?: () => void
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
    sessionKey: ComputedRef<string>
    sessionDurable: ComputedRef<boolean>
    sessionLoading: ComputedRef<boolean>
    sessionRefreshing: ComputedRef<boolean>
    sessionContext: ComputedRef<string>
    sessionPreparable: ComputedRef<boolean>
    workspaceId: ComputedRef<string | null>
    workspaceReady: ComputedRef<boolean>
    workspaceResolved: ComputedRef<boolean>
    visible: ComputedRef<boolean>
    title: ComputedRef<string>
    optimisticTitle: ComputedRef<string>
    copyState: ComputedRef<string | null>
    copyIcon: ComputedRef<IconName>
    copyLiveText: ComputedRef<string>
    deliverableCount: ComputedRef<number>
    hasNewDeliverable: ComputedRef<boolean>
    shareMode: ComputedRef<boolean>
    shareableMessageCount: ComputedRef<number>
    findState: ComputedRef<ChatRouteHeaderFindState | null>
  }
  register: (
    model: ChatRouteHeaderModel,
    commands: ChatRouteHeaderCommands,
  ) => ChatRouteHeaderRegistration
  clear: () => boolean
  setHost: (host: ChatRouteHeaderHostHandle | null) => void
  focusAction: (action: ChatRouteHeaderAction) => boolean
  closeMenu: () => void
  prepareWorkbenchSession: (expectedSessionKey: string) => Promise<string | null>
  invoke: <K extends keyof ChatRouteHeaderCommands>(
    command: K,
    ...args: Parameters<NonNullable<ChatRouteHeaderCommands[K]>>
  ) => void
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
    fallback: NonNullable<ChatRouteHeaderModel[K]>['value'],
  ): NonNullable<ChatRouteHeaderModel[K]>['value'] {
    return activeOwner.value?.model[key]?.value ?? fallback
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
      sessionKey: computed(() => ownerValue('sessionKey', '')),
      sessionDurable: computed(() => activeOwner.value?.model.sessionDurable?.value ?? false),
      sessionLoading: computed(() => activeOwner.value?.model.sessionLoading?.value ?? false),
      sessionRefreshing: computed(() => activeOwner.value?.model.sessionRefreshing?.value ?? false),
      sessionContext: computed(() => JSON.stringify([
        activeOwner.value?.token ?? null,
        activeOwner.value?.model.sessionContext?.value ?? '',
      ])),
      sessionPreparable: computed(() => activeOwner.value?.model.sessionPreparable?.value ?? false),
      workspaceId: computed(() => activeOwner.value?.model.workspaceId?.value ?? null),
      workspaceReady: computed(() => activeOwner.value?.model.workspaceReady?.value ?? false),
      workspaceResolved: computed(() => activeOwner.value?.model.workspaceResolved?.value ?? false),
      visible: computed(() => ownerValue('visible', false)),
      title: computed(() => ownerValue('title', '')),
      optimisticTitle: computed(() => ownerValue('optimisticTitle', '')),
      copyState: computed(() => ownerValue('copyState', null)),
      copyIcon: computed(() => ownerValue('copyIcon', DEFAULT_COPY_ICON)),
      copyLiveText: computed(() => ownerValue('copyLiveText', '')),
      deliverableCount: computed(() => ownerValue('deliverableCount', 0)),
      hasNewDeliverable: computed(() => ownerValue('hasNewDeliverable', false)),
      shareMode: computed(() => ownerValue('shareMode', false)),
      shareableMessageCount: computed(() => ownerValue('shareableMessageCount', 0)),
      findState: computed(() => activeOwner.value?.model.findState?.value ?? null),
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
    async prepareWorkbenchSession(expectedSessionKey) {
      const owner = activeOwner.value
      if (!owner || !expectedSessionKey || owner.model.sessionKey.value !== expectedSessionKey) return null
      const key = owner.commands.prepareWorkbenchSession
        ? await owner.commands.prepareWorkbenchSession(expectedSessionKey)
        : owner.model.sessionDurable?.value ? expectedSessionKey : null
      // A delayed create belongs to the initiating route owner. A replacement
      // ChatView must never receive its panel, even if it happens to share a key.
      return activeOwner.value === owner && key === owner.model.sessionKey.value
        && owner.model.sessionDurable?.value ? key : null
    },
    invoke(command, ...args) {
      const handler = activeOwner.value?.commands[command] as ((...values: typeof args) => void) | undefined
      handler?.(...args)
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
