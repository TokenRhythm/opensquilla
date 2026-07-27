import assert from 'node:assert/strict'
import {
  mkdirSync,
  mkdtempSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import {
  allProfileContexts,
  isRecoveryProfileId,
  primaryProfilePaths,
  recoveryProfilePaths,
} from '../dist/desktop-profile-context.js'

const root = mkdtempSync(join(tmpdir(), 'opensquilla-profile-consolidation-'))
try {
  const primary = primaryProfilePaths(root)
  assert.deepEqual(primary, {
    kind: 'primary',
    recoveryId: null,
    home: join(root, 'opensquilla'),
    credentialPath: join(root, 'desktop-credential.json'),
    logsDir: join(root, 'logs'),
  })

  const firstRecoveryId = '11234567-89ab-4cde-8fab-0123456789ab'
  const secondRecoveryId = '21234567-89ab-4cde-8fab-0123456789ab'
  assert.equal(isRecoveryProfileId(firstRecoveryId), true)
  assert.equal(isRecoveryProfileId('not-a-profile-id'), false)

  for (const recoveryId of [secondRecoveryId, firstRecoveryId]) {
    const profile = recoveryProfilePaths(root, recoveryId)
    mkdirSync(profile.home, { recursive: true })
    writeFileSync(join(profile.home, 'config.toml'), `[profile]\nid = "${recoveryId}"\n`)
  }
  mkdirSync(join(root, 'recovery-profiles', 'malformed-id', 'opensquilla'), {
    recursive: true,
  })

  const outside = join(root, 'outside')
  mkdirSync(join(outside, 'opensquilla'), { recursive: true })
  const unsafeRecoveryId = '31234567-89ab-4cde-8fab-0123456789ab'
  symlinkSync(
    outside,
    join(root, 'recovery-profiles', unsafeRecoveryId),
    process.platform === 'win32' ? 'junction' : 'dir',
  )

  const profiles = allProfileContexts(root)
  assert.deepEqual(
    profiles.map((profile) => [profile.kind, profile.recoveryId]),
    [
      ['primary', null],
      ['recovery', firstRecoveryId],
      ['recovery', secondRecoveryId],
    ],
  )
  assert.equal(
    profiles.some((profile) => profile.recoveryId === unsafeRecoveryId),
    false,
    'legacy discovery must ignore linked profile roots instead of traversing them',
  )

  assert.throws(
    () => recoveryProfilePaths(root, '../escape'),
    /Invalid recovery profile id/,
  )
  assert.equal(
    allProfileContexts(join(root, 'missing-user-data')).length,
    1,
    'a missing legacy container still yields the single primary profile',
  )
  console.log(JSON.stringify({
    ok: true,
    primaryOnlyRuntime: true,
    safeLegacyProfiles: profiles.length - 1,
  }))
} finally {
  rmSync(root, { recursive: true, force: true })
}
