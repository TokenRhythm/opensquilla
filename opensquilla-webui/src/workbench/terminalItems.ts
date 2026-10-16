import type { WorkbenchItem } from './types'
import { normalizeTerminalTarget, terminalTargetKey, type TerminalTarget } from '@/modules/terminal'

function digest(value: string): string {
  const bytes = new TextEncoder().encode(value)
  let hash = 0xcbf29ce484222325n
  for (const byte of bytes) {
    hash ^= BigInt(byte)
    hash = BigInt.asUintN(64, hash * 0x100000001b3n)
  }
  return hash.toString(16).padStart(16, '0')
}

export function createTerminalWorkbenchItem(scopeId: string, requestedTarget?: TerminalTarget): WorkbenchItem {
  const sessionKey = scopeId.trim()
  const target = requestedTarget ? normalizeTerminalTarget(requestedTarget) : null
  if (requestedTarget && !target) throw new Error('a valid terminal target is required')
  return {
    id: `terminal:${digest(target ? `${sessionKey}\0${terminalTargetKey(target)}` : sessionKey)}`,
    kind: 'terminal',
    title: 'Terminal',
    scope: { type: 'session', id: sessionKey },
    hostKind: 'dom',
    retention: 'keep-alive',
    payload: target ? { target } : { sessionKey },
  }
}
