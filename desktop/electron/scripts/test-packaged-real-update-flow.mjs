import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { once } from 'node:events'
import { readFile, writeFile } from 'node:fs/promises'
import { createServer } from 'node:http'
import { basename, resolve } from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'
import { spawn } from 'node:child_process'

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
const readyOutputIndex = process.argv.indexOf('--ready-output')
const readyOutput = readyOutputIndex >= 0 ? resolve(process.argv[readyOutputIndex + 1]) : null
const installDirIndex = process.argv.indexOf('--install-dir')
const installDir = installDirIndex >= 0 ? resolve(process.argv[installDirIndex + 1]) : null
const defaultInstall = process.argv.includes('--default-install')
const expectedShaIndex = process.argv.indexOf('--expected-sha256')
const expectedSha256 = expectedShaIndex >= 0
  ? String(process.argv[expectedShaIndex + 1]).trim().toLowerCase()
  : null

if (!['native', 'manual'].includes(mode)) {
  throw new Error(`--mode must be native or manual, received ${mode}`)
}
if (mode === 'manual' && (!readyOutput || (!installDir && !defaultInstall) || !expectedSha256)) {
  throw new Error(
    'manual mode requires --ready-output, --expected-sha256, and one installation mode',
  )
}
if (installDir && defaultInstall) {
  throw new Error('--install-dir and --default-install are mutually exclusive')
}
if (expectedSha256 && !/^[0-9a-f]{64}$/.test(expectedSha256)) {
  throw new Error('--expected-sha256 must be 64 lowercase hexadecimal characters')
}

const manifest = JSON.parse(await readFile(manifestPath, 'utf8'))
assert.equal(manifest.schemaVersion, 1)
assert.equal(manifest.version, expectedVersion)
assert.equal(manifest.tag, `v${expectedVersion}`)
assert.equal(manifest.prerelease, false, 'v0.5.3 stable must rehearse a final update')

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
try {
  app = await launchPackagedCandidate({
    executablePath,
    userDataDir,
    model: 'opensquilla-real-updater-rehearsal',
    env: {
      GITHUB_ACTIONS: '0',
      OPENSQUILLA_DESKTOP_DISABLE_AUTO_UPDATE: '0',
      OPENSQUILLA_DESKTOP_UPDATE_CHANNEL_ROOT: channelRoot,
      OPENSQUILLA_DESKTOP_UPDATE_SOURCE: 'oss',
      OPENSQUILLA_RECOVERY_OFFLINE: '1',
      OPENSQUILLA_TESTING: '0',
    },
  })
  const page = await app.firstWindow({ timeout: 60_000 })
  await waitFor(
    () => page.evaluate(() => typeof window.opensquillaDesktop?.checkForUpdates === 'function'),
    'the official v0.5.3 updater bridge',
  )

  const initial = await page.evaluate(() => window.opensquillaDesktop.getUpdateState())
  assert.equal(initial.currentVersion, '0.5.3')

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
  assert.equal(available.installMode, mode)
  assert.ok(
    channelRequests >= 2 && channelRequests <= 4,
    `official update discovery made an unexpected number of requests: ${channelRequests}`,
  )

  const downloaded = await page.evaluate(() => window.opensquillaDesktop.downloadUpdate())
  assert.equal(downloaded.status, 'downloaded', JSON.stringify(downloaded))
  assert.equal(downloaded.latestVersion, expectedVersion)
  assert.equal(downloaded.progress, 100)
  assert.equal(downloaded.source, 'oss')

  const result = {
    ok: true,
    fromVersion: initial.currentVersion,
    toVersion: expectedVersion,
    tag: manifest.tag,
    source: downloaded.source,
    installMode: downloaded.installMode,
    channelRequests,
    executable: basename(executablePath),
    oldPid: app.process().pid,
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

    // Start the exact installer while v0.5.3 is still running. It may reject,
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
        'installer reported success while the official v0.5.3 process remained live',
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
        throw new Error('official v0.5.3 did not hand off to quitAndInstall')
      }),
    ])
    handedOff = true
    app = null
    console.log(JSON.stringify(result))
  }
} finally {
  if (app && !handedOff) await app.close().catch(() => {})
  await new Promise((resolveClose) => server.close(resolveClose))
}
