import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { mkdtemp, readFile, readdir, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { GITHUB_UPDATE_OWNER, parseOpenSquillaReleaseTag } from '../dist/update-feed-resolver.js'
import {
  candidateFromUpdateChannel,
  orderedUpdateSources,
  updateAssetUrl,
  updateChannelManifestFromReleaseInventory,
  updateChannelPathForVersion,
  updateFeedBaseUrl,
  validateUpdateChannelManifest,
  UPDATE_GITHUB_RELEASES_API_URL,
} from '../dist/update-channel.js'
import {
  parseSha256SumsForAsset,
  readResponseTextWithLimit,
  streamResponseToVerifiedFile,
} from '../dist/update-verification.js'

// This exercises the resolver shipped after Preview 2. It proves that clients
// containing this resolver can select later releases; it does not prove that
// the already-published Preview 1/2 binaries can discover Preview 3.

// --- tag parsing: PEP440 rc, semver rc, stable, and rejects ---
assert.deepEqual(parseOpenSquillaReleaseTag('v0.5.0rc2'), { base: '0.5.0', rc: 2 })
assert.deepEqual(parseOpenSquillaReleaseTag('0.5.0-rc2'), { base: '0.5.0', rc: 2 })
assert.deepEqual(parseOpenSquillaReleaseTag('v0.5.0-rc.3'), { base: '0.5.0', rc: 3 })
assert.deepEqual(parseOpenSquillaReleaseTag('v0.5.0'), { base: '0.5.0', rc: null })
assert.equal(parseOpenSquillaReleaseTag('website-2026-01'), null)
assert.equal(parseOpenSquillaReleaseTag('v0.5'), null)

const channelManifest = (tag, version, prerelease = true) => ({
  schemaVersion: 1,
  tag,
  version,
  baseVersion: version.replace(/-rc\d+$/, ''),
  prerelease,
  publishedAt: '2026-07-15T00:00:00Z',
  releaseUrl: `https://github.com/opensquilla/opensquilla/releases/tag/${tag}`,
  sha256sums: 'SHA256SUMS',
  platforms: {
    'darwin-arm64': {
      feed: 'latest-mac.yml',
      archive: `OpenSquilla-${version}-mac-arm64.zip`,
      installer: `OpenSquilla-${version}-mac-arm64.dmg`,
    },
    'win32-x64': {
      feed: 'latest.yml',
      installer: `OpenSquilla-${version}-win-x64.exe`,
    },
  },
})

assert.equal(updateChannelPathForVersion('0.5.0-rc4'), 'preview/0.5.0.json')
assert.equal(updateChannelPathForVersion('0.5.0'), 'stable.json')
assert.equal(updateChannelPathForVersion('not-a-version'), null)

// Existing v1 mirrors retain the old URL; new clients display and download from
// the current repository while continuing to accept either exact spelling.
assert.equal(GITHUB_UPDATE_OWNER, 'TokenRhythm')
assert.equal(
  UPDATE_GITHUB_RELEASES_API_URL,
  'https://api.github.com/repos/TokenRhythm/opensquilla/releases?per_page=100',
)
for (const owner of ['opensquilla', 'TokenRhythm']) {
  const manifest = channelManifest('v0.5.5', '0.5.5', false)
  manifest.releaseUrl = `https://github.com/${owner}/opensquilla/releases/tag/v0.5.5`
  for (const platform of ['darwin-arm64', 'win32-x64']) {
    for (const currentVersion of ['0.5.3', '0.5.4']) {
      const candidate = candidateFromUpdateChannel(currentVersion, manifest, platform)
      assert.equal(candidate?.version, '0.5.5')
      assert.equal(candidate?.releaseUrl, 'https://github.com/TokenRhythm/opensquilla/releases/tag/v0.5.5')
      assert.equal(updateFeedBaseUrl(candidate, 'github'), 'https://github.com/TokenRhythm/opensquilla/releases/download/v0.5.5')
      assert.equal(updateFeedBaseUrl(candidate, 'oss'), 'https://opensquilla-releases.oss-cn-beijing.aliyuncs.com/releases/v0.5.5')
    }
  }
  const preview = channelManifest('v0.5.5rc1', '0.5.5-rc1')
  preview.releaseUrl = `https://github.com/${owner}/opensquilla/releases/tag/v0.5.5rc1`
  assert.equal(candidateFromUpdateChannel('0.5.4', preview, 'darwin-arm64'), null)
}
for (const releaseUrl of [
  'https://github.com/TokenRhythm/another-repo/releases/tag/v0.5.5',
  'https://github.com/another-owner/opensquilla/releases/tag/v0.5.5',
  'https://github.com/TokenRhythm/opensquilla/releases/tag/v0.5.4',
  'https://github.com/TokenRhythm/opensquilla/releases/tag/v0.5.5?download=1',
  'https://github.com/TokenRhythm/opensquilla/releases/tag/v0.5.5/extra',
  'https://github.com.example.test/TokenRhythm/opensquilla/releases/tag/v0.5.5',
]) {
  assert.throws(
    () => validateUpdateChannelManifest({ ...channelManifest('v0.5.5', '0.5.5', false), releaseUrl }),
    /canonical/,
  )
}

{
  const manifest = channelManifest('v0.5.0rc5', '0.5.0-rc5')
  assert.equal(validateUpdateChannelManifest(manifest).tag, 'v0.5.0rc5')
  const mac = candidateFromUpdateChannel('0.5.0-rc4', manifest, 'darwin-arm64')
  assert.ok(mac)
  assert.equal(mac.version, '0.5.0-rc5')
  assert.equal(
    updateFeedBaseUrl(mac, 'oss'),
    'https://opensquilla-releases.oss-cn-beijing.aliyuncs.com/releases/v0.5.0rc5',
  )
  assert.equal(
    updateAssetUrl(mac, 'github'),
    'https://github.com/TokenRhythm/opensquilla/releases/download/v0.5.0rc5/OpenSquilla-0.5.0-rc5-mac-arm64.dmg',
  )
  const win = candidateFromUpdateChannel('0.5.0-rc4', manifest, 'win32-x64')
  assert.equal(win?.installer, 'OpenSquilla-0.5.0-rc5-win-x64.exe')
}

assert.equal(
  candidateFromUpdateChannel(
    '0.5.0-rc5',
    channelManifest('v0.5.0rc4', '0.5.0-rc4'),
    'darwin-arm64',
  ),
  null,
)
assert.equal(
  candidateFromUpdateChannel(
    '0.5.0-rc4',
    channelManifest('v0.5.0', '0.5.0', false),
    'darwin-arm64',
  )?.version,
  '0.5.0',
)
assert.equal(
  candidateFromUpdateChannel(
    '0.5.0-rc4',
    channelManifest('v0.6.0rc1', '0.6.0-rc1'),
    'darwin-arm64',
  ),
  null,
)

assert.deepEqual(orderedUpdateSources(['zh-CN']), ['oss', 'github'])
assert.deepEqual(orderedUpdateSources(['zh-Hans']), ['oss', 'github'])
assert.deepEqual(orderedUpdateSources(['en-US']), ['github', 'oss'])
assert.deepEqual(orderedUpdateSources(['zh-SG']), ['github', 'oss'])
assert.deepEqual(orderedUpdateSources(['zh-Hant']), ['github', 'oss'])
assert.deepEqual(orderedUpdateSources(['zh-TW']), ['github', 'oss'])
assert.deepEqual(orderedUpdateSources(['en-US'], 'oss'), ['oss', 'github'])
assert.deepEqual(orderedUpdateSources(['zh-CN'], null, 'global'), ['github', 'oss'])

assert.throws(
  () => validateUpdateChannelManifest({ ...channelManifest('v0.5.0rc5', '0.5.0-rc5'), schemaVersion: 2 }),
  /schemaVersion/,
)
assert.throws(
  () => validateUpdateChannelManifest({
    ...channelManifest('v0.5.0rc5', '0.5.0-rc5'),
    releaseUrl: 'https://example.test/update',
  }),
  /canonical/,
)
assert.throws(
  () => validateUpdateChannelManifest({
    ...channelManifest('v0.5.0rc5', '0.5.0-rc5'),
    sha256sums: 'checksums.txt',
  }),
  /sha256sums/,
)
assert.throws(
  () => validateUpdateChannelManifest({
    ...channelManifest('v0.5.0rc5', '0.5.0-rc5'),
    publishedAt: 'July 15, 2026',
  }),
  /publishedAt/,
)
assert.throws(
  () => validateUpdateChannelManifest({
    ...channelManifest('v0.5.0rc5', '0.5.0-rc5'),
    publishedAt: '2026-02-30T00:00:00Z',
  }),
  /publishedAt/,
)
assert.throws(
  () => validateUpdateChannelManifest({
    ...channelManifest('v0.5.0rc5', '0.5.0-rc5'),
    tag: 'v0.5.0-rc.5',
    releaseUrl: 'https://github.com/opensquilla/opensquilla/releases/tag/v0.5.0-rc.5',
  }),
  /canonical/,
)
{
  const manifest = channelManifest('v0.5.0rc5', '0.5.0-rc5')
  manifest.platforms['win32-x64'].installer = 'OpenSquilla-0.5.0-rc4-win-x64.exe'
  assert.throws(() => validateUpdateChannelManifest(manifest), /platform assets/)
}
{
  const manifest = channelManifest('v0.5.0rc5', '0.5.0-rc5')
  manifest.platforms['darwin-arm64'].feed = 'custom.yml'
  assert.throws(() => validateUpdateChannelManifest(manifest), /platform assets/)
}

// Windows mirror downloads are trusted only after their bytes match the exact
// asset entry in the canonical SHA256SUMS file mirrored to OSS.
{
  const asset = 'OpenSquilla-0.5.0-rc5-win-x64.exe'
  const bytes = Buffer.from('verified windows installer bytes')
  const expected = createHash('sha256').update(bytes).digest('hex')
  const sums = `${'a'.repeat(64)}  another-asset.zip\n${expected.toUpperCase()} *${asset}\n`
  assert.equal(parseSha256SumsForAsset(sums, asset), expected)
  assert.throws(
    () => parseSha256SumsForAsset(`${expected}  ${asset}\n${expected} *${asset}\n`, asset),
    /more than once/,
  )
  assert.throws(() => parseSha256SumsForAsset(`${expected}  other.exe\n`, asset), /does not list/)
  assert.throws(() => parseSha256SumsForAsset('not a checksum line\n', asset), /malformed/)

  const root = await mkdtemp(join(tmpdir(), 'opensquilla-update-verification-'))
  const destination = join(root, asset)
  try {
    const checksumText = await readResponseTextWithLimit(
      new Response(`${expected}  ${asset}\n`),
      1024,
    )
    assert.equal(parseSha256SumsForAsset(checksumText, asset), expected)

    const verified = await streamResponseToVerifiedFile(
      new Response(bytes, { headers: { 'Content-Length': String(bytes.length) } }),
      destination,
      expected,
      { maxBytes: 1024 },
    )
    assert.equal(verified.sha256, expected)
    assert.deepEqual(await readFile(destination), bytes)

    const previous = Buffer.from('previous verified installer')
    await writeFile(destination, previous)
    await assert.rejects(
      streamResponseToVerifiedFile(
        new Response(Buffer.from('tampered mirror bytes')),
        destination,
        expected,
        { maxBytes: 1024 },
      ),
      (err) => err?.code === 'integrity_failed',
    )
    assert.deepEqual(await readFile(destination), previous, 'hash mismatch must preserve the old verified file')
    assert.deepEqual(await readdir(root), [asset], 'hash mismatch must remove partial downloads')

    await assert.rejects(
      streamResponseToVerifiedFile(
        new Response(Buffer.from('short'), { headers: { 'Content-Length': '10' } }),
        join(root, 'truncated.exe'),
        createHash('sha256').update('short').digest('hex'),
        { maxBytes: 1024 },
      ),
      (err) => err?.code === 'download_failed' && /truncated/.test(err.message),
    )
    assert.equal((await readdir(root)).includes('truncated.exe'), false)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
}

// --- GitHub release-inventory discovery: the second channel-manifest source ---

const releaseAssets = (version) => [
  { name: 'SHA256SUMS' },
  { name: 'latest-mac.yml' },
  { name: 'latest.yml' },
  { name: `OpenSquilla-${version}-mac-arm64.zip` },
  { name: `OpenSquilla-${version}-mac-arm64.dmg` },
  { name: `OpenSquilla-${version}-win-x64.exe` },
]
const inventoryRelease = (tag, version, overrides = {}) => ({
  tag_name: tag,
  draft: false,
  prerelease: version.includes('-rc'),
  published_at: '2026-07-15T00:00:00Z',
  assets: releaseAssets(version),
  ...overrides,
})

// A stable install derives the stable-channel head from the release listing;
// prereleases, drafts, and newer bases with incomplete assets never advance it.
{
  const manifest = updateChannelManifestFromReleaseInventory('0.5.0', [
    inventoryRelease('v0.5.2rc1', '0.5.2-rc1'),
    { ...inventoryRelease('v0.5.2', '0.5.2'), draft: true },
    inventoryRelease('v0.5.1', '0.5.1'),
    inventoryRelease('v0.5.0', '0.5.0'),
  ])
  assert.ok(manifest, 'stable channel head should be derived from the inventory')
  assert.equal(manifest.tag, 'v0.5.1')
  assert.equal(manifest.version, '0.5.1')
  assert.equal(manifest.prerelease, false)
  // The synthesized manifest satisfies the exact same contract as a mirrored one.
  assert.deepEqual(validateUpdateChannelManifest(manifest), manifest)
  const candidate = candidateFromUpdateChannel('0.5.0', manifest, 'win32-x64')
  assert.equal(candidate?.installer, 'OpenSquilla-0.5.1-win-x64.exe')
  assert.equal(
    updateAssetUrl(candidate, 'github'),
    'https://github.com/TokenRhythm/opensquilla/releases/download/v0.5.1/OpenSquilla-0.5.1-win-x64.exe',
  )
}

// A head release missing a required desktop asset is skipped in favor of the
// highest complete release.
{
  const incomplete = inventoryRelease('v0.5.2', '0.5.2')
  incomplete.assets = incomplete.assets.filter(
    (asset) => asset.name !== 'OpenSquilla-0.5.2-win-x64.exe',
  )
  const manifest = updateChannelManifestFromReleaseInventory('0.5.0', [
    incomplete,
    inventoryRelease('v0.5.1', '0.5.1'),
  ])
  assert.equal(manifest?.tag, 'v0.5.1')
}

// A preview install tracks its own release line: a later rc wins, the final
// stable of the same base outranks any rc, and other bases are ignored.
{
  const manifest = updateChannelManifestFromReleaseInventory('0.5.0-rc4', [
    inventoryRelease('v0.6.0rc1', '0.6.0-rc1'),
    inventoryRelease('v0.5.0rc5', '0.5.0-rc5'),
    inventoryRelease('v0.5.0rc4', '0.5.0-rc4'),
  ])
  assert.equal(manifest?.tag, 'v0.5.0rc5')
  assert.equal(candidateFromUpdateChannel('0.5.0-rc4', manifest, 'darwin-arm64')?.version, '0.5.0-rc5')
}
{
  const manifest = updateChannelManifestFromReleaseInventory('0.5.0-rc4', [
    inventoryRelease('v0.5.0', '0.5.0'),
    inventoryRelease('v0.5.0rc5', '0.5.0-rc5'),
  ])
  assert.equal(manifest?.tag, 'v0.5.0')
  assert.equal(manifest.prerelease, false)
}

// Two-digit rc ordering is numeric throughout the active update-channel path:
// rc10 is selected ahead of rc9 and is offered as an upgrade from rc9.
{
  const manifest = updateChannelManifestFromReleaseInventory('0.5.0-rc9', [
    inventoryRelease('v0.5.0rc9', '0.5.0-rc9'),
    inventoryRelease('v0.5.0rc10', '0.5.0-rc10'),
  ])
  assert.equal(manifest?.tag, 'v0.5.0rc10')
  assert.equal(
    candidateFromUpdateChannel('0.5.0-rc9', manifest, 'darwin-arm64')?.version,
    '0.5.0-rc10',
  )
  assert.equal(
    candidateFromUpdateChannel('0.5.0-rc10', manifest, 'darwin-arm64'),
    null,
    'rc10 must not move sideways or regress to rc9',
  )
}

// The channel head may equal the running version; the candidate gate then
// reports "up to date" instead of offering a sideways move.
{
  const manifest = updateChannelManifestFromReleaseInventory('0.5.0-rc4', [
    inventoryRelease('v0.5.0rc4', '0.5.0-rc4'),
  ])
  assert.equal(manifest?.tag, 'v0.5.0rc4')
  assert.equal(candidateFromUpdateChannel('0.5.0-rc4', manifest, 'darwin-arm64'), null)
}

// Non-canonical tag spellings and disagreeing prerelease flags do not
// participate: URLs and the manifest contract must agree on the exact tag.
{
  const manifest = updateChannelManifestFromReleaseInventory('0.5.0-rc4', [
    inventoryRelease('v0.5.0-rc5', '0.5.0-rc5'),
    inventoryRelease('v0.5.0rc5', '0.5.0-rc5', { prerelease: false }),
    inventoryRelease('v0.5.0rc5', '0.5.0-rc5', { published_at: 'July 15, 2026' }),
  ])
  assert.equal(manifest, null)
}

// Nothing eligible → null; malformed inventory or version → error/null.
assert.equal(updateChannelManifestFromReleaseInventory('0.5.0', []), null)
assert.equal(
  updateChannelManifestFromReleaseInventory('0.5.0', [inventoryRelease('v0.6.0rc1', '0.6.0-rc1')]),
  null,
)
assert.equal(updateChannelManifestFromReleaseInventory('not-a-version', []), null)
assert.throws(
  () => updateChannelManifestFromReleaseInventory('0.5.0', { message: 'rate limited' }),
  (err) => err?.code === 'manifest_invalid',
)

console.log('Update resolver tests passed.')
