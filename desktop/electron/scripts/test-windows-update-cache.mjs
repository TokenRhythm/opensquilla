import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { mkdtemp, readFile, realpath, rm, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import {
  WINDOWS_UPDATE_CACHE_DESCRIPTOR,
  createWindowsUpdateCacheDescriptor,
  loadWindowsUpdateCache,
  saveWindowsUpdateCache,
  verifyCachedInstaller,
} from '../dist/windows-update-cache.js'
import { WindowsUpdateSecurityError } from '../dist/windows-update-security.js'

const directory = await realpath(await mkdtemp(join(tmpdir(), 'opensquilla-update-cache-')))
try {
  const candidate = { tag: 'v0.5.5', version: '0.5.5', installer: 'OpenSquilla-0.5.5-win-x64.exe' }
  const bytes = Buffer.from('synthetic installer bytes, never executed')
  const digest = createHash('sha256').update(bytes).digest('hex')
  const descriptor = createWindowsUpdateCacheDescriptor(candidate, digest, bytes.length)
  const path = join(directory, candidate.installer)
  let checks = 0
  const options = { verifySignature: async (file) => { assert.equal(file, path); checks += 1 } }
  assert.equal(await loadWindowsUpdateCache(directory), null)
  assert.equal(await verifyCachedInstaller(directory, descriptor, '0.5.4', options), null)
  await writeFile(path, bytes)
  await saveWindowsUpdateCache(directory, { ...descriptor, path: 'C:\\untrusted.exe' })
  assert.deepEqual(await loadWindowsUpdateCache(directory), descriptor, 'absolute caller paths must not persist')
  await saveWindowsUpdateCache(directory, descriptor)
  const result = await verifyCachedInstaller(directory, await loadWindowsUpdateCache(directory), '0.5.4', options)
  assert.equal(result.path, path)
  assert.deepEqual(result.candidate, { ...candidate, baseVersion: '0.5.5', prerelease: false,
    releaseUrl: 'https://github.com/TokenRhythm/opensquilla/releases/tag/v0.5.5', feed: 'latest.yml' })
  assert.equal(checks, 1)
  for (const invalid of [
    { ...descriptor, installer: '..\\other.exe' }, { ...descriptor, installer: 'C:\\other.exe' },
    { ...descriptor, tag: 'v0.5.6' }, { ...descriptor, version: '0.05.5' },
    { ...descriptor, bytes: 0 }, { ...descriptor, sha256: '0'.repeat(64) },
    { ...descriptor, bytes: bytes.length + 1 }, { ...descriptor, schemaVersion: 2 },
  ]) assert.equal(await verifyCachedInstaller(directory, invalid, '0.5.4', options), null)
  assert.equal(checks, 1, 'invalid cache entries must not reach the signature checker')
  assert.equal(await verifyCachedInstaller(directory, descriptor, '0.5.5', options), null)
  assert.equal(await verifyCachedInstaller(directory, descriptor, '0.5.6', options), null)
  assert.equal(await verifyCachedInstaller(directory, descriptor, '0.5.4-rc9', options), null, 'preview must stay on its base')
  assert.equal(await verifyCachedInstaller(directory, descriptor, '0.5.4', {
    ...options, expectedCandidate: { ...candidate, tag: 'v0.5.6' },
  }), null)
  assert.equal(await verifyCachedInstaller(directory, descriptor, '0.5.4', {
    ...options, expectedSha256: '0'.repeat(64),
  }), null)
  for (const code of ['signature_invalid', 'signature_unavailable']) {
    await assert.rejects(verifyCachedInstaller(directory, descriptor, '0.5.4', {
      verifySignature: async () => { throw new WindowsUpdateSecurityError(code, 'synthetic verification failure') },
    }), (error) => error.code === code)
  }
  await writeFile(path, Buffer.alloc(bytes.length, 1))
  assert.equal(await verifyCachedInstaller(directory, descriptor, '0.5.4', options), null)
  await writeFile(path, bytes)
  assert.equal(await verifyCachedInstaller(directory, descriptor, '0.5.4', {
    verifySignature: async () => { await writeFile(path, 'changed during signature verification') },
  }), null)

  const previewCandidate = { tag: 'v0.5.5rc10', version: '0.5.5-rc10', installer: 'OpenSquilla-0.5.5-rc10-win-x64.exe' }
  const preview = createWindowsUpdateCacheDescriptor(previewCandidate, digest, bytes.length)
  await writeFile(join(directory, preview.installer), bytes)
  const previewOptions = { verifySignature: async () => {} }
  assert.ok(await verifyCachedInstaller(directory, preview, '0.5.5-rc9', previewOptions))
  assert.equal(await verifyCachedInstaller(directory, preview, '0.5.5-rc10', previewOptions), null)
  assert.equal(await verifyCachedInstaller(directory, preview, '0.5.4', previewOptions), null)
  await writeFile(join(directory, WINDOWS_UPDATE_CACHE_DESCRIPTOR), '{broken')
  assert.equal(await loadWindowsUpdateCache(directory), null)
  await saveWindowsUpdateCache(directory, null)
  assert.equal(await loadWindowsUpdateCache(directory), null)
  assert.ok((await readFile(join(directory, preview.installer))).equals(bytes), 'clearing metadata must preserve installer files')
} finally {
  await rm(directory, { recursive: true, force: true })
}
console.log('Windows installer cache checks passed (synthetic files; no installer executed).')
