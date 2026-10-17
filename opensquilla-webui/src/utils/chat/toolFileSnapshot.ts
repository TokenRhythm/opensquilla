import type { ToolFileSnapshot } from '@/types/chat'

/** Recorded display content belongs to one completed call, never a live file. */
export function normalizeToolFileSnapshot(value: unknown): ToolFileSnapshot | undefined {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined
  const snapshot = value as Record<string, unknown>
  if ((snapshot.kind !== 'write' && snapshot.kind !== 'edit')
    || typeof snapshot.path !== 'string' || !snapshot.path.trim()
    || typeof snapshot.content !== 'string') return undefined
  const result: ToolFileSnapshot = { kind: snapshot.kind, path: snapshot.path, content: snapshot.content }
  if (snapshot.content_unavailable === true) return { ...result, content: '', content_unavailable: true }
  if (snapshot.sha256_ref !== undefined) {
    if (typeof snapshot.sha256_ref !== 'string' || !/^[a-f0-9]{64}$/i.test(snapshot.sha256_ref)
      || !Number.isSafeInteger(snapshot.bytes) || (snapshot.bytes as number) < 0) return undefined
    return { ...result, sha256_ref: snapshot.sha256_ref.toLowerCase(), bytes: snapshot.bytes as number }
  }
  if (snapshot.legacy_ref !== undefined) {
    const ref = snapshot.legacy_ref
    if (snapshot.kind !== 'write' || !ref || typeof ref !== 'object' || Array.isArray(ref)) return undefined
    const locator = ref as Record<string, unknown>
    if (typeof locator.message_id !== 'string' || !locator.message_id.trim()
      || typeof locator.tool_use_id !== 'string' || !locator.tool_use_id.trim()
      || typeof locator.sha256 !== 'string' || !/^[a-f0-9]{64}$/i.test(locator.sha256)
      || !Number.isSafeInteger(locator.original_chars) || (locator.original_chars as number) < 0) return undefined
    return { ...result, content: '', legacy_ref: { message_id: locator.message_id,
      tool_use_id: locator.tool_use_id, sha256: locator.sha256.toLowerCase(), original_chars: locator.original_chars as number } }
  }
  return result
}
