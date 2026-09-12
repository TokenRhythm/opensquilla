import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { mkdtemp, readFile, readdir, realpath, rm, stat, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, relative, resolve } from 'node:path'
import vm from 'node:vm'
import ts from 'typescript'
import { DesktopWriterAdmission } from '../dist/desktop-writer-admission.js'
import { UpdateCheckScheduler, isUpdateCheckAllowed } from '../dist/update-check-scheduler.js'
import {
  UpdateChannelError, candidateFromUpdateChannel, orderedUpdateSources, updateAssetUrl,
  updateChannelManifestFromReleaseInventory, updateChannelManifestUrl, updateChannelPathForVersion,
  UPDATE_GITHUB_RELEASES_API_URL, UPDATE_OSS_RELEASE_ROOT,
} from '../dist/update-channel.js'
import { parseSha256SumsForAsset, readResponseTextWithLimit, streamResponseToVerifiedFile } from '../dist/update-verification.js'
import { WindowsUpdateSecurityError } from '../dist/windows-update-security.js'
import { WindowsUpdateHandoffError } from '../dist/windows-update-handoff.js'
import {
  createWindowsUpdateCacheDescriptor, loadWindowsUpdateCache, saveWindowsUpdateCache, verifyCachedInstaller,
} from '../dist/windows-update-cache.js'

// The actual main-process discovery, transport selection, checksum retry,
// download, scheduler and state functions run unchanged. Fetch is a closed
// synthetic router: it never invokes a real network API. SHA256 streaming and
// cache verification use the real modules and isolated files. Authenticode is
// an explicit seam; these tests make no native certificate-network, packaged
// upgrade or installation claim, and never launch an installer or PowerShell.
const source = await readFile(new URL('../src/main.ts', import.meta.url), 'utf8')
const parsed = ts.createSourceFile('main.ts', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS)
const declarations = new Map(parsed.statements.filter(ts.isFunctionDeclaration)
  .filter(node => node.name).map(node => [node.name.text, node]))
const names = [
  'windowsInstallerActionsSupported', 'windowsUpdateDownloadDirectory', 'clearWindowsUpdateCache',
  'publishVerifiedWindowsInstaller', 'restoreWindowsUpdateCache', 'revalidateReadyWindowsInstaller',
  'activeDesktopUpdateSnoozeFor', 'clearDesktopUpdateSnoozeIfVersionChanged',
  'desktopUpdateSnapshot', 'publishDesktopUpdateState', 'setDesktopUpdateState',
  'classifyDesktopUpdateError', 'classifyDesktopUpdateTelemetryError', 'desktopUpdateErrorMessage', 'showUpdateError', 'downloadDesktopUpdate',
  'desktopUpdateCheckAllowed', 'runDesktopUpdateCheck', 'checkForUpdates',
  'desktopUpdatePlatform', 'desktopUpdateLocaleTags',
  'fetchDesktopUpdateChannelFromRoot', 'fetchDesktopUpdateChannelFromGithubReleases', 'fetchDesktopUpdateChannel',
  'probeDesktopUpdateSource', 'chooseDesktopUpdateSource', 'rememberSuccessfulUpdateSource',
  'fetchWindowsInstallerDigestFromSource', 'fetchWindowsInstallerDigestWithRetries', 'fetchCanonicalWindowsInstallerDigest',
  'downloadVerifiedWindowsInstaller', 'downloadVerifiedWindowsInstallerWithFallback', 'alternateDesktopUpdateSource',
  'resolveDesktopUpdate',
]
for (const name of names) assert.ok(declarations.has(name), `production function ${name} must exist`)
const variableNames = [
  'UPDATE_SNOOZE_MS', 'UPDATE_CHECK_REPEAT_DELAY_MS', 'UPDATE_CHECKSUM_MAX_BYTES',
  'UPDATE_INSTALLER_MAX_BYTES', 'UPDATE_INSTALLER_DOWNLOAD_TIMEOUT_MS',
  'DESKTOP_UPDATE_CHECKSUM_SOURCES', 'UPDATE_CHECKSUM_FETCH_ATTEMPTS', 'UPDATE_CHECKSUM_RETRY_DELAY_MS',
  'desktopUpdateCheckScheduler',
]
const variables = variableNames.map(name => {
  const statement = parsed.statements.find(node => ts.isVariableStatement(node)
    && node.declarationList.declarations.some(declaration => declaration.name.getText(parsed) === name))
  assert.ok(statement, `production variable ${name} must exist`)
  return statement.getText(parsed)
})
const compiled = ts.transpileModule(
  `${names.map(name => declarations.get(name).getText(parsed)).join('\n')}
   ${variables.join('\n')}
   globalThis.subject = { checkForUpdates, downloadDesktopUpdate, restoreWindowsUpdateCache, revalidateReadyWindowsInstaller,
     desktopUpdateSnapshot, fetchCanonicalWindowsInstallerDigest, downloadVerifiedWindowsInstallerWithFallback };`,
  { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None } },
).outputText

const bytes = Buffer.from('Synthetic OSS Windows installer bytes; never executable or executed.')
const digest = createHash('sha256').update(bytes).digest('hex')
const manifest = {
  schemaVersion: 1, tag: 'v0.5.5', version: '0.5.5', baseVersion: '0.5.5', prerelease: false,
  publishedAt: '2026-09-09T00:00:00Z',
  releaseUrl: 'https://github.com/TokenRhythm/opensquilla/releases/tag/v0.5.5', sha256sums: 'SHA256SUMS',
  platforms: {
    'win32-x64': { feed: 'latest.yml', installer: 'OpenSquilla-0.5.5-win-x64.exe' },
    'darwin-arm64': { feed: 'latest-mac.yml', installer: 'OpenSquilla-0.5.5-mac-arm64.dmg', archive: 'OpenSquilla-0.5.5-mac-arm64.zip' },
  },
}
const candidate = candidateFromUpdateChannel('0.5.4', manifest, 'win32-x64')
assert.ok(candidate)
const channelUrl = updateChannelManifestUrl('0.5.4')
const feedUrl = updateAssetUrl(candidate, 'oss', candidate.feed)
const installerUrl = updateAssetUrl(candidate, 'oss')
const checksumUrl = updateAssetUrl(candidate, 'oss', 'SHA256SUMS')
const root = await mkdtemp(join(tmpdir(), 'opensquilla-update-network-'))
let sequence = 0
let passed = 0

function fixture(options = {}) {
  const userData = options.userData ?? join(root, `case-${++sequence}`)
  const directory = join(userData, 'update-downloads')
  const path = join(directory, candidate.installer)
  const calls = { requests: [], signatures: 0, reveals: [], states: [], events: [] }
  const network = { offline: Boolean(options.offline) }
  const signature = { error: options.signatureError ?? null }
  const globals = {
    desktopReliabilityTelemetry: {
      recordUpdateResult: (event) => { (calls.telemetry ??= []).push(event) },
      finishSession() {},
    },
    process: { platform: 'win32', arch: 'x64', env: options.disableInstall ? { OPENSQUILLA_DESKTOP_ENABLE_WIN_INSTALL: '0' } : {} },
    join, stat, setTimeout, AbortSignal,
    UpdateCheckScheduler, isUpdateCheckAllowed, UpdateChannelError, WindowsUpdateSecurityError, WindowsUpdateHandoffError,
    candidateFromUpdateChannel, orderedUpdateSources, updateAssetUrl, updateChannelManifestFromReleaseInventory,
    updateChannelManifestUrl, updateChannelPathForVersion, UPDATE_GITHUB_RELEASES_API_URL,
    parseSha256SumsForAsset, readResponseTextWithLimit, streamResponseToVerifiedFile,
    createWindowsUpdateCacheDescriptor, loadWindowsUpdateCache, saveWindowsUpdateCache,
    app: {
      getVersion: () => '0.5.4', getPath: () => userData,
      getPreferredSystemLanguages: () => [options.locale ?? 'zh-CN'], getLocale: () => options.locale ?? 'zh-CN',
    },
    BrowserWindow: { getAllWindows: () => [{ isDestroyed: () => false, webContents: {
      send: (channel, state) => { assert.equal(channel, 'desktop:update:state-changed'); calls.states.push(state) },
    } }] },
    desktopUpdateManaged: () => true, desktopUpdateInstallMode: () => 'manual',
    nativeAutoUpdateEnabled: () => false, mockUpdateVersion: () => null,
    desktopUpdateStatus: 'idle', desktopUpdateLatestVersion: null, desktopUpdateProgress: null,
    desktopUpdateCheckedAt: null, desktopUpdateError: null, desktopUpdateErrorCode: null,
    desktopUpdateReleaseUrl: null, desktopUpdateSource: null, desktopUpdateFallbackUsed: false,
    desktopUpdateSnoozedVersion: null, desktopUpdateSnoozedUntil: null,
    desktopUpdateCandidate: null, nativeUpdateReady: null, downloadedUpdateVersion: null,
    verifiedManualInstallerPath: null, windowsUpdateCacheDescriptor: null,
    windowsUpdateCacheRestore: null, windowsUpdateCacheRestoreAttempted: false,
    updateApplying: false, updateDownloadInProgress: false, manualInstallerActionInProgress: false,
    isQuitting: false, desktopWriters: new DesktopWriterAdmission(), lastSuccessfulUpdateSource: null,
    loadDesktopUpdatePersistence: () => {}, persistDesktopUpdateState: async () => {},
    verifyCachedInstaller: (cacheDirectory, descriptor, version, constraints = {}) => verifyCachedInstaller(
      cacheDirectory, descriptor, version, { ...constraints, verifySignature: async verifiedPath => {
        calls.signatures += 1
        assert.equal(verifiedPath, await realpath(path), 'the signer seam receives only this synthetic cache file')
        assert.equal(createHash('sha256').update(await readFile(verifiedPath)).digest('hex'), digest)
        assert.ok(await loadWindowsUpdateCache(directory), 'verified download metadata must precede the signer seam')
        // Deliberately no OS signature verification or child process here.
        if (signature.error) throw new WindowsUpdateSecurityError(signature.error, 'Synthetic signature verifier unavailable')
      } },
    ),
    desktopMonitoredFetch: (...args) => globals.fetch(...args),
    fetch: async (input, init = {}) => {
      const url = new URL(String(input))
      const range = new Headers(init.headers).get('range')
      const allowedOss = url.origin === new URL(UPDATE_OSS_RELEASE_ROOT).origin
        && url.pathname.startsWith(`${new URL(UPDATE_OSS_RELEASE_ROOT).pathname}/`)
      const request = { url: url.href, range, rejected: !allowedOss || network.offline }
      calls.requests.push(request)
      if (request.rejected) throw new TypeError(`Synthetic network policy denied ${url.href}`)
      assert.equal(init.cache, 'no-store')
      assert.ok(init.signal, 'production fetch must retain its bounded timeout')
      if (url.href === channelUrl) return Response.json(manifest)
      if (url.href === feedUrl) {
        assert.equal(range, 'bytes=0-0')
        return new Response(`version: ${candidate.version}\n`, { status: 206 })
      }
      if (url.href === checksumUrl) {
        assert.equal(range, null)
        return options.missingChecksum ? new Response('missing', { status: 404 })
          : new Response(`${digest}  ${candidate.installer}\n`)
      }
      if (url.href === installerUrl) {
        if (range) { assert.equal(range, 'bytes=0-0'); return new Response(bytes.subarray(0, 1), { status: 206 }) }
        const body = options.tamperedDownload ? Buffer.alloc(bytes.length, 0x58) : bytes
        return new Response(body, { headers: { 'Content-Length': String(body.length) } })
      }
      assert.fail(`Unexpected OSS request: ${url.href}`)
    },
    shell: { showItemInFolder: revealedPath => { calls.reveals.push(revealedPath) } },
    createApplicationMenu: () => {}, desktopT: key => key,
    desktopLog: (name, details) => { calls.events.push({ name, details }) },
    console: { error: (...args) => { calls.events.push({ name: 'console-error', args }) } },
  }
  const context = vm.createContext(globals)
  vm.runInContext(compiled, context, { filename: 'production-windows-update-network.js' })
  return { context, subject: context.subject, calls, network, signature, directory, path, userData,
    state: () => context.subject.desktopUpdateSnapshot() }
}

async function discover(f, canInstall = true) {
  await f.subject.checkForUpdates(true)
  assert.equal(f.state().status, 'available', JSON.stringify(f.state()))
  assert.equal(f.state().latestVersion, candidate.version)
  assert.equal(f.state().source, 'oss')
  assert.equal(f.state().canInstall, canInstall, 'handoff is enabled by default and respects the emergency opt-out')
}

async function check(name, test) {
  await test()
  passed += 1
  console.log(`PASS ${name}`)
}

try {
  let completed
  await check('OSS-only discovery, feed probe, checksum, download and cache verification succeed without GitHub', async () => {
    const f = fixture()
    await discover(f)
    const state = await f.subject.downloadDesktopUpdate()
    assert.equal(state.status, 'downloaded', JSON.stringify(state))
    assert.equal(state.source, 'oss')
    assert.equal(state.progress, 100)
    assert.equal(state.errorCode, null)
    assert.equal(state.canInstall, true)
    assert.equal(f.calls.signatures, 1)
    assert.deepEqual(f.calls.requests, [
      { url: channelUrl, range: null, rejected: false },
      { url: feedUrl, range: 'bytes=0-0', rejected: false },
      { url: installerUrl, range: 'bytes=0-0', rejected: false },
      { url: checksumUrl, range: null, rejected: false },
      { url: installerUrl, range: null, rejected: false },
    ])
    assert.deepEqual(await readFile(f.path), bytes)
    assert.equal((await loadWindowsUpdateCache(f.directory)).sha256, digest)
    assert.equal(f.context.verifiedManualInstallerPath, await realpath(f.path))
    completed = f
  })

  await check('GitHub-first discovery and probes fall back to the complete OSS path', async () => {
    const f = fixture({ locale: 'en-US' })
    await discover(f)
    assert.equal(f.state().fallbackUsed, true)
    const state = await f.subject.downloadDesktopUpdate()
    assert.equal(state.status, 'downloaded', JSON.stringify(state))
    assert.equal(state.source, 'oss')
    assert.equal(state.fallbackUsed, true)
    assert.deepEqual(f.calls.requests.filter(request => request.rejected).map(request => request.url), [
      UPDATE_GITHUB_RELEASES_API_URL,
      updateAssetUrl(candidate, 'github', candidate.feed),
      updateAssetUrl(candidate, 'github'),
    ])
    assert.equal(f.calls.requests.filter(request => request.url === checksumUrl).length, 1)
    assert.equal(f.calls.signatures, 1)
    assert.deepEqual(await readFile(f.path), bytes)
  })

  await check('default OSS-only handoff remains ready after local revalidation without a network request', async () => {
    const f = fixture()
    await discover(f, true)
    const state = await f.subject.downloadDesktopUpdate()
    assert.equal(state.status, 'downloaded', JSON.stringify(state))
    assert.equal(state.canInstall, true)
    assert.equal(state.source, 'oss')
    assert.ok(f.calls.requests.every(request => !request.rejected))
    const requestsBefore = structuredClone(f.calls.requests)
    const signaturesBefore = f.calls.signatures
    f.network.offline = true
    const readyPath = await f.subject.revalidateReadyWindowsInstaller()
    assert.equal(readyPath, await realpath(f.path))
    assert.equal(f.calls.signatures, signaturesBefore + 1)
    assert.deepEqual(f.calls.requests, requestsBefore)
    assert.equal(f.state().status, 'downloaded')
    assert.equal(f.state().canInstall, true)
    assert.deepEqual(f.calls.reveals, [])
  })

  await check('emergency opt-out preserves verified OSS downloads and the manual installer entry', async () => {
    const f = fixture({ disableInstall: true })
    await discover(f, false)
    const state = await f.subject.downloadDesktopUpdate()
    assert.equal(state.status, 'downloaded')
    assert.equal(state.canInstall, false)
    assert.equal(f.calls.signatures, 1)
    assert.deepEqual(await readFile(f.path), bytes)
    assert.equal(f.context.verifiedManualInstallerPath, await realpath(f.path))
  })

  await check('temporary signature unavailability retains verified bytes and recovers on Download without any new fetch', async () => {
    const f = fixture({ signatureError: 'signature_unavailable' })
    await discover(f)
    const failed = await f.subject.downloadDesktopUpdate()
    assert.equal(failed.status, 'error', JSON.stringify(failed))
    assert.equal(failed.errorCode, 'signature_unavailable')
    const failedTelemetry = f.calls.telemetry.filter(event => event.updateStage === 'download')
    assert.equal(failedTelemetry.length, 1)
    assert.equal(failedTelemetry[0].outcome, 'fail')
    assert.equal(failedTelemetry[0].errorCode, 'internal_error')
    assert.equal(f.context.verifiedManualInstallerPath, null)
    assert.equal(f.calls.signatures, 1)
    assert.ok(!f.calls.states.some(state => state.status === 'downloaded'))
    assert.deepEqual(await readFile(f.path), bytes)
    const retained = await loadWindowsUpdateCache(f.directory)
    assert.equal(retained.sha256, digest)
    assert.equal(retained.bytes, bytes.length)
    const requestsBefore = structuredClone(f.calls.requests)
    f.network.offline = true
    f.signature.error = null
    const recovered = await f.subject.downloadDesktopUpdate()
    assert.equal(recovered.status, 'downloaded', JSON.stringify(recovered))
    assert.equal(recovered.errorCode, null)
    assert.equal(f.context.verifiedManualInstallerPath, await realpath(f.path))
    assert.ok(f.calls.signatures > 1, 'retry must execute the signature seam again')
    assert.deepEqual(f.calls.requests, requestsBefore)
    assert.deepEqual(await loadWindowsUpdateCache(f.directory), retained)
    assert.deepEqual(await readFile(f.path), bytes)
  })

  await check('a previously selected GitHub download source retries OSS with the same canonical digest', async () => {
    const f = fixture()
    const expected = await f.subject.fetchCanonicalWindowsInstallerDigest(candidate)
    const downloaded = await f.subject.downloadVerifiedWindowsInstallerWithFallback(
      candidate, { source: 'github', fallbackUsed: false }, expected,
    )
    assert.equal(downloaded.source, 'oss')
    assert.equal(downloaded.fallbackUsed, true)
    assert.deepEqual(f.calls.requests.map(request => [request.url, request.rejected]), [
      [checksumUrl, false], [updateAssetUrl(candidate, 'github'), true], [installerUrl, false],
    ])
    assert.deepEqual(await readFile(downloaded.path), bytes)
  })

  await check('missing OSS checksum plus blocked GitHub fails before downloading installer bytes', async () => {
    const f = fixture({ missingChecksum: true })
    await discover(f)
    const state = await f.subject.downloadDesktopUpdate()
    assert.equal(state.status, 'error', JSON.stringify(state))
    assert.equal(state.errorCode, 'checksum_unavailable')
    assert.equal(f.calls.requests.filter(request => request.url === checksumUrl).length, 3)
    assert.equal(f.calls.requests.filter(request => request.url === updateAssetUrl(candidate, 'github', 'SHA256SUMS') && request.rejected).length, 3)
    assert.equal(f.calls.requests.filter(request => request.url === installerUrl && !request.range).length, 0)
    assert.equal(f.calls.signatures, 0)
    assert.equal(f.context.verifiedManualInstallerPath, null)
    assert.equal(await loadWindowsUpdateCache(f.directory), null)
    assert.ok(!f.calls.states.some(state => state.status === 'downloaded'))
  })

  await check('a valid persisted cache is rehashed and reverified without any discovery, checksum or byte fetch', async () => {
    const f = fixture({ userData: completed.userData, offline: true })
    const state = await f.subject.downloadDesktopUpdate()
    assert.equal(state.status, 'downloaded', JSON.stringify(state))
    assert.deepEqual(f.calls.requests, [])
    assert.ok(f.calls.signatures >= 1)
    assert.deepEqual(f.calls.reveals, [await realpath(f.path)])
    assert.deepEqual(await readFile(f.path), bytes)
  })

  await check('tampered OSS installer bytes fail SHA256 and blocked GitHub cannot weaken that failure', async () => {
    const f = fixture({ tamperedDownload: true })
    await discover(f)
    const state = await f.subject.downloadDesktopUpdate()
    assert.equal(state.status, 'error', JSON.stringify(state))
    assert.equal(state.errorCode, 'integrity_failed')
    assert.ok(f.calls.requests.some(request => request.url === updateAssetUrl(candidate, 'github') && request.rejected && !request.range))
    assert.equal(f.calls.signatures, 0, 'a hash mismatch must never reach the signer seam')
    assert.equal(await loadWindowsUpdateCache(f.directory), null)
    assert.deepEqual(await readdir(f.directory), [], 'no invalid installer, descriptor or partial download survives')
    assert.ok(!f.calls.states.some(state => state.status === 'downloaded'))
  })

  await check('a tampered persisted cache is rejected offline before the signer seam or any network request', async () => {
    await writeFile(completed.path, Buffer.alloc(bytes.length, 0x59))
    const f = fixture({ userData: completed.userData, offline: true })
    await f.subject.restoreWindowsUpdateCache()
    assert.equal(f.state().status, 'idle')
    assert.equal(f.context.verifiedManualInstallerPath, null)
    assert.equal(await loadWindowsUpdateCache(f.directory), null)
    assert.deepEqual(f.calls.requests, [])
    assert.equal(f.calls.signatures, 0)
    assert.deepEqual(f.calls.reveals, [])
  })

  console.log(`Windows update restricted-network checks passed: ${passed} (production main/network-selection/hash/cache; synthetic transport and signature seams; no external network or installer).`)
} finally {
  const cleanup = resolve(root)
  const fromTemp = relative(resolve(tmpdir()), cleanup)
  assert.ok(fromTemp && !fromTemp.startsWith('..') && !fromTemp.includes(':'))
  await rm(cleanup, { recursive: true, force: true })
}
