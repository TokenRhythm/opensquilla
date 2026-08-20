import assert from 'node:assert/strict'
import {
  mkdtempSync,
  mkdirSync,
  readFileSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import {
  DESKTOP_GATEWAY_OWNERSHIP_PROTOCOL,
  canonicalDesktopGatewayIdentityPayload,
  canonicalDesktopGatewayShutdownPayload,
  desktopGatewayIdentityProof,
  desktopGatewayOwnershipMatchesLaunch,
  desktopGatewayOwnershipRecordPath,
  desktopGatewayShutdownProof,
  desktopGatewayStartIdentityConflict,
  desktopProcessStartIdentity,
  desktopProfileFingerprint,
  linuxProcStatStartIdentity,
  loadDesktopGatewayOwnershipRecord,
  posixPsLstartIdentity,
  requestVerifiedDesktopGatewayShutdown,
  sameDesktopGatewayOwnershipInstance,
  verifyDesktopGatewayOwnership,
  verifyDesktopGatewayLaunchOwnership,
  waitForDesktopGatewayOwnershipRelease,
} from '../dist/desktop-gateway-ownership.js'
import {
  DesktopGatewayOwnershipVerificationCoordinator,
} from '../dist/desktop-gateway-ownership-verification.js'

const nonce = 'abcdefghijklmnopqrstuvwxyzABCDEFG'
const challenge = '0123456789abcdef0123456789abcdef'
const record = {
  schema_version: 1,
  protocol: DESKTOP_GATEWAY_OWNERSHIP_PROTOCOL,
  profile_fingerprint: '0123456789abcdef'.repeat(4),
  pid: 4242,
  start_identity: 'opaque-start-identity',
  port: 18791,
  version: '1.2.3',
  instance_nonce: nonce,
}
const unsignedIdentity = {
  schema_version: record.schema_version,
  protocol: record.protocol,
  profile_fingerprint: record.profile_fingerprint,
  pid: record.pid,
  start_identity: record.start_identity,
  port: record.port,
  version: record.version,
  challenge,
}
const canonical = canonicalDesktopGatewayIdentityPayload(unsignedIdentity)
assert.equal(
  canonical,
  '{"challenge":"0123456789abcdef0123456789abcdef","pid":4242,'
    + '"port":18791,"profile_fingerprint":"0123456789abcdef0123456789abcdef'
    + '0123456789abcdef0123456789abcdef","protocol":"opensquilla-desktop-gateway-'
    + 'ownership-v1","schema_version":1,"start_identity":"opaque-start-identity",'
    + '"version":"1.2.3"}',
)
assert.equal(
  desktopGatewayIdentityProof(nonce, unsignedIdentity),
  '67f44cb9dd44df65360c36f5ab7090bcbd30a11c710b8131b960e3ed1f33e0cb',
  'the Electron proof must remain byte-identical to Python\'s golden vector',
)
assert.equal(
  canonicalDesktopGatewayShutdownPayload(record, challenge),
  '{"action":"shutdown","challenge":"0123456789abcdef0123456789abcdef",'
    + '"pid":4242,"port":18791,"profile_fingerprint":"0123456789abcdef0123456789abcdef'
    + '0123456789abcdef0123456789abcdef","protocol":"opensquilla-desktop-gateway-'
    + 'ownership-v1","schema_version":1,"start_identity":"opaque-start-identity",'
    + '"version":"1.2.3"}',
)
assert.equal(
  desktopGatewayShutdownProof(record, challenge),
  '68b2c749e4d727fbbc92cffa8b4e6bbe1e7c7c0ad4175a1671f903d0be2eb5d9',
  'identity and shutdown proofs use separate cross-language domains',
)
assert.equal(
  desktopGatewayOwnershipMatchesLaunch({ ...record, pid: 9999 }, {
    instanceNonce: nonce,
    profileFingerprint: record.profile_fingerprint,
    port: record.port,
  }),
  true,
  'a uv launcher PID may differ from its Python Gateway descendant PID',
)
assert.equal(
  desktopGatewayOwnershipMatchesLaunch(record, {
    instanceNonce: 'x'.repeat(43),
    profileFingerprint: record.profile_fingerprint,
    port: record.port,
  }),
  false,
  'the per-launch nonce remains mandatory',
)

{
  const authority = {
    instanceNonce: nonce,
    profileFingerprint: record.profile_fingerprint,
    port: record.port,
  }
  let challengeCalls = 0
  assert.equal(
    await verifyDesktopGatewayLaunchOwnership('/profile/current', authority, {
      load: () => ({ status: 'valid', record }),
      verify: async () => {
        challengeCalls += 1
        return true
      },
    }),
    true,
    'a matching launch record plus identity challenge proves the listener',
  )
  assert.equal(challengeCalls, 1)

  assert.equal(
    await verifyDesktopGatewayLaunchOwnership('/profile/current', authority, {
      load: () => ({
        status: 'valid',
        record: { ...record, instance_nonce: 'x'.repeat(43) },
      }),
      verify: async () => {
        challengeCalls += 1
        return true
      },
    }),
    false,
    'a healthy foreign listener cannot inherit authority from a live launcher child',
  )
  assert.equal(challengeCalls, 1, 'a foreign record is rejected before any challenge')

  assert.equal(
    await verifyDesktopGatewayLaunchOwnership('/profile/current', authority, {
      load: () => ({ status: 'valid', record }),
      verify: async () => false,
    }),
    false,
    'a matching record without a successful identity challenge fails closed',
  )
}

const root = mkdtempSync(join(tmpdir(), 'opensquilla-desktop-gateway-owner-'))
try {
  const stateDir = join(root, 'state')
  mkdirSync(stateDir)
  const path = desktopGatewayOwnershipRecordPath(stateDir)
  assert.equal(loadDesktopGatewayOwnershipRecord(stateDir).status, 'missing')

  writeFileSync(path, JSON.stringify(record), 'utf8')
  const loaded = loadDesktopGatewayOwnershipRecord(stateDir)
  assert.equal(loaded.status, 'valid')
  assert.deepEqual(loaded.record, record)
  assert.equal(sameDesktopGatewayOwnershipInstance(loaded.record, record), true)
  assert.equal(
    sameDesktopGatewayOwnershipInstance(loaded.record, { ...record, pid: 4243 }),
    false,
  )
  assert.match(desktopProfileFingerprint(root), /^[0-9a-f]{64}$/)

  let capturedUrl = ''
  let capturedMethod = ''
  const verified = await verifyDesktopGatewayOwnership(record, {
    challenge,
    fetchImpl: async (url, init) => {
      capturedUrl = String(url)
      capturedMethod = String(init?.method)
      assert.deepEqual(JSON.parse(String(init?.body)), { challenge })
      return new Response(JSON.stringify({
        ...unsignedIdentity,
        proof: desktopGatewayIdentityProof(nonce, unsignedIdentity),
      }), { status: 200, headers: { 'Content-Type': 'application/json' } })
    },
  })
  assert.equal(verified, true)
  assert.equal(capturedUrl, 'http://127.0.0.1:18791/api/desktop/identity')
  assert.equal(capturedMethod, 'POST')

  let shutdownBody = null
  assert.equal(
    await requestVerifiedDesktopGatewayShutdown(record, {
      challenge,
      fetchImpl: async (url, init) => {
        assert.equal(String(url), 'http://127.0.0.1:18791/api/desktop/shutdown')
        shutdownBody = JSON.parse(String(init?.body))
        return new Response('{}', { status: 202 })
      },
    }),
    true,
  )
  assert.deepEqual(shutdownBody, {
    challenge,
    proof: '68b2c749e4d727fbbc92cffa8b4e6bbe1e7c7c0ad4175a1671f903d0be2eb5d9',
  })
  assert.equal(
    await requestVerifiedDesktopGatewayShutdown(record, {
      challenge,
      fetchImpl: async () => new Response('{}', { status: 403 }),
    }),
    false,
  )

  for (const badPayload of [
    { ...unsignedIdentity, proof: '0'.repeat(64) },
    {
      ...unsignedIdentity,
      profile_fingerprint: 'f'.repeat(64),
      proof: desktopGatewayIdentityProof(nonce, {
        ...unsignedIdentity,
        profile_fingerprint: 'f'.repeat(64),
      }),
    },
    {
      ...unsignedIdentity,
      proof: desktopGatewayIdentityProof(nonce, unsignedIdentity),
      unexpected: true,
    },
  ]) {
    assert.equal(
      await verifyDesktopGatewayOwnership(record, {
        challenge,
        fetchImpl: async () => new Response(JSON.stringify(badPayload), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      }),
      false,
      'a proof, metadata, or response-shape mismatch must fail closed',
    )
  }

  setTimeout(() => rmSync(path), 10)
  assert.equal(
    await waitForDesktopGatewayOwnershipRelease(stateDir, record, {
      timeoutMs: 500,
      pollIntervalMs: 5,
    }),
    true,
  )
  writeFileSync(path, JSON.stringify({ ...record, instance_nonce: 's'.repeat(43) }), 'utf8')
  assert.equal(
    await waitForDesktopGatewayOwnershipRelease(stateDir, record, {
      timeoutMs: 100,
      pollIntervalMs: 5,
    }),
    false,
    'a successor ownership record must never be treated as our released child',
  )

  writeFileSync(path, '{truncated', 'utf8')
  assert.equal(loadDesktopGatewayOwnershipRecord(stateDir).status, 'invalid')
  assert.equal(readFileSync(path, 'utf8'), '{truncated', 'inspection must never repair/delete')

  writeFileSync(path, JSON.stringify({ ...record, schema_version: 2 }), 'utf8')
  assert.equal(loadDesktopGatewayOwnershipRecord(stateDir).status, 'invalid')

  rmSync(path)
  const outside = join(root, 'outside-record.json')
  writeFileSync(outside, JSON.stringify(record), 'utf8')
  try {
    symlinkSync(outside, path)
    assert.equal(loadDesktopGatewayOwnershipRecord(stateDir).status, 'invalid')
    assert.equal(readFileSync(outside, 'utf8'), JSON.stringify(record))
  } catch (error) {
    if (process.platform !== 'win32' || error?.code !== 'EPERM') throw error
  }
} finally {
  rmSync(root, { recursive: true, force: true })
}

// --- process-start identity: PID recycling is detected, never over-claimed ---

// /proc stat parsing tolerates a parenthesized comm with spaces and ')'.
const procStatSuffix = 'S 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 987654321'
assert.equal(
  linuxProcStatStartIdentity(`4242 (gateway wor)ker) ${procStatSuffix}`),
  'linux-proc-start-ticks:987654321',
)
assert.equal(linuxProcStatStartIdentity('no close paren'), null)
assert.equal(linuxProcStatStartIdentity('4242 (short) S 1 2'), null)
assert.equal(
  linuxProcStatStartIdentity(`4242 (x) ${procStatSuffix.replace('987654321', 'oops')}`),
  null,
)

// ps lstart output is whitespace-normalized exactly like the Gateway does.
assert.equal(
  posixPsLstartIdentity('Mon Jul 20 12:34:56  2026\n'),
  'posix-ps-lstart:Mon Jul 20 12:34:56 2026',
)
assert.equal(posixPsLstartIdentity('   \n'), null)
assert.equal(posixPsLstartIdentity(''), null)

// Only a same-scheme, different-value identity is a conflict. Unknown, null,
// cross-scheme, and the Gateway's opaque runtime fallback all fail open.
assert.equal(
  desktopGatewayStartIdentityConflict(
    'posix-ps-lstart:Mon Jul 20 12:34:56 2026',
    'posix-ps-lstart:Tue Jul 21 08:00:00 2026',
  ),
  true,
  'a recycled PID with a different start time must invalidate the record',
)
assert.equal(
  desktopGatewayStartIdentityConflict(
    'linux-proc-start-ticks:100',
    'linux-proc-start-ticks:200',
  ),
  true,
)
assert.equal(
  desktopGatewayStartIdentityConflict(
    'windows-creation-filetime:133700000000000000',
    'windows-creation-filetime:133700000000000001',
  ),
  true,
)
assert.equal(
  desktopGatewayStartIdentityConflict(
    'posix-ps-lstart:Mon Jul 20 12:34:56 2026',
    'posix-ps-lstart:Mon Jul 20 12:34:56 2026',
  ),
  false,
  'a matching start identity keeps the conservative wait',
)
assert.equal(
  desktopGatewayStartIdentityConflict('posix-ps-lstart:Mon Jul 20 12:34:56 2026', null),
  false,
  'an unavailable probe must fail open',
)
assert.equal(
  desktopGatewayStartIdentityConflict(
    'runtime-start:4242:1:abcd',
    'posix-ps-lstart:Mon Jul 20 12:34:56 2026',
  ),
  false,
  'the opaque runtime fallback identity is never comparable',
)
assert.equal(
  desktopGatewayStartIdentityConflict(
    'linux-proc-start-ticks:100',
    'posix-ps-lstart:Mon Jul 20 12:34:56 2026',
  ),
  false,
  'cross-scheme identities are never comparable',
)
assert.equal(
  desktopGatewayStartIdentityConflict(
    'unknown-scheme:1',
    'unknown-scheme:2',
  ),
  false,
  'unknown schemes are never comparable',
)

// The live probe is comparable and stable when the platform can answer. The
// production API deliberately returns null when an OS probe is unavailable,
// and its callers must remain fail-open in that case (notably on a saturated
// Windows CI runner while PowerShell is starting).
{
  const own = desktopProcessStartIdentity(process.pid)
  if (own) {
    assert.match(
      own,
      /^(linux-proc-start-ticks|windows-creation-filetime|posix-ps-lstart):/,
    )
    assert.equal(desktopProcessStartIdentity(process.pid), own)
    assert.equal(desktopGatewayStartIdentityConflict(own, own), false)
  } else {
    assert.equal(
      desktopGatewayStartIdentityConflict('windows-creation-filetime:1', own),
      false,
      'an unavailable platform probe must preserve the conservative path',
    )
  }
}
assert.equal(desktopProcessStartIdentity(0), null)
assert.equal(desktopProcessStartIdentity(-1), null)
assert.equal(desktopProcessStartIdentity(1.5), null)

// --- readiness verification budget: one deadline per exact record instance ---

{
  let now = 0
  let verifyCalls = 0
  let livenessCalls = 0
  let startIdentityCalls = 0
  const coordinator = new DesktopGatewayOwnershipVerificationCoordinator({
    identityReadyTimeoutMs: 100,
    pollIntervalMs: 25,
    now: () => now,
    wait: async (timeoutMs) => {
      now += timeoutMs
    },
    verify: async () => {
      verifyCalls += 1
      return false
    },
    load: () => ({ status: 'valid', record }),
    processMayStillBeAlive: () => {
      livenessCalls += 1
      return true
    },
    processStartIdentity: () => {
      startIdentityCalls += 1
      return null
    },
    startIdentityConflicts: () => false,
  })

  assert.equal(await coordinator.verifyWhenReady('/profile/owner', record), false)
  assert.equal(await coordinator.verifyWhenReady('/profile/owner', record), false)
  assert.equal(await coordinator.verifyWhenReady('/profile/owner', record), false)
  assert.equal(now, 100, 'three sequential startup phases share one total wait budget')
  assert.equal(verifyCalls, 7, 'later phases still perform one fresh identity challenge')
  assert.equal(livenessCalls, 7, 'every failed challenge gets a fresh process liveness probe')
  assert.equal(startIdentityCalls, 3, 'later phases still perform a fresh liveness check')
}

{
  let verifyCalls = 0
  let releaseVerification
  const verificationGate = new Promise((resolve) => {
    releaseVerification = resolve
  })
  const coordinator = new DesktopGatewayOwnershipVerificationCoordinator({
    verify: async () => {
      verifyCalls += 1
      return await verificationGate
    },
    load: () => ({ status: 'missing', record: null }),
  })

  const first = coordinator.verifyWhenReady('/profile/concurrent', record)
  const second = coordinator.verifyWhenReady('/profile/concurrent', record)
  releaseVerification(false)
  assert.deepEqual(await Promise.all([first, second]), [false, false])
  assert.equal(verifyCalls, 1, 'concurrent callers share one in-flight challenge')
}

{
  let recoveryCalls = 0
  let releaseRecovery
  const recoveryGate = new Promise((resolve) => {
    releaseRecovery = resolve
  })
  const coordinator = new DesktopGatewayOwnershipVerificationCoordinator({
    load: () => ({ status: 'valid', record }),
  })
  const recover = async () => {
    recoveryCalls += 1
    await recoveryGate
  }

  const first = coordinator.runRecovery('/profile/recovery', record, recover)
  const second = coordinator.runRecovery('/profile/recovery', record, recover)
  releaseRecovery()
  await Promise.all([first, second])
  assert.equal(recoveryCalls, 1, 'concurrent callers share shutdown and release work')

  await coordinator.runRecovery('/profile/recovery', record, async () => {
    recoveryCalls += 1
  })
  assert.equal(recoveryCalls, 2, 'completed recovery outcomes are never cached as authority')

  await assert.rejects(
    coordinator.runRecovery('/profile/recovery', record, async () => {
      recoveryCalls += 1
      throw new Error('synthetic release failure')
    }),
    /synthetic release failure/,
  )
  await coordinator.runRecovery('/profile/recovery', record, async () => {
    recoveryCalls += 1
  })
  assert.equal(recoveryCalls, 4, 'a failed recovery is retried and never cached as success')
}

{
  const replacement = { ...record, version: '1.2.4' }
  let currentRecord = record
  let activeRecoveries = 0
  let maxActiveRecoveries = 0
  const order = []
  let releaseFirst
  const firstGate = new Promise((resolve) => {
    releaseFirst = resolve
  })
  const coordinator = new DesktopGatewayOwnershipVerificationCoordinator({
    load: () => ({ status: 'valid', record: currentRecord }),
  })
  const recover = async (fresh) => {
    order.push(`${fresh.version}:start`)
    activeRecoveries += 1
    maxActiveRecoveries = Math.max(maxActiveRecoveries, activeRecoveries)
    if (fresh.version === record.version) await firstGate
    activeRecoveries -= 1
    order.push(`${fresh.version}:end`)
  }
  const first = coordinator.runRecovery('/profile/serialized', record, recover)
  currentRecord = replacement
  const second = coordinator.runRecovery('/profile/serialized', replacement, recover)

  await Promise.resolve()
  assert.deepEqual(order, [`${record.version}:start`])
  releaseFirst()
  await Promise.all([first, second])
  assert.equal(maxActiveRecoveries, 1, 'different records in one directory recover serially')
  assert.deepEqual(order, [
    `${record.version}:start`,
    `${record.version}:end`,
    `${replacement.version}:start`,
    `${replacement.version}:end`,
  ])
}

{
  const replacement = { ...record, version: '1.2.5' }
  let currentRecord = record
  let releaseFirst
  const firstGate = new Promise((resolve) => {
    releaseFirst = resolve
  })
  const recoveredVersions = []
  const coordinator = new DesktopGatewayOwnershipVerificationCoordinator({
    load: () => ({ status: 'valid', record: currentRecord }),
  })
  const recover = async (fresh) => {
    recoveredVersions.push(fresh.version)
    if (fresh.version === record.version) {
      await firstGate
      throw new Error('old record failed')
    }
  }
  const first = coordinator.runRecovery('/profile/replaced-error', record, recover)
  currentRecord = replacement
  const second = coordinator.runRecovery('/profile/replaced-error', replacement, recover)

  releaseFirst()
  const outcomes = await Promise.allSettled([first, second])
  assert.equal(outcomes[0].status, 'rejected')
  assert.equal(outcomes[1].status, 'fulfilled')
  assert.deepEqual(
    recoveredVersions,
    [record.version, replacement.version],
    'a replacement record does not inherit the old failure',
  )
}

{
  const replacement = { ...record, version: '1.2.6' }
  let currentRecord = record
  let releaseFirst
  const firstGate = new Promise((resolve) => {
    releaseFirst = resolve
  })
  const recoveredVersions = []
  const coordinator = new DesktopGatewayOwnershipVerificationCoordinator({
    load: () => ({ status: 'valid', record: currentRecord }),
  })
  const recover = async (fresh) => {
    recoveredVersions.push(fresh.version)
    if (fresh.version === record.version) await firstGate
  }

  const leader = coordinator.runRecovery('/profile/replaced-after-join', record, recover)
  const follower = coordinator.runRecovery('/profile/replaced-after-join', record, recover)
  currentRecord = replacement
  releaseFirst()
  await Promise.all([leader, follower])
  assert.deepEqual(
    recoveredVersions,
    [record.version],
    'a same-record recovery never expands its authority to a replacement',
  )
}

{
  let now = 0
  let currentRecord = record
  let verificationReady = false
  let verifyCalls = 0
  const coordinator = new DesktopGatewayOwnershipVerificationCoordinator({
    identityReadyTimeoutMs: 40,
    pollIntervalMs: 20,
    now: () => now,
    wait: async (timeoutMs) => {
      now += timeoutMs
    },
    verify: async () => {
      verifyCalls += 1
      return verificationReady
    },
    load: () => ({ status: 'valid', record: currentRecord }),
    processMayStillBeAlive: () => true,
    processStartIdentity: () => null,
    startIdentityConflicts: () => false,
  })

  assert.equal(await coordinator.verifyWhenReady('/profile/replaced', record), false)
  assert.equal(now, 40)

  verificationReady = true
  assert.equal(
    await coordinator.verifyWhenReady('/profile/replaced', record),
    true,
    'an orphan that becomes ready after the deadline still gets a fresh challenge',
  )
  assert.equal(now, 40, 'the later fresh challenge does not reset the expired budget')

  verificationReady = false
  currentRecord = { ...record, version: '1.2.4' }
  assert.equal(
    await coordinator.verifyWhenReady('/profile/replaced', currentRecord),
    false,
  )
  assert.equal(now, 80, 'a change to any persisted record field receives a new budget')
  assert.ok(verifyCalls >= 7)
}

{
  let now = 0
  let currentRecord = record
  const coordinator = new DesktopGatewayOwnershipVerificationCoordinator({
    identityReadyTimeoutMs: 20,
    pollIntervalMs: 20,
    maxRecordBudgetsPerDirectory: 2,
    now: () => now,
    wait: async (timeoutMs) => {
      now += timeoutMs
    },
    verify: async () => false,
    load: () => ({ status: 'valid', record: currentRecord }),
    processMayStillBeAlive: () => true,
    processStartIdentity: () => null,
    startIdentityConflicts: () => false,
  })

  for (const version of ['1.0.0', '1.0.1']) {
    currentRecord = { ...record, version }
    assert.equal(
      await coordinator.verifyWhenReady('/profile/churn', currentRecord),
      false,
    )
  }
  assert.equal(now, 40)
  currentRecord = { ...record, version: '1.0.2' }
  assert.equal(await coordinator.verifyWhenReady('/profile/churn', currentRecord), false)
  assert.equal(now, 40, 'abnormal record churn cannot allocate unbounded wait budgets')
}

console.log('desktop gateway ownership checks passed')
