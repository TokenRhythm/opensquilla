import { createHash } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

import {
  TELEMETRY_NOTICE_VERSION_BY_SCOPE,
  TELEMETRY_PROTOCOL_FINGERPRINT_SHA256,
} from './protocol'

function canonicalize(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonicalize)
  if (value !== null && typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([key, child]) => [key, canonicalize(child)]),
    )
  }
  return value
}

describe('telemetry protocol artifact', () => {
  it('pins WebUI notice versions to the shared manifest fingerprint', () => {
    const manifest = JSON.parse(readFileSync(resolve(
      process.cwd(),
      '../src/opensquilla/telemetry/contracts/protocol-manifest.v1.json',
    ), 'utf8')) as { notice_versions: Record<string, string> }
    const fingerprint = createHash('sha256')
      .update(JSON.stringify(canonicalize(manifest)), 'utf8')
      .digest('hex')

    expect(fingerprint).toBe(TELEMETRY_PROTOCOL_FINGERPRINT_SHA256)
    expect(TELEMETRY_NOTICE_VERSION_BY_SCOPE).toEqual(manifest.notice_versions)
  })
})
