import { computed, inject, type ComputedRef, type InjectionKey } from 'vue'

/** Surface permissions are separate from transport, execution and resource access. */
export interface ConversationPolicy {
  kind: 'main' | 'side'
  fork: boolean
  edit: boolean
  retry: boolean
  plan: boolean
  goal: boolean
  newChat: boolean
  reset: boolean
}

export const MAIN_CONVERSATION_POLICY: Readonly<ConversationPolicy> = Object.freeze({
  kind: 'main', fork: true, edit: true, retry: true, plan: true, goal: true, newChat: true, reset: true,
})

export const SIDE_CONVERSATION_POLICY: Readonly<ConversationPolicy> = Object.freeze({
  kind: 'side', fork: false, edit: false, retry: false, plan: false, goal: false, newChat: false, reset: false,
})

export const CONVERSATION_POLICY_KEY: InjectionKey<ComputedRef<Readonly<ConversationPolicy>>> = Symbol('conversation-policy')

// Presentation ownership is independent of the child session authorizing a
// file. Descendant link renderers must not activate a hidden child-scoped tab.
export const CONVERSATION_WORKBENCH_OWNER_KEY: InjectionKey<ComputedRef<string>> = Symbol('conversation-workbench-owner')

export function useConversationPolicy() {
  // Legacy standalone renderers retain main-chat behavior. A surface supplies
  // the policy to all its transcript, live-edge and composer descendants.
  return inject(CONVERSATION_POLICY_KEY, computed(() => MAIN_CONVERSATION_POLICY))
}

export function conversationSlashActionAllowed(policy: Readonly<ConversationPolicy>, action: string): boolean {
  if (['/side', '/btw', 'workbench.openSideChat'].includes(action)) return policy.kind === 'main'
  if (['/fork', 'fork_session', 'sessions.fork'].includes(action)) return policy.fork
  if (['/plan', 'plans.setMode', 'plans.toggleMode'].includes(action)) return policy.plan
  if (['/goal', 'goal.set'].includes(action)) return policy.goal
  if (['/new', 'new_chat'].includes(action)) return policy.newChat
  if (['/reset', 'reset_session', 'sessions.reset'].includes(action)) return policy.reset
  return true
}
