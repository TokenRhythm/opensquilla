import type { ChatRenderedMessage } from '@/types/chat'

export function chatMessageKey(message: ChatRenderedMessage, index: number): string {
  if (message.isRouterStrip && message.routerTurnKey) return message.routerTurnKey
  // A local row keeps clientId when the first canonical history snapshot adds
  // messageId. Prefer that stable optimistic identity so stateful disclosures
  // are not unmounted at the live-to-history handoff.
  return message.clientId || message.messageId || message.id || `${message.displayRole || message.role}-${message.sourceIndex ?? index}`
}

let clientMessageSequence = 0
let clientRequestSequence = 0

/** Return a SHA-256-derived canonical UUIDv8, or null when Web Crypto is unavailable. */
export async function stableClientUuid(identity: string): Promise<string | null> {
  const subtle = globalThis.crypto?.subtle
  if (!subtle) return null
  let digest: ArrayBuffer
  try {
    digest = await subtle.digest('SHA-256', new TextEncoder().encode(identity))
  } catch {
    return null
  }
  const hex = Array.from(new Uint8Array(digest).slice(0, 16), byte => (
    byte.toString(16).padStart(2, '0')
  )).join('').split('')
  hex[12] = '8'
  const variant = Number.parseInt(hex[16]!, 16)
  hex[16] = ((variant & 0x3) | 0x8).toString(16)
  const value = hex.join('')
  return [
    value.slice(0, 8),
    value.slice(8, 12),
    value.slice(12, 16),
    value.slice(16, 20),
    value.slice(20),
  ].join('-')
}

export function createClientMessageId(): string {
  clientMessageSequence += 1
  return `local-${Date.now().toString(36)}-${clientMessageSequence.toString(36)}`
}

export function createClientRequestId(): string {
  if (typeof globalThis.crypto?.randomUUID === 'function') {
    return globalThis.crypto.randomUUID()
  }
  const bytes = new Uint8Array(16)
  if (typeof globalThis.crypto?.getRandomValues === 'function') {
    globalThis.crypto.getRandomValues(bytes)
  } else {
    // UUID shape remains stable in legacy/non-secure browser contexts. The
    // timestamp and sequence reduce collisions where Web Crypto is absent;
    // these IDs are idempotency keys, not authentication credentials.
    const seed = Date.now() + (++clientRequestSequence * 0x9e3779b1)
    for (let index = 0; index < bytes.length; index += 1) {
      bytes[index] = Math.floor(Math.random() * 256) ^ ((seed >> (index % 6)) & 0xff)
    }
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40
  bytes[8] = (bytes[8] & 0x3f) | 0x80
  const hex = Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('')
  return [hex.slice(0, 8), hex.slice(8, 12), hex.slice(12, 16), hex.slice(16, 20), hex.slice(20)].join('-')
}

export function isShareableChatMessage(message: ChatRenderedMessage): boolean {
  if (message.stopNotice) return false
  return message.displayRole === 'user' || message.displayRole === 'assistant'
}
