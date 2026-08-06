import type { SandboxCapabilityReport, SandboxSetupStatusPayload } from '@/types/sandbox'

export type SandboxSetupOutcome =
  | 'idle'
  | 'ready'
  | 'cancelled'
  | 'failed'
  | 'verification_failed'

export type SandboxSetupCall = (
  method: string,
  params?: Record<string, unknown>,
) => Promise<unknown>

export type SandboxSetupConnectionWait = () => Promise<unknown>

export interface SandboxSetupResult {
  ready: boolean
  status: SandboxSetupStatusPayload | null
  outcome: Exclude<SandboxSetupOutcome, 'idle'>
}

export function normalizeSandboxSetupStatus(payload: unknown): SandboxSetupStatusPayload | null {
  if (!payload || typeof payload !== 'object') return null
  const raw = payload as Record<string, unknown>
  const state = String(raw.state || '') as SandboxSetupStatusPayload['state']
  if (!['not_setup', 'setting_up', 'ready', 'failed', 'unavailable'].includes(state)) return null
  return {
    state,
    platform: String(raw.platform || ''),
    message: String(raw.message || ''),
    requiresAdmin: raw.requiresAdmin === true || raw.requires_admin === true,
    detail: typeof raw.detail === 'string' ? raw.detail : undefined,
  }
}

export async function ensureSandboxReady(
  call: SandboxSetupCall,
  verifyCapability: (() => Promise<Pick<SandboxCapabilityReport, 'available'> | null>) | null = null,
  waitForConnection: SandboxSetupConnectionWait | null = null,
): Promise<SandboxSetupResult> {
  const finish = async (status: SandboxSetupStatusPayload): Promise<SandboxSetupResult> => {
    if (status.state !== 'ready') {
      return {
        ready: false,
        status,
        outcome: status.detail?.toLowerCase().includes('cancel') ? 'cancelled' : 'failed',
      }
    }
    const report = verifyCapability
      ? await verifyCapability()
      : await call('sandbox.capability.status', { refresh: true }) as { available?: unknown }
    return report?.available === true
      ? { ready: true, status, outcome: 'ready' }
      : { ready: false, status, outcome: 'verification_failed' }
  }

  try {
    const status = normalizeSandboxSetupStatus(await call('sandbox.setup.ensure'))
    if (!status) return { ready: false, status: null, outcome: 'failed' }
    return await finish(status)
  } catch {
    if (!waitForConnection) return { ready: false, status: null, outcome: 'failed' }
    try {
      // The elevated Windows helper can finish successfully after the browser's
      // original response socket has gone away. Reconnect and ask the Gateway
      // for authoritative state instead of making the user repeat UAC.
      await waitForConnection()
      const status = normalizeSandboxSetupStatus(await call('sandbox.setup.status'))
      if (!status) return { ready: false, status: null, outcome: 'failed' }
      return await finish(status)
    } catch {
      return { ready: false, status: null, outcome: 'failed' }
    }
  }
}
