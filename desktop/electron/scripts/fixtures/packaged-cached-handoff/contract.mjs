import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { constants, createReadStream } from 'node:fs'
import { copyFile, lstat, mkdir, readFile, realpath } from 'node:fs/promises'
import { basename, isAbsolute, join, relative, resolve, sep } from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'
import { candidateFromUpdateChannel } from '../../../dist/update-channel.js'
import { createWindowsUpdateCacheDescriptor, saveWindowsUpdateCache, verifyCachedInstaller } from '../../../dist/windows-update-cache.js'

export const CACHE_AUDIT_MARKER = 'cached-handoff-audit.json'
export const CACHE_AUDIT_PURPOSE = 'opensquilla-synthetic-cached-handoff-audit'
const fullSha = /^[a-f0-9]{40}$/
const sha256 = /^[a-f0-9]{64}$/
const stable = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/
const parseJson = bytes => JSON.parse(bytes.toString('utf8').replace(/^\uFEFF/, ''))
const samePath = (left, right) => process.platform === 'win32'
  ? resolve(left).toLowerCase() === resolve(right).toLowerCase()
  : resolve(left) === resolve(right)

export function bytesSha256(bytes) { return createHash('sha256').update(bytes).digest('hex') }
export async function fileSha256(path) {
  const digest = createHash('sha256')
  for await (const chunk of createReadStream(path)) digest.update(chunk)
  return digest.digest('hex')
}

export async function canonicalInput(path, directory = false) {
  assert.ok(typeof path === 'string' && isAbsolute(path), 'Audit paths must be explicit and absolute')
  const info = await lstat(path)
  assert.ok(!info.isSymbolicLink() && (directory ? info.isDirectory() : info.isFile()), 'Audit input must be an ordinary file or directory')
  assert.ok(samePath(await realpath(path), path), 'Audit input must not traverse redirected paths')
  return info
}

export function validateCachedCandidate(input, manifest) {
  assert.ok(stable.test(input.baselineVersion) && !['0.5.3', '0.5.4'].includes(input.baselineVersion), 'Cached handoff requires a new stable A')
  assert.ok(stable.test(input.expectedVersion) && sha256.test(input.expectedSha256), 'Candidate version and SHA256 must be pinned')
  assert.ok(fullSha.test(input.sourceSha) && fullSha.test(input.baselineSourceSha), 'Both source SHAs must be pinned')
  const candidate = candidateFromUpdateChannel(input.baselineVersion, manifest, 'win32-x64')
  assert.ok(candidate && candidate.version === input.expectedVersion, 'Production channel rules must select the pinned forward candidate B')
  assert.equal(basename(input.installerPath), candidate.installer, 'Use the canonical signed installer filename')
  return candidate
}

export async function verifyCachedHandoffInputs(input) {
  await canonicalInput(input.manifestPath)
  const candidate = validateCachedCandidate(input, parseJson(await readFile(input.manifestPath)))
  await canonicalInput(input.userDataDir, true)
  const markerPath = join(input.userDataDir, CACHE_AUDIT_MARKER)
  await canonicalInput(markerPath)
  const markerBytes = await readFile(markerPath)
  const marker = parseJson(markerBytes)
  assert.equal(marker.schemaVersion, 1)
  assert.equal(marker.purpose, CACHE_AUDIT_PURPOSE)
  assert.match(marker.auditId ?? '', /^[a-f0-9]{32}$/)
  assert.equal(marker.seedLabel, 'signed-update-audit')
  assert.ok(samePath(marker.userDataDir, input.userDataDir), 'Marker must bind this synthetic native profile')
  for (const name of ['baselineVersion', 'expectedVersion', 'expectedSha256', 'sourceSha', 'baselineSourceSha']) {
    assert.equal(marker[name], input[name], `Marker must pin ${name}`)
  }
  const configPath = join(input.userDataDir, 'opensquilla', 'config.toml')
  await canonicalInput(configPath)
  assert.match(marker.configSha256 ?? '', sha256)
  assert.equal(await fileSha256(configPath), marker.configSha256, 'Seed config changed before staging')
  const info = await canonicalInput(input.installerPath)
  const descriptor = createWindowsUpdateCacheDescriptor(candidate, input.expectedSha256, info.size)
  assert.equal(await fileSha256(input.installerPath), input.expectedSha256, 'Input installer must match the approved Actions artifact')
  const cacheDirectory = join(input.userDataDir, 'update-downloads')
  const suffix = relative(input.userDataDir, cacheDirectory)
  assert.equal(suffix, 'update-downloads')
  assert.ok(!suffix.includes(sep))
  return { ...input, candidate, descriptor, cacheDirectory, markerSha256: bytesSha256(markerBytes), auditId: marker.auditId }
}

export async function copyCachedHandoffInstaller(plan) {
  // A fresh audit owns this new directory. Never merge with or overwrite a cache.
  await canonicalInput(plan.userDataDir, true)
  assert.ok(samePath(plan.cacheDirectory, join(plan.userDataDir, 'update-downloads')))
  await mkdir(plan.cacheDirectory)
  await canonicalInput(plan.cacheDirectory, true)
  const path = join(plan.cacheDirectory, plan.candidate.installer)
  await copyFile(plan.installerPath, path, constants.COPYFILE_EXCL)
  const info = await canonicalInput(path)
  assert.equal(info.size, plan.descriptor.bytes)
  assert.equal(await fileSha256(path), plan.descriptor.sha256, 'Staged bytes differ from the approved B artifact')
  return path
}

export async function stageVerifiedCachedHandoff(input) {
  const plan = await verifyCachedHandoffInputs(input)
  const path = await copyCachedHandoffInstaller(plan)
  // No verifier injection exists in this native harness. This invokes the real
  // Windows PowerShell trust/identity/timestamp checks on the copied installer.
  const verified = await verifyCachedInstaller(plan.cacheDirectory, plan.descriptor, plan.baselineVersion, {
    expectedCandidate: plan.candidate, expectedSha256: plan.expectedSha256,
  })
  assert.ok(verified && samePath(verified.path, path), 'The staged signed installer must pass the production verifier')
  await saveWindowsUpdateCache(plan.cacheDirectory, verified.descriptor)
  return {
    auditId: plan.auditId, markerSha256: plan.markerSha256,
    inputMode: 'verified-cache', fixtureSource: 'local Actions artifact and local channel fixture',
    candidateValidation: 'production-parser-on-local-fixture',
    downloadVerified: false, remotePublicationVerified: false,
    baselineSourceSha: plan.baselineSourceSha, sourceSha: plan.sourceSha,
    installerSha256: plan.expectedSha256, installerBytes: plan.descriptor.bytes,
    manifestSha256: await fileSha256(plan.manifestPath),
    descriptorSha256: await fileSha256(join(plan.cacheDirectory, 'windows-update-cache.json')),
    cachePath: path, cacheStagedAndVerified: true,
  }
}

export function assertCachedRestartEvidence(first, second, credentialBefore, credentialAfter) {
  for (const cycle of [first, second]) {
    assert.ok(Number.isSafeInteger(cycle.electronPid) && cycle.electronPid > 0)
    assert.ok(cycle.gatewayStartIdentity, 'Actual owned Gateway start identity is required')
    assert.equal(cycle.cacheRestored, true)
  }
  assert.equal(first.normalQuitVerified, true, 'A must quit naturally before the cache restart')
  assert.notEqual(first.electronPid, second.electronPid, 'Restart must observe a new actual Electron PID')
  assert.notEqual(first.gatewayStartIdentity, second.gatewayStartIdentity, 'Restart must observe a new owned Gateway identity')
  assert.match(credentialBefore, sha256)
  assert.equal(credentialAfter, credentialBefore, 'Restart must retain the original synthetic credential bytes')
}

export async function waitForRestoredCache(readState, { timeoutMs = 60_000, intervalMs = 250 } = {}) {
  const deadline = Date.now() + timeoutMs
  let lastState
  while (Date.now() < deadline) {
    const controller = new AbortController()
    try {
      lastState = await Promise.race([
        Promise.resolve().then(readState),
        delay(Math.max(0, deadline - Date.now()), undefined, { signal: controller.signal }).then(() => {
          throw new Error(`Cache state observation timed out: ${JSON.stringify(lastState)}`)
        }),
      ])
    } finally { controller.abort() }
    if (lastState?.status === 'error' || ['signature_invalid', 'signature_unavailable', 'integrity_failed'].includes(lastState?.errorCode)) {
      throw new Error(`Actual cached installer verification failed: ${JSON.stringify(lastState)}`)
    }
    if (lastState?.status === 'downloaded') return lastState
    await delay(Math.min(intervalMs, Math.max(0, deadline - Date.now())))
  }
  throw new Error(`Cache was not restored within ${timeoutMs}ms: ${JSON.stringify(lastState)}`)
}

export async function requestCachedQuitOnce(state, quit) {
  if (state.requested) return false
  // The request remains fenced even if the real Quit rejects or times out.
  state.requested = true
  await quit()
  return true
}
