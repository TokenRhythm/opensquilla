// PetLaunch — session actions the pet offers (new chat, meme, travel).
//
// Everything goes through the gateway RPC (`sessions.create` + `sessions.send`)
// so the new session lands in the same gateway the pet already watches. There
// is no terminal/pid targeting — "focus a session" means bringing the desktop
// control window to the foreground.

import { GatewayRpc } from './rpc.js'

export interface LaunchDeps {
  rpc: () => GatewayRpc | null
  focusMainWindow: () => void
}

export interface LaunchResult { ok: boolean; key?: string; error?: string }

export function createLaunch(deps: LaunchDeps) {
  async function createSession(opts: { message?: string; displayName?: string } = {}): Promise<LaunchResult> {
    const rpc = deps.rpc()
    if (!rpc) return { ok: false, error: 'offline' }
    try {
      const res = await rpc.call<any>('sessions.create', {
        agentId: 'main',
        displayName: opts.displayName,
        ...(opts.message ? { message: opts.message } : {}),
      })
      const key = res && (res.key || res.sessionKey)
      if (!key) return { ok: false, error: 'no-key' }
      return { ok: true, key }
    } catch (err) {
      return { ok: false, error: err instanceof Error ? err.message : String(err) }
    }
  }

  async function sendMessage(key: string, message: string): Promise<LaunchResult> {
    const rpc = deps.rpc()
    if (!rpc) return { ok: false, error: 'offline' }
    try {
      await rpc.call('sessions.send', { key, message })
      return { ok: true, key }
    } catch (err) {
      return { ok: false, key, error: err instanceof Error ? err.message : String(err) }
    }
  }

  /** Create a session and immediately start a turn with `message`. */
  async function newChatWith(message: string, displayName?: string): Promise<LaunchResult> {
    const rpc = deps.rpc()
    if (!rpc) return { ok: false, error: 'offline' }
    const created = await createSession({ displayName })
    if (!created.ok || !created.key) return created
    return sendMessage(created.key, message)
  }

  function focusSession(): void {
    deps.focusMainWindow()
  }

  return { createSession, sendMessage, newChatWith, focusSession }
}

export type PetLaunch = ReturnType<typeof createLaunch>
