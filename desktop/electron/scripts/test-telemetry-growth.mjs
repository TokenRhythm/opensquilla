import assert from 'node:assert/strict'
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  writeFileSync,
} from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { writeConsentMirror } from '../dist/telemetry/consent-mirror.js'
import { CURRENT_NOTICE_VERSION_BY_SCOPE } from '../dist/telemetry/contracts.js'
import { DesktopTelemetryRuntimeGate } from '../dist/telemetry/early-spool.js'
import {
  clearDesktopGrowthTelemetryState,
  DesktopGrowthTelemetry,
} from '../dist/telemetry/growth.js'

const NOW = new Date('2026-09-02T01:02:03.004Z')

function uuid(counter) {
  return `00000000-0000-4000-8000-${counter.toString(16).padStart(12, '0')}`
}

function ids(start = 1) {
  let counter = start
  return () => uuid(counter++)
}

function paths(root) {
  return {
    profileKey: join(root, 'profile'),
    telemetryDirectory: join(root, 'telemetry'),
    spoolRoot: join(root, 'telemetry', 'desktop-early-spool'),
    consentMirrorPath: join(root, 'telemetry', 'desktop-consent-mirror.json'),
  }
}

async function mirror(path, enabled) {
  await writeConsentMirror(path, {
    schema_version: 1,
    reliability: {
      enabled: false,
      notice_version: null,
      consented_at_utc: null,
      forced_off: false,
    },
    growth: enabled === true
      ? {
          enabled: true,
          notice_version: CURRENT_NOTICE_VERSION_BY_SCOPE.growth,
          consented_at_utc: NOW.toISOString(),
          forced_off: false,
        }
      : {
          enabled: enabled === false ? false : null,
          notice_version: null,
          consented_at_utc: null,
          forced_off: false,
        },
  })
}

function openGate() {
  const gate = new DesktopTelemetryRuntimeGate()
  gate.openAfterConsentSync()
  return gate
}

function runtime(fakePaths, randomId = ids()) {
  const telemetry = new DesktopGrowthTelemetry({
    runtimeGate: openGate(),
    appVersion: () => '0.5.3',
    platform: 'macos',
    env: {},
    nowDate: () => NOW,
    randomId,
  })
  telemetry.observeProfileInspection({
    profileKey: fakePaths.profileKey,
    stableCode: 'fresh_profile',
  })
  return telemetry
}

function readyEvents(fakePaths) {
  const scope = join(fakePaths.spoolRoot, 'growth')
  if (!existsSync(scope)) return []
  return readdirSync(scope)
    .filter((name) => name.endsWith('.ready'))
    .map((name) => JSON.parse(readFileSync(join(scope, name), 'utf8')))
}

const root = mkdtempSync(join(tmpdir(), 'opensquilla-growth-'))
try {
  // Freshness alone is not consent and must not create an identifier or state.
  {
    const fakePaths = paths(join(root, 'unset'))
    await mirror(fakePaths.consentMirrorPath, null)
    const telemetry = runtime(fakePaths)
    telemetry.synchronize(fakePaths)
    assert.equal(existsSync(join(fakePaths.telemetryDirectory, 'growth_identity.json')), false)
    assert.equal(existsSync(join(fakePaths.telemetryDirectory, 'growth_cohort.json')), false)

    await mirror(fakePaths.consentMirrorPath, true)
    telemetry.synchronize(fakePaths)
    assert.equal(existsSync(join(fakePaths.telemetryDirectory, 'growth_identity.json')), true)
    assert.equal(existsSync(join(fakePaths.telemetryDirectory, 'growth_cohort.json')), true)
  }

  // An upgrade/unknown profile with consent is never backfilled into a cohort.
  {
    const fakePaths = paths(join(root, 'upgrade'))
    await mirror(fakePaths.consentMirrorPath, true)
    const telemetry = runtime(fakePaths)
    telemetry.observeProfileInspection({
      profileKey: fakePaths.profileKey,
      stableCode: 'ready',
    })
    telemetry.synchronize(fakePaths)
    telemetry.recordFirstAppReady()
    assert.equal(existsSync(join(fakePaths.telemetryDirectory, 'growth_identity.json')), false)
    assert.deepEqual(readyEvents(fakePaths), [])
  }

  // Imported recovery profiles are explicitly excluded even if a caller
  // accidentally forwards a fresh-looking stable code.
  {
    const fakePaths = paths(join(root, 'imported'))
    await mirror(fakePaths.consentMirrorPath, true)
    const telemetry = runtime(fakePaths)
    telemetry.observeProfileInspection({
      profileKey: fakePaths.profileKey,
      stableCode: 'fresh_profile',
      importedOrMigrated: true,
    })
    telemetry.synchronize(fakePaths)
    assert.equal(existsSync(join(fakePaths.telemetryDirectory, 'growth_cohort.json')), false)
  }

  // The two Desktop milestones are strict, ordered, and exactly once.
  {
    const fakePaths = paths(join(root, 'milestones'))
    await mirror(fakePaths.consentMirrorPath, true)
    const telemetry = runtime(fakePaths, ids(100))
    telemetry.synchronize(fakePaths)
    telemetry.recordOnboardingCompleted()
    telemetry.recordFirstAppReady()
    telemetry.recordOnboardingCompleted()
    telemetry.recordFirstAppReady()

    const events = readyEvents(fakePaths)
    assert.deepEqual(events.map((event) => event.event_name).sort(), [
      'first_app_ready',
      'onboarding_result',
    ])
    assert.equal(new Set(events.map((event) => event.analytics_user_id)).size, 1)
    assert.equal(events.every((event) => event.sample_rate === 1), true)
    const onboarding = events.find((event) => event.event_name === 'onboarding_result')
    assert.equal(onboarding.flow_version, 1)
    const marker = JSON.parse(readFileSync(
      join(fakePaths.telemetryDirectory, 'growth_desktop_milestones.json'),
      'utf8',
    ))
    assert.equal(marker.onboarding_result.status, 'enqueued')
    assert.equal(marker.first_app_ready.status, 'enqueued')
  }

  // A durable cohort receipt can recover identity creation after an OS crash.
  {
    const fakePaths = paths(join(root, 'activation-recovery'))
    await mirror(fakePaths.consentMirrorPath, true)
    mkdirSync(fakePaths.telemetryDirectory, { recursive: true })
    writeFileSync(join(fakePaths.telemetryDirectory, 'growth_cohort.json'), JSON.stringify({
      schema_version: 1,
      state: 'active',
      activated_at_utc: NOW.toISOString(),
    }))
    const telemetry = runtime(fakePaths, ids(200))
    telemetry.observeProfileInspection({
      profileKey: fakePaths.profileKey,
      stableCode: 'ready',
    })
    telemetry.synchronize(fakePaths)
    telemetry.recordFirstAppReady()
    assert.equal(existsSync(join(fakePaths.telemetryDirectory, 'growth_identity.json')), true)
    assert.equal(readyEvents(fakePaths).length, 1)
  }

  // A blocked spool leaves the same pending event for a later launch retry.
  {
    const fakePaths = paths(join(root, 'pending'))
    await mirror(fakePaths.consentMirrorPath, true)
    const first = runtime(fakePaths, ids(300))
    first.synchronize(fakePaths)
    const growthSpool = join(fakePaths.spoolRoot, 'growth')
    mkdirSync(growthSpool, { recursive: true })
    for (let index = 0; index < 512; index += 1) {
      writeFileSync(join(growthSpool, `quota-${index}`), '')
    }
    first.recordOnboardingCompleted()
    assert.deepEqual(readyEvents(fakePaths), [])

    const pendingId = JSON.parse(readFileSync(
      join(fakePaths.telemetryDirectory, 'growth_desktop_milestones.json'),
      'utf8',
    )).onboarding_result.event.event_id
    rmSync(growthSpool, { recursive: true, force: true })
    const second = new DesktopGrowthTelemetry({
      runtimeGate: openGate(),
      appVersion: () => '0.5.3',
      platform: 'linux',
      env: {},
      nowDate: () => NOW,
      randomId: ids(900),
    })
    second.observeProfileInspection({
      profileKey: fakePaths.profileKey,
      stableCode: 'ready',
    })
    second.synchronize(fakePaths)
    assert.equal(readyEvents(fakePaths)[0].event_id, pendingId)
  }

  // Corrupt authority fails closed and remains untouched.
  {
    const fakePaths = paths(join(root, 'corrupt'))
    await mirror(fakePaths.consentMirrorPath, true)
    writeFileSync(join(fakePaths.telemetryDirectory, 'growth_cohort.json'), '{"bad":true}')
    const telemetry = runtime(fakePaths)
    telemetry.synchronize(fakePaths)
    assert.equal(existsSync(join(fakePaths.telemetryDirectory, 'growth_identity.json')), false)
    assert.deepEqual(JSON.parse(readFileSync(
      join(fakePaths.telemetryDirectory, 'growth_cohort.json'),
      'utf8',
    )), { bad: true })
  }

  // Withdrawal removes only the four Growth files and keeps reliability state.
  {
    const directory = join(root, 'cleanup', 'telemetry')
    mkdirSync(directory, { recursive: true })
    for (const name of [
      'growth_identity.json',
      'growth_cohort.json',
      'growth_desktop_milestones.json',
      'growth_gateway_milestones.json',
    ]) writeFileSync(join(directory, name), '{}')
    const keep = join(directory, 'reliability-outbox.sqlite3')
    writeFileSync(keep, 'keep')
    clearDesktopGrowthTelemetryState(directory)
    assert.equal(readFileSync(keep, 'utf8'), 'keep')
    assert.deepEqual(readdirSync(directory), ['reliability-outbox.sqlite3'])
  }
} finally {
  rmSync(root, { recursive: true, force: true })
}

console.log('telemetry growth milestone tests passed')

const mainSource = readFileSync(new URL('../src/main.ts', import.meta.url), 'utf8')
const appSuccess = mainSource.slice(
  mainSource.indexOf('function finishAppStartSuccess'),
  mainSource.indexOf('function finishAppStartFailure'),
)
assert.ok(appSuccess.includes('desktopGrowthTelemetry.recordFirstAppReady()'))
const onboardingSave = mainSource.slice(
  mainSource.indexOf('async function performOnboardingSave'),
  mainSource.indexOf('async function withRecoveryOperation'),
)
assert.ok(
  onboardingSave.indexOf('completeOnboardingFlow(flow, credential)')
    < onboardingSave.indexOf('desktopGrowthTelemetry.recordOnboardingCompleted()'),
)
const profileInspection = mainSource.slice(
  mainSource.indexOf('async function inspectActiveProfileBeforeStartup'),
  mainSource.indexOf('async function openOrResumeDesktopApp'),
)
assert.ok(profileInspection.includes('stableCode: inspection.stable_code'))
assert.ok(profileInspection.includes('desktopGrowthTelemetry.observeProfileInspection(growthInspection)'))
