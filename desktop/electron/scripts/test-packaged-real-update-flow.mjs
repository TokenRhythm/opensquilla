import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { once } from 'node:events'
import { readFile, writeFile } from 'node:fs/promises'
import { createServer } from 'node:http'
import { basename, resolve } from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'
import { spawn } from 'node:child_process'
import { fileURLToPath } from 'node:url'

import {
  launchPackagedCandidate,
  requiredOption,
  waitFor,
} from './packaged-smoke-helpers.mjs'

const executablePath = resolve(requiredOption('--executable'))
const userDataDir = resolve(requiredOption('--user-data-dir'))
const manifestPath = resolve(requiredOption('--channel-manifest'))
const expectedVersion = requiredOption('--expected-version')
const mode = requiredOption('--mode')
if (!['native', 'manual', 'signed-handoff', 'signed-cached-handoff'].includes(mode)) {
  throw new Error(`--mode must be native, manual, signed-handoff, or signed-cached-handoff, received ${mode}`)
}
const cachedHandoff = mode === 'signed-cached-handoff'
const signedHandoff = mode === 'signed-handoff' || cachedHandoff
const downloadSourceMode = process.argv.includes('--download-source-mode')
  ? requiredOption('--download-source-mode') : 'oss'
if (!['oss', 'github-to-oss'].includes(downloadSourceMode)) {
  throw new Error('--download-source-mode must be oss or github-to-oss')
}
if (process.argv.includes('--download-source-mode') && mode !== 'signed-handoff') {
  throw new Error('--download-source-mode requires signed-handoff download mode')
}
const requireSourceFallback = downloadSourceMode === 'github-to-oss'
const stableVersion = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/
if (signedHandoff && !process.argv.includes('--baseline-version')) {
  throw new Error('signed-handoff requires an explicit --baseline-version with the new installer capability')
}
const baselineVersion = process.argv.includes('--baseline-version')
  ? requiredOption('--baseline-version')
  : '0.5.3'
if (signedHandoff && (!stableVersion.test(baselineVersion) || ['0.5.3', '0.5.4'].includes(baselineVersion))) {
  throw new Error('signed-handoff requires a canonical stable baseline containing the new installer capability; 0.5.3/0.5.4 use manual mode')
}
if (!signedHandoff && !['0.5.3', '0.5.4'].includes(baselineVersion)) {
  throw new Error('--baseline-version must be 0.5.3 or 0.5.4')
}
if (!stableVersion.test(expectedVersion)) {
  throw new Error('--expected-version must be a canonical stable version')
}
const baselineParts = baselineVersion.split('.').map(Number)
const candidateParts = expectedVersion.split('.').map(Number)
const firstDifference = candidateParts.findIndex((part, index) => part !== baselineParts[index])
if (firstDifference < 0 || candidateParts[firstDifference] <= baselineParts[firstDifference]) {
  throw new Error(`The candidate must be newer than baseline ${baselineVersion}`)
}
const readyOutputIndex = process.argv.indexOf('--ready-output')
const readyOutput = readyOutputIndex >= 0 ? resolve(process.argv[readyOutputIndex + 1]) : null
const installDirIndex = process.argv.indexOf('--install-dir')
const installDir = installDirIndex >= 0 ? resolve(process.argv[installDirIndex + 1]) : null
const defaultInstall = process.argv.includes('--default-install')
const expectedShaIndex = process.argv.indexOf('--expected-sha256')
const expectedSha256 = expectedShaIndex >= 0
  ? String(process.argv[expectedShaIndex + 1]).trim().toLowerCase()
  : null
const sourceSha = process.argv.includes('--source-sha') ? requiredOption('--source-sha') : null
const cachedInstaller = cachedHandoff ? resolve(requiredOption('--cached-installer')) : null
const baselineSourceSha = cachedHandoff ? requiredOption('--baseline-source-sha') : null
if (!cachedHandoff && (process.argv.includes('--cached-installer') || process.argv.includes('--baseline-source-sha'))) {
  throw new Error('Cached artifact arguments require explicit signed-cached-handoff mode')
}

if (mode === 'manual' && (!readyOutput || (!installDir && !defaultInstall) || !expectedSha256)) {
  throw new Error(
    'manual mode requires --ready-output, --expected-sha256, and one installation mode',
  )
}
if (signedHandoff && (!readyOutput || !expectedSha256 || !/^[0-9a-f]{40}$/.test(sourceSha || ''))) {
  throw new Error('signed-handoff requires --ready-output, --expected-sha256, and a full lowercase --source-sha from the candidate build')
}
if (signedHandoff && (installDir || defaultInstall)) {
  throw new Error('signed-handoff uses the installed baseline directory; --install-dir/--default-install are manual-mode options')
}
if (installDir && defaultInstall) {
  throw new Error('--install-dir and --default-install are mutually exclusive')
}
if (expectedSha256 && !/^[0-9a-f]{64}$/.test(expectedSha256)) {
  throw new Error('--expected-sha256 must be 64 lowercase hexadecimal characters')
}
const {
  electron, environmentWithoutProviderSecrets, captureElectronProcessIdentity,
  closeElectronAndObserveExit, desktopShutdownEvidenceSince, gatewayProcessSnapshot,
  desktopProfileFingerprint, loadDesktopGatewayOwnershipRecord, verifyDesktopGatewayOwnership,
  DESKTOP_GATEWAY_STARTUP_TIMEOUT_MS, assertCachedRestartEvidence, fileSha256,
  requestCachedQuitOnce, stageVerifiedCachedHandoff, waitForRestoredCache,
} = cachedHandoff ? await import('./fixtures/packaged-cached-handoff/runtime.mjs') : {}
const { captureSignedHandoffProcesses, observeSignedHandoff, preserveFailedDriverUntilExit, releaseExitedHandoffTransport, trackSignedChildClose } = signedHandoff
  ? await import('./fixtures/packaged-cached-handoff/signed-exit-observer.mjs') : {}

const manifest = JSON.parse((await readFile(manifestPath, 'utf8')).replace(/^\uFEFF/, ''))
assert.equal(manifest.schemaVersion, 1)
assert.equal(manifest.version, expectedVersion)
assert.equal(manifest.tag, `v${expectedVersion}`)
assert.equal(manifest.prerelease, false, `v${baselineVersion} stable must rehearse a final update`)

let channelRequests = 0
let channelAvailable = false
const server = createServer((request, response) => {
  if (request.url !== '/channels/stable.json') {
    response.writeHead(404)
    response.end()
    return
  }
  channelRequests += 1
  if (!channelAvailable) {
    response.writeHead(503, { 'Cache-Control': 'no-store' })
    response.end('synthetic pre-handoff failure')
    return
  }
  response.writeHead(200, {
    'Content-Type': 'application/json; charset=utf-8',
    'Cache-Control': 'no-store',
  })
  response.end(JSON.stringify(manifest))
})
server.listen(0, '127.0.0.1')
await once(server, 'listening')
const address = server.address()
assert.ok(address && typeof address === 'object')
const channelRoot = `http://127.0.0.1:${address.port}`

let app
let handedOff = false
let signedInstallRequested = false
let handoffProcesses
let failureExitContext
let driverFailure = null
let appLaunchLogCheckpoint = ''
let cacheEvidence
let cachedQuitState = { requested: false }
const cacheCycles = []
const desktopLogPath = resolve(userDataDir, 'logs', 'desktop.log')
const launchEnvironment = {
  GITHUB_ACTIONS: '0',
  OPENSQUILLA_DESKTOP_DISABLE_AUTO_UPDATE: '0',
  OPENSQUILLA_DESKTOP_UPDATE_CHANNEL_ROOT: channelRoot,
  OPENSQUILLA_DESKTOP_UPDATE_SOURCE: requireSourceFallback ? 'github' : 'oss',
  OPENSQUILLA_RECOVERY_OFFLINE: '1',
  OPENSQUILLA_TESTING: '0',
  OPENSQUILLA_DESKTOP_ENABLE_WIN_INSTALL: signedHandoff ? '1' : '0',
  OPENSQUILLA_DESKTOP_ENABLE_WIN_UPDATE: '0',
  OPENSQUILLA_DESKTOP_MOCK_UPDATE_VERSION: '',
  OPENSQUILLA_DESKTOP_MOCK_UPDATE_DIALOG_RESPONSES: '',
}
async function cachedCycle(page, checkpoint) {
  await page.locator('.conn-pill.connected').waitFor({ state: 'visible', timeout: DESKTOP_GATEWAY_STARTUP_TIMEOUT_MS + 45_000 })
  const state = await app.evaluate(({ app }) => ({ version: app.getVersion(), userData: app.getPath('userData') }))
  assert.equal(state.version, baselineVersion)
  assert.equal(resolve(state.userData).toLowerCase(), userDataDir.toLowerCase())
  const update = await waitForRestoredCache(() => page.evaluate(() => window.opensquillaDesktop.getUpdateState()))
  assert.equal(update.status, 'downloaded', JSON.stringify(update))
  assert.equal(update.currentVersion, baselineVersion)
  assert.equal(update.latestVersion, expectedVersion)
  assert.equal(update.progress, 100)
  assert.equal(update.installMode, 'manual')
  assert.equal(update.canInstall, true)
  const currentLog = await readFile(desktopLogPath, 'utf8')
  assert.ok(currentLog.startsWith(checkpoint), 'Desktop log must retain the prelaunch evidence')
  const restored = currentLog.slice(checkpoint.length).split(/\r?\n/).some(line => {
    try { const record = JSON.parse(line); return record.event === 'update_windows_cache_restored' && record.version === expectedVersion } catch { return false }
  })
  assert.equal(restored, true, 'This actual A process must restore the verified cache')
  const ownership = await waitFor(async () => {
    const loaded = loadDesktopGatewayOwnershipRecord(resolve(userDataDir, 'gateway-ownership', desktopProfileFingerprint(resolve(userDataDir, 'opensquilla'))))
    return loaded.status === 'valid' && await verifyDesktopGatewayOwnership(loaded.record) ? loaded.record : null
  }, 'the actual cached A owned Gateway identity', 45_000)
  assert.equal(ownership.version, baselineVersion)
  return { ...await captureElectronProcessIdentity(app), gatewayPid: ownership.pid, gatewayStartIdentity: ownership.start_identity, ownership, cacheRestored: true, update }
}
async function captureCachedStage(page, stage) {
  const path = `${readyOutput}.cache-${stage}.png`
  await page.screenshot({ path, fullPage: true })
  cacheEvidence.screenshots ??= []
  cacheEvidence.screenshots.push({ stage, path, sha256: await fileSha256(path) })
  await writeFile(`${readyOutput}.cache.json`, `${JSON.stringify({ ...cacheEvidence, stage, cycles: cacheCycles, handoffObserved: false }, null, 2)}\n`)
}
try {
  if (cachedHandoff) {
    assert.equal(process.platform, 'win32', 'Cached native handoff requires Windows')
    cacheEvidence = await stageVerifiedCachedHandoff({
      userDataDir, baselineVersion, expectedVersion, expectedSha256,
      sourceSha, baselineSourceSha, manifestPath, installerPath: cachedInstaller,
    })
    cacheEvidence.probeSources = await Promise.all([
      'test-packaged-real-update-flow.mjs',
      'fixtures/packaged-cached-handoff/contract.mjs',
      'fixtures/packaged-cached-handoff/runtime.mjs',
      'fixtures/packaged-cached-handoff/signed-exit-observer.mjs',
      '../dist/update-channel.js', '../dist/windows-update-cache.js', '../dist/windows-update-security.js',
    ].map(async file => ({ file, sha256: await fileSha256(fileURLToPath(new URL(file, import.meta.url))) })))
  }
  if (signedHandoff) appLaunchLogCheckpoint = await readFile(desktopLogPath, 'utf8').catch(error => {
    if (error.code === 'ENOENT') return ''
    throw error
  })
  app = await launchPackagedCandidate({
    executablePath,
    userDataDir,
    // The signed A-to-B audit retains the original seed provider. Changing it
    // here would turn post-upgrade credential preservation into a false claim.
    model: signedHandoff
      ? 'opensquilla-release-session-recovery-smoke'
      : 'opensquilla-real-updater-rehearsal',
    scrubProviderSecrets: true,
    // Neither the legacy native Windows opt-in nor the mock updater replaces
    // the signed installer verifier in either signed handoff mode.
    env: launchEnvironment,
  })
  if (signedHandoff) trackSignedChildClose(app.process())
  let page = await app.firstWindow({ timeout: 60_000 })
  await waitFor(
    () => page.evaluate(() => typeof window.opensquillaDesktop?.checkForUpdates === 'function'),
    `the official v${baselineVersion} updater bridge`,
  )

  const initial = await page.evaluate(() => window.opensquillaDesktop.getUpdateState())
  assert.equal(initial.currentVersion, baselineVersion)

  let downloaded
  if (cachedHandoff) {
    // The loopback channel remains unavailable throughout this mode. The only
    // admitted candidate comes from the signed, hash-pinned local cache, not a
    // fake successful download or a public release assertion.
    const first = await cachedCycle(page, '')
    cacheCycles.push(first)
    await captureCachedStage(page, 'first-restored')
    const credentialBefore = await fileSha256(resolve(userDataDir, 'desktop-credential.json'))
    const beforeQuit = await readFile(desktopLogPath, 'utf8')
    failureExitContext = await captureSignedHandoffProcesses({ app, userDataDir, desktopLogPath, launchCheckpoint: appLaunchLogCheckpoint, baselineVersion })
    let quitTimer
    try {
      await requestCachedQuitOnce(cachedQuitState, () => Promise.race([
        closeElectronAndObserveExit(app, first, 100_000),
        new Promise((_, reject) => { quitTimer = setTimeout(() => reject(new Error('A normal Quit timed out before cached restart')), 100_000) }),
      ]))
    } finally { clearTimeout(quitTimer) }
    assert.equal(gatewayProcessSnapshot(first.ownership).alive, false, 'The first A owned Gateway must exit naturally')
    first.shutdown = desktopShutdownEvidenceSince(beforeQuit, await readFile(desktopLogPath, 'utf8'))
    assert.deepEqual(first.shutdown, { gatewayExitLogged: true, committedExitLogged: true })
    first.normalQuitVerified = true
    app = null
    cachedQuitState = { requested: false }
    assert.equal(await fileSha256(resolve(userDataDir, 'desktop-credential.json')), credentialBefore)
    const restartCheckpoint = await readFile(desktopLogPath, 'utf8')
    appLaunchLogCheckpoint = restartCheckpoint
    failureExitContext = null
    // The shared first-launch helper rewrites credentials. Restart directly so
    // the same existing credential bytes must survive both A processes and B.
    app = await electron.launch({
      executablePath,
      args: ['--use-mock-keychain', `--user-data-dir=${userDataDir}`],
      env: { ...environmentWithoutProviderSecrets(process.env), OPENSQUILLA_DESKTOP_SECRET_STORAGE: 'plain', ...launchEnvironment },
      timeout: DESKTOP_GATEWAY_STARTUP_TIMEOUT_MS + 45_000,
    })
    trackSignedChildClose(app.process())
    page = await app.firstWindow({ timeout: 60_000 })
    const second = await cachedCycle(page, restartCheckpoint)
    cacheCycles.push(second)
    assertCachedRestartEvidence(first, second, credentialBefore, await fileSha256(resolve(userDataDir, 'desktop-credential.json')))
    downloaded = second.update
    cacheEvidence.cacheRestartVerified = true
    cacheEvidence.credentialSha256 = credentialBefore
    cacheEvidence.cycles = cacheCycles.map(({ ownership: _ownership, ...cycle }) => cycle)
    await captureCachedStage(page, 'restart-restored')
  } else {
    // A failed discovery must leave the old, fully functional client running;
    // no installer handoff is allowed until an exact candidate has been found
    // and downloaded. The same process then retries against the valid fixture.
    const failed = await page.evaluate(() => window.opensquillaDesktop.checkForUpdates())
    assert.equal(failed.status, 'error', JSON.stringify(failed))
    assert.equal(failed.errorCode, 'source_unreachable')
    assert.equal(app.process().killed, false)
    channelAvailable = true
    const available = await page.evaluate(() => window.opensquillaDesktop.checkForUpdates())
    assert.equal(available.status, 'available', JSON.stringify(available))
    assert.equal(available.latestVersion, expectedVersion)
    assert.equal(available.source, 'oss')
    if (requireSourceFallback) {
      assert.equal(available.fallbackUsed, true, 'Discovery must fall back from the requested GitHub feed to OSS')
    }
    assert.equal(available.installMode, signedHandoff ? 'manual' : mode)
    assert.ok(
      channelRequests >= 2 && channelRequests <= 4,
      `official update discovery made an unexpected number of requests: ${channelRequests}`,
    )

    downloaded = await page.evaluate(() => window.opensquillaDesktop.downloadUpdate())
    assert.equal(downloaded.status, 'downloaded', JSON.stringify(downloaded))
    assert.equal(downloaded.latestVersion, expectedVersion)
    assert.equal(downloaded.progress, 100)
    assert.equal(downloaded.source, 'oss')
    if (requireSourceFallback) {
      assert.equal(downloaded.fallbackUsed, true, 'The verified download must retain the OSS fallback evidence')
    }
    if (signedHandoff) {
      assert.equal(downloaded.installMode, 'manual')
      assert.equal(downloaded.canInstall, true, 'the signed candidate must pass the production installation gate')
    }
  }

  const result = {
    ok: true,
    fromVersion: initial.currentVersion,
    toVersion: expectedVersion,
    tag: manifest.tag,
    source: downloaded.source,
    installMode: downloaded.installMode,
    channelRequests,
    executable: basename(executablePath),
    oldPid: cachedHandoff ? cacheCycles.at(-1).electronPid : app.process().pid,
  }

  if (mode === 'manual') {
    const installerName = manifest.platforms['win32-x64'].installer
    const installer = resolve(userDataDir, 'update-downloads', installerName)
    const bytes = await readFile(installer)
    const actualSha256 = createHash('sha256').update(bytes).digest('hex')
    assert.equal(actualSha256, expectedSha256, 'downloaded installer checksum differs')
    let appClosed = false
    app.on('close', () => {
      appClosed = true
    })
    const runInstaller = () => {
      const installerArgs = defaultInstall ? ['/S'] : ['/S', `/D=${installDir}`]
      const child = spawn(installer, installerArgs, {
        windowsHide: true,
        stdio: 'inherit',
      })
      return {
        child,
        exit: Promise.race([
          once(child, 'exit').then(([code, signal]) => ({ code, signal })),
          once(child, 'error').then(([error]) => {
            throw error
          }),
        ]),
      }
    }
    const requireInstallerExit = async (exit, label) => await Promise.race([
      exit,
      delay(300_000).then(() => {
        throw new Error(`${label} did not exit within five minutes`)
      }),
    ])

    // Start the exact installer while the baseline is still running. It may reject,
    // wait, or close the old process; it must never report success while the
    // old process remains live over a partially overwritten installation.
    let collisionOutcome
    const first = runInstaller()
    const firstOutcome = await Promise.race([
      first.exit.then((value) => ({ kind: 'installer-exit', value })),
      once(app, 'close').then(() => ({ kind: 'app-closed' })),
      delay(8_000).then(() => ({ kind: 'still-waiting' })),
    ])
    if (firstOutcome.kind === 'still-waiting') {
      collisionOutcome = 'waited-for-running-client'
      if (!appClosed) await app.close()
      const finished = await requireInstallerExit(first.exit, 'waiting installer')
      assert.equal(finished.code, 0, `waiting installer failed: ${JSON.stringify(finished)}`)
    } else if (firstOutcome.kind === 'app-closed') {
      collisionOutcome = 'closed-running-client'
      const finished = await requireInstallerExit(first.exit, 'installer handoff')
      assert.equal(finished.code, 0, `installer handoff failed: ${JSON.stringify(finished)}`)
    } else if (firstOutcome.value.code !== 0) {
      collisionOutcome = 'refused-while-running'
      if (!appClosed) await app.close()
      const retry = runInstaller()
      const finished = await requireInstallerExit(retry.exit, 'installer retry')
      assert.equal(finished.code, 0, `installer retry failed: ${JSON.stringify(finished)}`)
    } else {
      // Allow a just-committed process shutdown to reach Playwright before
      // classifying an unsafe "success while still running" result.
      await delay(2_000)
      assert.equal(
        appClosed,
        true,
        `installer reported success while the official v${baselineVersion} process remained live`,
      )
      collisionOutcome = 'closed-running-client'
    }

    const manualResult = {
      ...result,
      downloadedInstaller: installer,
      sha256: actualSha256,
      collisionOutcome,
    }
    await writeFile(readyOutput, `${JSON.stringify(manualResult, null, 2)}\n`, { mode: 0o600 })
    console.log(JSON.stringify(manualResult))
    handedOff = appClosed
    if (!appClosed) await app.close()
    app = null
  } else if (signedHandoff) {
    const installerName = manifest.platforms['win32-x64'].installer
    assert.equal(installerName, `OpenSquilla-${expectedVersion}-win-x64.exe`)
    const installer = resolve(userDataDir, 'update-downloads', installerName)
    const actualSha256 = createHash('sha256').update(await readFile(installer)).digest('hex')
    assert.equal(actualSha256, expectedSha256, 'verified cache does not match the signed candidate artifact')
    const credentialSha256 = createHash('sha256')
      .update(await readFile(resolve(userDataDir, 'desktop-credential.json'))).digest('hex')
    const indicator = page.locator('[data-testid="desktop-update-indicator"]')
    await indicator.waitFor({ state: 'visible', timeout: 30_000 })
    await indicator.click()
    const install = page.locator('[data-testid="desktop-update-relaunch"]')
    await install.waitFor({ state: 'visible', timeout: 30_000 })
    assert.equal(await install.isEnabled(), true, 'the signed installer action must be available in the real UI')
    if (cachedHandoff) await captureCachedStage(page, 'before-install-click')
    handoffProcesses = await captureSignedHandoffProcesses({ app, userDataDir, desktopLogPath, launchCheckpoint: appLaunchLogCheckpoint, baselineVersion })
    failureExitContext = handoffProcesses
    let playwrightCloseObserved = false
    const onPlaywrightClose = () => { playwrightCloseObserved = true }
    app.on('close', onPlaywrightClose)
    const handoffStartedAt = new Date().toISOString()
    signedInstallRequested = true
    // Windows Playwright close waits for wrapper stdio EOF. NSIS may keep an
    // inherited handle while its visible wizard remains open, after A exited.
    // Keep the real UI click, but prove handoff from OS identities and new logs.
    let clickError = null
    const clicked = install.click({ timeout: 30_000, noWaitAfter: true }).catch(error => { clickError = String(error) })
    let exitEvidence
    try {
      exitEvidence = await observeSignedHandoff({ ...handoffProcesses, expectedVersion, clickPromise: clicked, readLog: () => readFile(desktopLogPath, 'utf8') })
    } finally { app.off('close', onPlaywrightClose) }
    handedOff = true
    app = null
    // Process exit is a handoff observation, not proof that NSIS installed B.
    // The outer packaged audit must check B's PE version, signatures, startup,
    // Gateway and profile before treating this rehearsal as successful.
    const handoffResult = {
      ...result,
      oldPid: handoffProcesses.electronPid,
      ok: false,
      stage: 'installer-handoff',
      requiresPostInstallVerification: true,
      handoffObserved: true,
      mode,
      inputMode: cachedHandoff ? 'verified-cache' : 'download',
      downloadVerified: !cachedHandoff,
      remotePublicationVerified: false,
      downloadSourceMode: cachedHandoff ? null : downloadSourceMode,
      sourceFallbackVerified: !cachedHandoff && requireSourceFallback,
      discoveryScope: 'controlled loopback channel; production asset sources',
      networkIsolationVerified: false,
      ...(cachedHandoff ? { ...cacheEvidence, cacheRestoreVerified: true } : {}),
      canInstall: true,
      sourceSha,
      downloadedInstaller: installer,
      sha256: actualSha256,
      credentialSha256,
      handoffStartedAt,
      oldProcessClosedAt: new Date().toISOString(),
      desktopLog: resolve(userDataDir, 'logs', 'desktop.log'),
      processExitEvidence: exitEvidence,
      playwrightCloseObservedBeforeTransportRelease: playwrightCloseObserved,
      ...(clickError ? { clickErrorAfterDispatch: clickError } : {}),
    }
    await writeFile(readyOutput, `${JSON.stringify(handoffResult, null, 2)}\n`, { mode: 0o600 })
    // Only after independent proof is durable, release this driver's local
    // pipe references. Their induced close event is not installation evidence.
    await releaseExitedHandoffTransport(handoffProcesses.child, exitEvidence)
    console.log(JSON.stringify(handoffResult))
  } else {
    assert.equal(downloaded.installMode, 'native')
    const closed = once(app, 'close')
    await page.evaluate(() => {
      void window.opensquillaDesktop.relaunchToUpdate()
      return true
    })
    await Promise.race([
      closed,
      delay(180_000).then(() => {
        throw new Error(`official v${baselineVersion} did not hand off to quitAndInstall`)
      }),
    ])
    handedOff = true
    app = null
    console.log(JSON.stringify(result))
  }
} catch (error) {
  driverFailure = error
  throw error
} finally {
  if (signedHandoff) {
    // Before an install/quit request, one ordinary cleanup Quit is permitted.
    // Never repeat it after failure, and never swallow an active-client error.
    if (app && !handedOff && !signedInstallRequested && !cachedQuitState.requested) {
      let cleanupTimer
      try {
        failureExitContext ??= await captureSignedHandoffProcesses({ app, userDataDir, desktopLogPath, launchCheckpoint: appLaunchLogCheckpoint, baselineVersion })
        cachedQuitState.requested = true
        const { closeElectronAndObserveExit: closeOnce } = await import('./packaged-first-send-cleanup.mjs')
        await Promise.race([
          closeOnce(app, { electronPid: failureExitContext.electronPid, wrapperPid: failureExitContext.child.pid }, 100_000),
          new Promise((_, reject) => { cleanupTimer = setTimeout(() => reject(new Error('Signed audit cleanup Quit timed out')), 100_000) }),
        ])
        app = null
      } catch (error) {
        driverFailure ??= error
        if (cacheEvidence) cacheEvidence.cleanupError = String(error)
      } finally { clearTimeout(cleanupTimer) }
    }
    if (driverFailure && (app || failureExitContext)) {
      // Retain even a late context after app=null: handoff file writes and
      // transport cleanup can fail after independent OS-exit proof succeeded.
      // This fence intentionally does not finish until operator exit is safe.
      await preserveFailedDriverUntilExit({
        context: failureExitContext,
        originalError: driverFailure,
        publish: async status => {
          await writeFile(`${readyOutput}.failure.json`, `${JSON.stringify(status, null, 2)}\n`, { mode: 0o600 })
          if (cacheEvidence) await writeFile(`${readyOutput}.cache.json`, `${JSON.stringify({ ...cacheEvidence, ...status, cycles: cacheCycles }, null, 2)}\n`)
        },
      })
      app = null
    }
  } else if (app && !handedOff) await app.close().catch(() => {})
  await new Promise((resolveClose) => server.close(resolveClose))
  if (driverFailure) throw driverFailure
}
