import assert from 'node:assert/strict'
import {
  chmodSync,
  existsSync,
  lstatSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  renameSync,
  rmSync,
  statSync,
  symlinkSync,
  utimesSync,
  writeFileSync,
} from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import {
  canWriteDurableTelemetryMarker,
  clearEarlyTelemetryScope,
  DESKTOP_RELIABILITY_RECOVERY_MARKER_PREFIX,
  DESKTOP_RELIABILITY_SESSION_MARKER_NAME,
  DESKTOP_UPDATE_TRANSITION_MARKER_NAME,
  DesktopTelemetryRuntimeGate,
  EARLY_SPOOL_MAX_BYTES,
  EARLY_SPOOL_MAX_FILES,
  spoolEarlyTelemetryEvent,
} from '../dist/telemetry/early-spool.js'
import { CURRENT_NOTICE_VERSION_BY_SCOPE } from '../dist/telemetry/contracts.js'

const NOW = new Date('2026-09-01T08:00:00.000Z')
const EVENT_ID = '00000000-0000-4000-8000-000000000001'
const APP_SESSION_ID = '00000000-0000-4000-8000-000000000002'
const ANALYTICS_USER_ID = '00000000-0000-4000-8000-000000000003'
const runtimeGate = new DesktopTelemetryRuntimeGate()
runtimeGate.openAfterConsentSync()

function grantedScope(scope) {
  return {
    enabled: true,
    notice_version: CURRENT_NOTICE_VERSION_BY_SCOPE[scope],
    consented_at_utc: NOW.toISOString(),
    forced_off: false,
  }
}

function writeMirror(path, overrides = {}) {
  const mirror = {
    schema_version: 1,
    reliability: grantedScope('reliability'),
    growth: grantedScope('growth'),
    ...overrides,
  }
  writeFileSync(path, JSON.stringify(mirror), { mode: 0o600 })
}

function reliabilityEvent(overrides = {}) {
  return {
    event_name: 'app_start_result',
    event_version: 1,
    event_id: EVENT_ID,
    occurred_at_utc: NOW.toISOString(),
    source: 'desktop',
    app_version: '0.5.3',
    platform: 'macos',
    outcome: 'success',
    error_code: null,
    duration_ms: 120,
    consent_scope: 'reliability',
    notice_version: CURRENT_NOTICE_VERSION_BY_SCOPE.reliability,
    sample_rate: 1,
    app_session_id: APP_SESSION_ID,
    failure_stage: null,
    ...overrides,
  }
}

function growthEvent(overrides = {}) {
  return {
    event_name: 'first_app_ready',
    event_version: 1,
    event_id: '00000000-0000-4000-8000-000000000004',
    occurred_at_utc: NOW.toISOString(),
    source: 'desktop',
    app_version: '0.5.3',
    platform: 'linux',
    outcome: null,
    error_code: null,
    duration_ms: null,
    consent_scope: 'growth',
    notice_version: CURRENT_NOTICE_VERSION_BY_SCOPE.growth,
    sample_rate: 1,
    analytics_user_id: ANALYTICS_USER_ID,
    ...overrides,
  }
}

function spool(root, mirrorPath, event, extra = {}) {
  return spoolEarlyTelemetryEvent({
    spoolRoot: join(root, 'spool'),
    consentMirrorPath: mirrorPath,
    event,
    runtimeGate,
    env: {},
    now: NOW,
    ...extra,
  })
}

const root = mkdtempSync(join(tmpdir(), 'opensquilla-telemetry-spool-'))
try {
  const mirrorPath = join(root, 'consent-mirror.json')
  writeMirror(mirrorPath)

  const closedGate = new DesktopTelemetryRuntimeGate()
  assert.deepEqual(
    spool(root, mirrorPath, reliabilityEvent(), { runtimeGate: closedGate }),
    { status: 'dropped', reason: 'consent_blocked' },
  )
  closedGate.openAfterConsentSync()
  closedGate.close()
  assert.deepEqual(
    spool(root, mirrorPath, reliabilityEvent(), { runtimeGate: closedGate }),
    { status: 'dropped', reason: 'consent_blocked' },
  )

  const reliability = spool(root, mirrorPath, reliabilityEvent())
  const growth = spool(root, mirrorPath, growthEvent())
  assert.equal(reliability.status, 'written')
  assert.equal(growth.status, 'written')
  assert.equal(lstatSync(reliability.path).isSymbolicLink(), false)
  assert.equal(lstatSync(growth.path).isSymbolicLink(), false)
  assert.deepEqual(readdirSync(join(root, 'spool', 'reliability')), [`${EVENT_ID}.ready`])
  assert.deepEqual(readdirSync(join(root, 'spool', 'growth')), [
    '00000000-0000-4000-8000-000000000004.ready',
  ])
  assert.deepEqual(JSON.parse(readFileSync(reliability.path, 'utf8')), reliabilityEvent())
  assert.equal(
    readdirSync(join(root, 'spool', 'reliability')).some((name) => name.includes('.tmp')),
    false,
  )
  if (process.platform !== 'win32') {
    assert.equal(statSync(join(root, 'spool')).mode & 0o777, 0o700)
    assert.equal(statSync(join(root, 'spool', 'reliability')).mode & 0o777, 0o700)
    assert.equal(statSync(reliability.path).mode & 0o777, 0o600)
  }

  const duplicate = spool(root, mirrorPath, reliabilityEvent())
  assert.equal(duplicate.status, 'duplicate')
  const conflict = spool(root, mirrorPath, reliabilityEvent({ duration_ms: 121 }))
  assert.deepEqual(conflict, { status: 'dropped', reason: 'unsafe_path' })

  assert.deepEqual(
    spool(root, mirrorPath, reliabilityEvent({ prompt: 'synthetic-private-value' })),
    { status: 'dropped', reason: 'invalid_event' },
  )
  assert.deepEqual(
    spool(root, join(root, 'missing-mirror.json'), reliabilityEvent()),
    { status: 'dropped', reason: 'consent_blocked' },
  )
  const invalidMirrorPath = join(root, 'invalid-mirror.json')
  writeFileSync(invalidMirrorPath, JSON.stringify({ schema_version: 1, reliability: {} }))
  assert.deepEqual(spool(root, invalidMirrorPath, reliabilityEvent()), {
    status: 'dropped',
    reason: 'consent_blocked',
  })

  const growthOffPath = join(root, 'growth-off.json')
  writeMirror(growthOffPath, {
    growth: {
      enabled: false,
      notice_version: null,
      consented_at_utc: null,
      forced_off: false,
    },
  })
  assert.deepEqual(spool(join(root, 'off-case'), growthOffPath, growthEvent()), {
    status: 'dropped',
    reason: 'consent_blocked',
  })
  assert.equal(
    spool(join(root, 'off-case'), growthOffPath, reliabilityEvent()).status,
    'written',
  )
  assert.deepEqual(
    spool(join(root, 'dnt-case'), mirrorPath, reliabilityEvent(), { env: { DO_NOT_TRACK: '1' } }),
    { status: 'dropped', reason: 'consent_blocked' },
  )

  const countRoot = join(root, 'count-quota')
  const countScope = join(countRoot, 'spool', 'reliability')
  mkdirSync(countScope, { recursive: true })
  for (let index = 0; index < EARLY_SPOOL_MAX_FILES; index += 1) {
    writeFileSync(join(countScope, `existing-${index}`), '')
  }
  assert.deepEqual(spool(countRoot, mirrorPath, reliabilityEvent()), {
    status: 'dropped',
    reason: 'quota_exceeded',
  })

  const durableQuotaRoot = join(root, 'durable-quota')
  const durableQuotaScope = join(durableQuotaRoot, 'spool', 'reliability')
  mkdirSync(durableQuotaScope, { recursive: true })
  const durableMarker = join(durableQuotaScope, DESKTOP_RELIABILITY_SESSION_MARKER_NAME)
  writeFileSync(durableMarker, '{}')
  assert.equal(spool(durableQuotaRoot, mirrorPath, reliabilityEvent()).status, 'written')
  for (let index = 0; index < EARLY_SPOOL_MAX_FILES - 2; index += 1) {
    writeFileSync(join(durableQuotaScope, `existing-${index}`), '')
  }
  // A duplicate is an acknowledgement, not queue growth, and must stay usable
  // when the existing marker plus ready files exactly fill the quota.
  assert.equal(spool(durableQuotaRoot, mirrorPath, reliabilityEvent()).status, 'duplicate')
  assert.equal(canWriteDurableTelemetryMarker({
    spoolRoot: join(durableQuotaRoot, 'spool'),
    scope: 'reliability',
    markerName: DESKTOP_RELIABILITY_SESSION_MARKER_NAME,
    payloadBytes: 2,
    now: NOW,
  }), true)
  assert.equal(canWriteDurableTelemetryMarker({
    spoolRoot: join(durableQuotaRoot, 'spool'),
    scope: 'reliability',
    markerName: `${DESKTOP_RELIABILITY_RECOVERY_MARKER_PREFIX}00000000-0000-4000-8000-000000000099.tmp`,
    payloadBytes: 2,
    now: NOW,
  }), false)
  assert.equal(canWriteDurableTelemetryMarker({
    spoolRoot: join(durableQuotaRoot, 'spool'),
    scope: 'reliability',
    markerName: DESKTOP_UPDATE_TRANSITION_MARKER_NAME,
    payloadBytes: 2,
    now: NOW,
  }), false)

  const byteRoot = join(root, 'byte-quota')
  const byteScope = join(byteRoot, 'spool', 'reliability')
  mkdirSync(byteScope, { recursive: true })
  writeFileSync(join(byteScope, 'existing'), Buffer.alloc(EARLY_SPOOL_MAX_BYTES))
  assert.deepEqual(spool(byteRoot, mirrorPath, reliabilityEvent()), {
    status: 'dropped',
    reason: 'quota_exceeded',
  })

  const ageRoot = join(root, 'age-prune')
  const ageScope = join(ageRoot, 'spool', 'reliability')
  mkdirSync(ageScope, { recursive: true })
  const expired = join(ageScope, 'expired.ready')
  writeFileSync(expired, '{}')
  const eightDaysAgo = new Date(NOW.valueOf() - 8 * 24 * 60 * 60 * 1000)
  utimesSync(expired, eightDaysAgo, eightDaysAgo)
  assert.equal(spool(ageRoot, mirrorPath, reliabilityEvent()).status, 'written')
  assert.equal(existsSync(expired), false)

  const cleanupRoot = join(root, 'cleanup', 'spool')
  const cleanupReliability = join(cleanupRoot, 'reliability')
  const cleanupGrowth = join(cleanupRoot, 'growth')
  mkdirSync(cleanupReliability, { recursive: true })
  mkdirSync(cleanupGrowth, { recursive: true })
  for (const name of ['one.ready', 'two.processing.123', '.three.123.tmp']) {
    writeFileSync(join(cleanupReliability, name), '{}')
  }
  writeFileSync(join(cleanupGrowth, 'growth.ready'), '{}')
  assert.deepEqual(clearEarlyTelemetryScope(cleanupRoot, 'reliability'), {
    removed: 3,
    failed: 0,
    unsafe: false,
  })
  assert.equal(existsSync(cleanupReliability), false)
  assert.deepEqual(readdirSync(cleanupGrowth), ['growth.ready'])

  const fencedRoot = join(root, 'cleanup-fence', 'spool')
  const fencedScope = join(fencedRoot, 'reliability')
  mkdirSync(fencedScope, { recursive: true })
  const fencedTemp = join(fencedScope, `.${EVENT_ID}.999.synthetic.tmp`)
  writeFileSync(fencedTemp, '{}')
  assert.deepEqual(clearEarlyTelemetryScope(fencedRoot, 'reliability'), {
    removed: 1,
    failed: 0,
    unsafe: false,
  })
  assert.throws(
    () => renameSync(fencedTemp, join(fencedScope, `${EVENT_ID}.ready`)),
    /ENOENT/,
  )

  const unexpectedRoot = join(root, 'cleanup-unexpected', 'spool')
  const unexpectedScope = join(unexpectedRoot, 'reliability')
  mkdirSync(unexpectedScope, { recursive: true })
  writeFileSync(join(unexpectedScope, 'keep.local'), 'keep')
  const unexpected = clearEarlyTelemetryScope(unexpectedRoot, 'reliability')
  assert.deepEqual(unexpected, { removed: 0, failed: 0, unsafe: true })
  const quarantinedUnexpected = readdirSync(unexpectedRoot)
    .find((name) => name.startsWith('.revoked-reliability-'))
  assert.ok(quarantinedUnexpected)
  assert.equal(
    readFileSync(join(unexpectedRoot, quarantinedUnexpected, 'keep.local'), 'utf8'),
    'keep',
  )

  if (process.platform !== 'win32') {
    const symlinkCase = join(root, 'symlink-case')
    mkdirSync(symlinkCase, { recursive: true })
    const mirrorLink = join(symlinkCase, 'mirror-link.json')
    symlinkSync(mirrorPath, mirrorLink)
    assert.deepEqual(spool(symlinkCase, mirrorLink, reliabilityEvent()), {
      status: 'dropped',
      reason: 'consent_blocked',
    })

    const unsafeRoot = join(root, 'unsafe-scope')
    const unsafeSpool = join(unsafeRoot, 'spool')
    const redirect = join(unsafeRoot, 'redirect')
    mkdirSync(unsafeSpool, { recursive: true })
    mkdirSync(redirect)
    symlinkSync(redirect, join(unsafeSpool, 'reliability'))
    assert.deepEqual(spool(unsafeRoot, mirrorPath, reliabilityEvent()), {
      status: 'dropped',
      reason: 'unsafe_path',
    })
  }

  // Permission helpers are best-effort by contract and must not affect payload durability.
  if (process.platform !== 'win32') chmodSync(mirrorPath, 0o600)
} finally {
  rmSync(root, { recursive: true, force: true })
}

console.log('telemetry early spool tests passed')
