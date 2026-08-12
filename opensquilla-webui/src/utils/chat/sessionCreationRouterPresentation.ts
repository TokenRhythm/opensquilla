import type { ChatRenderedMessage } from '@/types/chat'
import { createdSessionFromToolCall } from '@/utils/chat/createdSessions'

export interface SessionCreationRouterPresentation {
  messages: ChatRenderedMessage[]
  active: boolean
}

function createsSession(message: ChatRenderedMessage): boolean {
  return (message.toolCalls ?? []).some(call => createdSessionFromToolCall(call) !== null)
}

/**
 * During a sessions_spawn handoff, the durable router strip for the creation
 * call can settle just before the created-chat card while the resumed parent
 * call starts a second live router surface below it. Treat both engine turns as
 * one visible interaction: once the card exists, keep the first parent router
 * above the card and suppress every later router in that user interaction.
 *
 * This is deliberately a display-only projection. Rehomed cards, other user
 * turns, and the underlying router records remain unchanged. Once the
 * interaction settles, the same first-router rule remains stable across refresh.
 */
export function projectSessionCreationRouterPresentation(
  messages: readonly ChatRenderedMessage[],
  isStreaming: boolean,
): SessionCreationRouterPresentation {
  let latestUserIndex = -1
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    if (messages[index]?.displayRole === 'user') {
      latestUserIndex = index
      break
    }
  }

  let cardIndex = -1
  let hasCreationSource = false
  for (let index = messages.length - 1; index > latestUserIndex; index -= 1) {
    const message = messages[index]!
    if (cardIndex < 0 && (message.createdSessionLinks?.length ?? 0) > 0) cardIndex = index
    if (createsSession(message)) hasCreationSource = true
  }
  // Rehoming intentionally separates these signals: the source assistant keeps
  // the successful sessions_spawn tool while the final parent reply owns the
  // visible card. Requiring both signals prevents unrelated cards or malformed
  // tool results from collapsing ordinary route history.
  if (cardIndex < 0 || !hasCreationSource) {
    return { messages: messages as ChatRenderedMessage[], active: false }
  }

  if (!isStreaming) {
    const routerIndices = messages.flatMap((message, index) => (
      message.isRouterStrip && index > latestUserIndex ? [index] : []
    ))
    if (routerIndices.length <= 1) {
      return { messages: messages as ChatRenderedMessage[], active: false }
    }
    const firstRouterIndex = routerIndices[0]
    return {
      messages: messages.filter((message, index) => !(
        message.isRouterStrip
        && index > latestUserIndex
        && index !== firstRouterIndex
      )),
      active: false,
    }
  }

  let keptRouter = false
  const projected = messages.filter((message, index) => {
    if (!message.isRouterStrip || index <= latestUserIndex) return true
    if (!keptRouter) {
      keptRouter = true
      return true
    }
    return false
  })
  return { messages: projected, active: true }
}

export function hasRouterAfterLatestUser(messages: readonly ChatRenderedMessage[]): boolean {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index]!
    if (message.isRouterStrip) return true
    if (message.displayRole === 'user') return false
  }
  return false
}
