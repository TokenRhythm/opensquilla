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
  waitForDesktopGatewayOwnershipRelease,
} from '../dist/desktop-gateway-ownership.js'

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

// The live probe answers for this very process with a comparable scheme, is
// stable across calls, and never conflicts with itself.
{
  const own = desktopProcessStartIdentity(process.pid)
  assert.ok(own, 'the platform probe should answer for the current process')
  assert.match(
    own,
    /^(linux-proc-start-ticks|windows-creation-filetime|posix-ps-lstart):/,
  )
  assert.equal(desktopProcessStartIdentity(process.pid), own)
  assert.equal(desktopGatewayStartIdentityConflict(own, own), false)
}
assert.equal(desktopProcessStartIdentity(0), null)
assert.equal(desktopProcessStartIdentity(-1), null)
assert.equal(desktopProcessStartIdentity(1.5), null)

console.log('desktop gateway ownership checks passed')
