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
  const suppressedRouters = new Set<number>()
  let interactionStart = 0
  let active = false

  function projectInteraction(start: number, end: number): void {
    const interaction = messages.slice(start, end)
    // Rehoming intentionally separates these signals: the source assistant
    // keeps the successful sessions_spawn tool while the final parent reply
    // owns the visible card. Requiring both prevents unrelated cards or
    // malformed tool results from collapsing ordinary route history.
    const hasCard = interaction.some(message => (
      (message.createdSessionLinks?.length ?? 0) > 0
    ))
    const hasCreationSource = interaction.some(createsSession)
    if (!hasCard || !hasCreationSource) return

    const routerIndices = interaction.flatMap((message, offset) => (
      message.isRouterStrip ? [start + offset] : []
    ))
    if (routerIndices.length <= 1) {
      if (isStreaming && end === messages.length) active = true
      return
    }
    routerIndices.slice(1).forEach(index => suppressedRouters.add(index))
    if (isStreaming && end === messages.length) active = true
  }

  // Treat each visible user row as the start of one interaction. Project every
  // completed history interaction as well as the live tail so a later user turn
  // cannot make a previously suppressed resumed-parent router reappear.
  for (let index = 1; index < messages.length; index += 1) {
    if (messages[index]?.displayRole !== 'user') continue
    projectInteraction(interactionStart, index)
    interactionStart = index
  }
  projectInteraction(interactionStart, messages.length)

  if (!suppressedRouters.size) {
    return { messages: messages as ChatRenderedMessage[], active }
  }
  return {
    messages: messages.filter((_, index) => !suppressedRouters.has(index)),
    active,
  }
}

export function hasRouterAfterLatestUser(messages: readonly ChatRenderedMessage[]): boolean {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index]!
    if (message.isRouterStrip) return true
    if (message.displayRole === 'user') return false
  }
  return false
}
