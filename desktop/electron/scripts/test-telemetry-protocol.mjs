import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

import {
  CURRENT_NOTICE_VERSION_BY_SCOPE,
  DESKTOP_EARLY_EVENT_SCOPES,
  TELEMETRY_PROTOCOL_FINGERPRINT_SHA256,
} from '../dist/telemetry/contracts.js'

const manifestPath = fileURLToPath(new URL(
  '../../../src/opensquilla/telemetry/contracts/protocol-manifest.v1.json',
  import.meta.url,
))
const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'))

function canonicalize(value) {
  if (Array.isArray(value)) return value.map(canonicalize)
  if (value !== null && typeof value === 'object') {
    return Object.fromEntries(
      Object.keys(value).sort().map((key) => [key, canonicalize(value[key])]),
    )
  }
  return value
}

const fingerprint = createHash('sha256')
  .update(JSON.stringify(canonicalize(manifest)), 'utf8')
  .digest('hex')
assert.equal(fingerprint, TELEMETRY_PROTOCOL_FINGERPRINT_SHA256)
assert.deepEqual(CURRENT_NOTICE_VERSION_BY_SCOPE, manifest.notice_versions)

const manifestEvents = new Map(
  manifest.events.map((event) => [event.event_name, event]),
)
for (const [eventName, scope] of Object.entries(DESKTOP_EARLY_EVENT_SCOPES)) {
  assert.deepEqual(manifestEvents.get(eventName), {
    event_name: eventName,
    event_version: 1,
    scope,
  })
}

console.log('telemetry protocol manifest parity passed')
