import type { BigIntStats } from 'node:fs'
import { lstat, realpath } from 'node:fs/promises'
import { isAbsolute } from 'node:path'

interface OwnedConnection {
  instanceId: string; profile: string; nonce: string; url: string; authToken: string
}

function bounded(value: unknown, limit: number): value is string {
  return typeof value === 'string' && value.length > 0 && value.length <= limit
    && value.trim() === value && !/[\u0000-\u001f\u007f]/.test(value)
}

function sameFile(left: BigIntStats, right: BigIntStats): boolean {
  return right.isFile() && ['dev', 'ino', 'size', 'mtimeNs', 'ctimeNs', 'birthtimeNs']
    .every(key => left[key as keyof BigIntStats] === right[key as keyof BigIntStats])
}

/** Returns ordinary text, not a file capability. Never opens or reads file contents. */
export async function chooseLocalFilePaths(request: unknown, deps: {
  /** Main supplies this only for a trusted window and its ready, owned Gateway child. */
  connection: () => OwnedConnection | null
  pick: () => Promise<string[]>
}): Promise<string[]> {
  if (!request || typeof request !== 'object' || Array.isArray(request)
    || Object.keys(request).some(key => key !== 'gatewayInstanceId')
    || !bounded((request as Record<string, unknown>).gatewayInstanceId, 512)) {
    throw new Error('Invalid local path picker request')
  }
  const instanceId = (request as { gatewayInstanceId: string }).gatewayInstanceId
  const initial = deps.connection()
  if (!initial || initial.instanceId !== instanceId) throw new Error('Owned Gateway is unavailable')
  const binding = { ...initial }
  const assertCurrent = () => {
    const current = deps.connection()
    if (!current || (Object.keys(binding) as Array<keyof OwnedConnection>)
      .some(key => current[key] !== binding[key])) {
      throw new Error('Local file selection expired; select again')
    }
  }
  const paths = await deps.pick()
  assertCurrent()
  if (paths.length > 10) throw new Error('Select at most 10 files')
  const result: string[] = []
  for (const selected of paths) {
    if (!bounded(selected, 32768) || !isAbsolute(selected)) throw new Error('Invalid selected path')
    try {
      const before = await lstat(selected, { bigint: true })
      if (!before.isFile()) throw new Error('Not a regular file')
      const canonical = await realpath(selected)
      if (!bounded(canonical, 32768) || !isAbsolute(canonical)) throw new Error('Invalid canonical path')
      const after = await lstat(canonical, { bigint: true })
      // Reject replacement and parent-link retargeting during selection. Later reads are live.
      if (!sameFile(before, after) || await realpath(selected) !== canonical
        || !sameFile(before, await lstat(selected, { bigint: true }))) {
        throw new Error('Selected file changed')
      }
      assertCurrent()
      result.push(canonical)
    } catch {
      // Do not return filesystem errors containing host paths or other private metadata.
      throw new Error('Local file selection changed or is unavailable; select a regular file again')
    }
  }
  assertCurrent()
  return result
}
