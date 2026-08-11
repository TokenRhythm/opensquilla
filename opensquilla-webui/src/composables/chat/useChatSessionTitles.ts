import {
  computed,
  inject,
  provide,
  type ComputedRef,
  type InjectionKey,
} from 'vue'

import { truncate } from '@/composables/chat/useChatRenderedMessages'

export type ChatSessionTitles = Readonly<Record<string, string>>

const chatSessionTitlesKey: InjectionKey<ComputedRef<ChatSessionTitles>> = Symbol('chat-session-titles')

export function provideChatSessionTitles(titles: ComputedRef<ChatSessionTitles>) {
  provide(chatSessionTitlesKey, titles)
}

export function useChatSessionTitles(): ComputedRef<ChatSessionTitles> {
  return inject(chatSessionTitlesKey, computed<ChatSessionTitles>(() => ({})))
}

const RAW_SESSION_KEY_PATTERN = /\bagent:[a-z0-9_-]+:[a-z0-9_-]+:/i
const UUID_PATTERN = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i

export function looksLikeRawSessionId(value: string): boolean {
  return RAW_SESSION_KEY_PATTERN.test(value)
    || UUID_PATTERN.test(value)
    || /^(agent|cron):/i.test(value)
}

export function isSensibleChatTitle(value: string): boolean {
  const title = String(value || '').trim()
  return !!title && !looksLikeRawSessionId(title)
}

interface ChatSessionTitleItem {
  key: string
  title: string
}

export function buildChatSessionTitles(
  sessions: readonly ChatSessionTitleItem[],
  renameOverrides: ChatSessionTitles,
): ChatSessionTitles {
  const titles: Record<string, string> = {}
  for (const session of sessions) {
    if (session.key && isSensibleChatTitle(session.title)) {
      titles[session.key] = session.title.trim()
    }
  }
  for (const [key, title] of Object.entries(renameOverrides)) {
    if (key && isSensibleChatTitle(title)) titles[key] = title.trim()
  }
  return titles
}

export interface ChatHeaderMessage {
  role: string
  text?: string | null
}

interface ChatHeaderLabels {
  newChat: string
  chatWithSuffix: (suffix: string) => string
}

export function resolveChatHeaderTitle(
  sessionKey: string,
  sessionTitles: ChatSessionTitles,
  messages: readonly ChatHeaderMessage[],
  stripTimePrefix: (text: string) => string,
  labels: ChatHeaderLabels,
): string {
  const storedTitle = sessionKey ? sessionTitles[sessionKey] || '' : ''
  if (isSensibleChatTitle(storedTitle)) return truncate(storedTitle.trim(), 28)

  const firstUser = messages.find(message => (
    message.role === 'user' && stripTimePrefix(message.text || '').trim()
  ))
  if (firstUser) {
    return truncate(
      stripTimePrefix(firstUser.text || '').replace(/\s+/g, ' ').trim(),
      28,
    )
  }

  const suffix = sessionKey.split(':').pop() || ''
  if (!suffix || suffix === 'default') return labels.newChat
  return labels.chatWithSuffix(suffix)
}
