import type { ComputedRef, InjectionKey } from 'vue'
import { truncate } from '@/composables/chat/useChatRenderedMessages'

/**
 * 会话标题映射（key -> 展示标题）的注入 key。
 *
 * App.vue 以 sessions 列表（后端 display_name 优先）为主源，叠加本地草稿
 * 与重命名的乐观覆盖后 provide；ChatView 等深层路由组件注入后，chat header
 * 与 sidebar 使用同一份标题数据，重命名后 header 立即跟随，不再停留在
 * 首条消息摘要。
 */
export const chatSessionTitlesKey: InjectionKey<ComputedRef<Record<string, string>>> = Symbol('chat-session-titles')

// Raw session keys (agent:…:…) and bare UUIDs must never render in the header,
// mirroring the sidebar's filter in App.vue.
const RAW_SESSION_KEY_PATTERN = /\bagent:[a-z0-9_-]+:[a-z0-9_-]+:/i
const UUID_PATTERN = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i

export function looksLikeRawSessionId(value: string): boolean {
  return RAW_SESSION_KEY_PATTERN.test(value) || UUID_PATTERN.test(value) || /^(agent|cron):/i.test(value)
}

/** True when a stored title is meaningful enough to show in the chat header. */
export function isSensibleChatTitle(title: string): boolean {
  const text = String(title || '').trim()
  return !!text && !looksLikeRawSessionId(text)
}

const CHAT_HEADER_MAX = 28

export interface ChatHeaderMessage {
  role: string
  text?: string | null
}

/**
 * Resolve the chat header title. The session's stored title (manual rename /
 * LLM-generated, display_name first) wins; the first user message summary is
 * only a fallback for sessions that carry no meaningful title yet (drafts,
 * sessions outside the sidebar list window).
 */
export function resolveChatHeaderTitle(
  sessionKey: string,
  sessionTitles: Record<string, string>,
  messages: ChatHeaderMessage[],
  stripTimePrefix: (text: string) => string,
): string {
  const named = sessionKey ? sessionTitles[sessionKey] : ''
  if (isSensibleChatTitle(named)) return truncate(named, CHAT_HEADER_MAX)

  const firstUser = messages.find((msg) => msg.role === 'user' && stripTimePrefix(msg.text || '').trim())
  if (firstUser) {
    return truncate(stripTimePrefix(firstUser.text || '').replace(/\s+/g, ' ').trim(), CHAT_HEADER_MAX)
  }
  const suffix = sessionKey.split(':').pop() || ''
  if (!suffix || suffix === 'default') return 'New chat'
  return `Chat ${suffix}`
}
