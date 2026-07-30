import type { Ref } from 'vue'

// Small helpers shared by the /channels view and its composables so the
// dashboard cards, the drill page, and the members panel cannot drift on
// error rendering, in-flight guards, or approval payloads.

/** Human-readable message for a thrown RPC/transport error. */
export function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

/**
 * Set-based in-flight guard: run `run` under `key`, skipping re-entry while a
 * previous invocation of the same key is still pending. The Set ref is
 * replaced (never mutated in place) so computeds tracking it re-evaluate.
 */
export async function withPendingKey(
  pendingActions: Ref<Set<string>>,
  key: string,
  run: () => Promise<void>,
): Promise<void> {
  if (pendingActions.value.has(key)) return
  pendingActions.value = new Set(pendingActions.value).add(key)
  try {
    await run()
  } finally {
    const next = new Set(pendingActions.value)
    next.delete(key)
    pendingActions.value = next
  }
}

/**
 * Params for channels.pairing.approve. Only include asAdmin when set: a plain
 * approval keeps its minimal payload and never touches channel_admin_senders.
 */
export function approvePairingParams(
  channelName: string,
  pairingId: string,
  asAdmin: boolean,
): Record<string, unknown> {
  const params: Record<string, unknown> = { channelName, pairingId }
  if (asAdmin) params.asAdmin = true
  return params
}
