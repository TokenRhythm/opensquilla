// Real packaged Electron + frozen Gateway; no source overlay or test timers.
// Usage: node scripts/test-packaged-session-title-startup.mjs
//   --baseline-executable <A.exe> --candidate-executable <B.exe>
//   --python <python.exe> --workdir <new-short-directory> --output <new-report.json>
//   [--pairs 6] [--verify-backfill] [--max-regression-ms 250] [--max-regression-ratio .05]
//   [--read-diagnostics --named 45 --unnamed 5] (separate work-count evidence, not timing)
// These are fresh-process launches with uncontrolled/warm OS caches, never a
// claim about machine-cold cache. Run without concurrent builds or benchmarks.
import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { createReadStream } from 'node:fs'
import { copyFile, mkdir, readFile, stat, writeFile } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { setTimeout as delay } from 'node:timers/promises'
import { _electron as electron } from 'playwright'
import {
  environmentWithoutProviderSecrets, requiredOption, waitFor, writeSyntheticCredential,
  DESKTOP_GATEWAY_STARTUP_TIMEOUT_MS,
} from './packaged-smoke-helpers.mjs'
import { captureElectronProcessIdentity, cleanupPackagedFirstSend } from './packaged-first-send-cleanup.mjs'
import { desktopShutdownEvidenceSince, gatewayProcessSnapshot } from './e2e-shutdown-helpers.mjs'
import {
  desktopProfileFingerprint, loadDesktopGatewayOwnershipRecord, verifyDesktopGatewayOwnership,
} from '../dist/desktop-gateway-ownership.js'

const scriptDirectory = dirname(fileURLToPath(import.meta.url))
const source = resolve(scriptDirectory, '../../..')
const fixture = join(scriptDirectory, 'fixtures', 'packaged-session-title-startup')
const executables = {
  baseline: resolve(requiredOption('--baseline-executable')),
  candidate: resolve(requiredOption('--candidate-executable')),
}
const python = resolve(requiredOption('--python'))
const workdir = resolve(requiredOption('--workdir'))
const output = resolve(requiredOption('--output'))
const option = (name, fallback) => process.argv.includes(name) ? requiredOption(name) : fallback
const pairs = Number(option('--pairs', '6'))
const verifyBackfill = process.argv.includes('--verify-backfill')
const readDiagnostics = process.argv.includes('--read-diagnostics')
const named = Number(option('--named', '50'))
const unnamed = Number(option('--unnamed', '0'))
const maxRegressionMs = Number(option('--max-regression-ms', '250'))
const maxRegressionRatio = Number(option('--max-regression-ratio', '.05'))
const timeout = DESKTOP_GATEWAY_STARTUP_TIMEOUT_MS + 30_000
assert.equal(process.platform, 'win32', 'This probe requires native Windows')
assert.ok(Number.isInteger(pairs) && pairs >= 0 && (pairs > 0 || verifyBackfill))
assert.ok(Number.isInteger(named) && Number.isInteger(unnamed)
  && Math.min(named, unnamed) >= 0 && named + unnamed > 0 && named + unnamed <= 50)
assert.ok(Number.isFinite(maxRegressionMs) && maxRegressionMs >= 0)
assert.ok(Number.isFinite(maxRegressionRatio) && maxRegressionRatio >= 0)
for (const path of [python, ...Object.values(executables)]) assert.ok((await stat(path)).isFile())
await mkdir(dirname(workdir), { recursive: true })
await mkdir(workdir) // Refuse to reuse a profile or overwrite previous evidence.
await mkdir(dirname(output), { recursive: true })

async function sha256(path) {
  const hash = createHash('sha256')
  for await (const chunk of createReadStream(path)) hash.update(chunk)
  return hash.digest('hex')
}
const report = {
  kind: 'packaged-session-title-startup-v1',
  ok: false,
  cacheState: 'fresh process; OS cache uncontrolled, normally warm; no machine reboot/cache flush',
  fixtureScope: 'synthetic SQLite; current schema with legacy NULL content_byte_length',
  restartPreparation: 'same profile; index metadata completed offline only after natural process exit',
  instrumentation: 'passive WebSocket frames and DOM observer, no request routing or product timer override',
  readDiagnostics,
  timingEligible: !readDiagnostics && pairs > 0,
  buildDetection: 'Common build workers checked before launch and after startup; not full host isolation',
  hostBuildChecks: [],
  thresholds: { maxRegressionMs, maxRegressionRatio },
  executables: {},
  pairs,
  boots: [],
}
for (const [label, executable] of Object.entries(executables)) {
  const resources = join(dirname(executable), 'resources')
  report.executables[label] = {
    path: executable, sha256: await sha256(executable),
    appAsarSha256: await sha256(join(resources, 'app.asar')),
    gatewaySha256: await sha256(join(resources, 'runtime', 'gateway', 'opensquilla-gateway', 'opensquilla-gateway.exe')),
    gatewayBuildSha256: await sha256(join(resources, 'runtime', 'gateway', 'gateway-build.json')),
  }
}
await writeFile(output, JSON.stringify(report, null, 2) + '\n', { flag: 'wx' })
const persist = () => writeFile(output, JSON.stringify(report, null, 2) + '\n')
const cleanEnv = environmentWithoutProviderSecrets(process.env)
for (const name of Object.keys(cleanEnv)) {
  if (/^(?:OPENSQUILLA_|UV_)/i.test(name)
    || /^(?:ELECTRON_RUN_AS_NODE|PYTHONPATH|PYTHONHOME)$/i.test(name)) delete cleanEnv[name]
}
function environment(root) {
  return {
    ...cleanEnv,
    HOME: join(root, 'home'), USERPROFILE: join(root, 'home'),
    APPDATA: join(root, 'appdata'), LOCALAPPDATA: join(root, 'localappdata'),
    TEMP: join(root, 'temp'), TMP: join(root, 'temp'),
    OPENSQUILLA_USER_STATE_DIR: join(root, 'user-state'),
    OPENSQUILLA_TEST_PROFILE_LOCK_ROOT: '1',
    OPENSQUILLA_DESKTOP_SECRET_STORAGE: 'plain',
    OPENSQUILLA_DESKTOP_DISABLE_AUTO_UPDATE: '1',
    OPENSQUILLA_OPENROUTER_LIVE_PRICING: '0',
    OPENSQUILLA_TESTING: '0', GITHUB_ACTIONS: '0',
    ...(readDiagnostics ? { OPENSQUILLA_READ_PATH_DIAGNOSTICS: '1' } : {}),
    HTTP_PROXY: 'http://127.0.0.1:1', HTTPS_PROXY: 'http://127.0.0.1:1', ALL_PROXY: 'http://127.0.0.1:1',
    http_proxy: 'http://127.0.0.1:1', https_proxy: 'http://127.0.0.1:1', all_proxy: 'http://127.0.0.1:1',
    NO_PROXY: '127.0.0.1,localhost,::1', no_proxy: '127.0.0.1,localhost,::1',
  }
}
function runSeed(args) {
  return JSON.parse(execFileSync(python, [join(fixture, 'seed.py'), ...args], {
    encoding: 'utf8', windowsHide: true, env: cleanEnv,
  }))
}
function protocolGuard(action) {
  return JSON.parse(execFileSync('pwsh.exe', [
    '-NoLogo', '-NoProfile', '-NonInteractive', '-File', join(fixture, 'protocol-association-guard.ps1'),
    '-Action', action, '-EvidenceDirectory', workdir,
    '-BaselineExecutable', executables.baseline, '-CandidateExecutable', executables.candidate,
  ], { encoding: 'utf8', windowsHide: true }))
}
async function assertNoConcurrentBuilds(label, pair, phase, boundary) {
  if (!report.timingEligible) return
  const state = JSON.parse(execFileSync('pwsh.exe', [
    '-NoLogo', '-NoProfile', '-NonInteractive', '-File', join(fixture, 'inspect-build-processes.ps1'),
  ], { encoding: 'utf8', windowsHide: true }))
  report.hostBuildChecks.push({ label, pair, phase, boundary, ...state })
  await persist()
  assert.equal(state.clear, true, `Concurrent build detected at ${boundary}; timing run cannot pass`)
}
async function prepareProfile(root, seedPath) {
  const userData = join(root, 'u')
  const profile = join(userData, 'opensquilla')
  const env = environment(root)
  for (const path of [join(profile, 'state'), join(profile, 'workspace'), env.HOME,
    env.APPDATA, env.LOCALAPPDATA, env.TEMP]) await mkdir(path, { recursive: true })
  const database = join(profile, 'state', 'sessions.db')
  await copyFile(seedPath, database)
  await writeFile(join(profile, 'config.toml'), [
    '# Synthetic session-title startup profile',
    'config_version = 2',
    `state_dir = ${JSON.stringify(join(profile, 'state'))}`,
    `workspace_dir = ${JSON.stringify(join(profile, 'workspace'))}`,
    '[llm]', 'provider = "ollama"', 'model = "synthetic-title-startup"',
    'base_url = "http://127.0.0.1:9"', 'context_window_tokens = 131072',
    '[squilla_router]', 'enabled = false', '[llm_ensemble]', 'enabled = false',
    '[privacy]', 'disable_network_observability = true', '',
  ].join('\n'))
  await writeFile(join(profile, 'desktop-layout-v2.json'), JSON.stringify({
    schema_version: 2, migratedAt: '2026-10-20T00:00:00Z', moved: [],
  }) + '\n')
  await writeSyntheticCredential(userData, {
    baseUrl: 'http://127.0.0.1:9', model: 'synthetic-title-startup', disableNetworkObservability: true,
  })
  return { root, userData, profile, database, env }
}

// Installed at document start, with a single coalesced check per DOM mutation.
// Capture user-visible timestamps inside the renderer instead of adding polling latency.
function installDomProbe({ expected, requireBeforeBoundaries = false }) {
  if (requireBeforeBoundaries
    && (document.querySelector('.topbar[data-connection-state="connected"]')
      || document.querySelector(`[data-session-key="${expected.key}"] .sidebar-history-title`))) {
    throw new Error('DOM probe missed a measured startup boundary')
  }
  const probe = window.__sessionTitleStartupProbe = {}
  let scheduled = false
  const check = () => {
    scheduled = false
    const now = Date.now()
    const connected = Boolean(document.querySelector('.topbar[data-connection-state="connected"]'))
    if (!probe.connectedAt && connected) probe.connectedAt = now
    const composer = document.querySelector('.chat-textarea')
    if (!probe.connectedComposerAt && connected && composer && composer.getClientRects().length
      && !composer.disabled && !composer.readOnly) probe.connectedComposerAt = now
    const row = document.querySelector(`[data-session-key="${expected.key}"] .sidebar-history-title`)
    if (row?.getClientRects().length) {
      if (!probe.listVisibleAt) {
        probe.listVisibleAt = now
        probe.firstTitle = row.textContent?.trim()
      }
      if (row.textContent?.trim() === expected.expectedTitle) probe.titleCorrectAt ||= now
    }
  }
  new MutationObserver(() => {
    if (!scheduled) { scheduled = true; requestAnimationFrame(check) }
  }).observe(document, { subtree: true, childList: true, characterData: true, attributes: true })
  document.addEventListener('DOMContentLoaded', check, { once: true })
  check()
}
function parseFrame(payload) {
  try { return JSON.parse(typeof payload === 'string' ? payload : payload.toString()) } catch { return null }
}
function parseDesktopLog(content) {
  return content.split(/\r?\n/).flatMap(line => {
    try { return [JSON.parse(line)] } catch { return [] }
  })
}
function titleReadCounters(content) {
  const records = content.split(/\r?\n/).flatMap(line => {
    let record
    try { record = JSON.parse(line.slice(line.indexOf('{'))) } catch { return [] }
    return typeof record.operation === 'string' && record.operation.startsWith('session_titles:')
      ? [{ operation: record.operation, rows: record.rows, queryMs: record.query_ms }] : []
  })
  return {
    candidateSelects: records.filter(record => record.operation === 'session_titles:candidates').length,
    contentSelects: records.filter(record => record.operation === 'session_titles:content').length,
    contentRows: records.filter(record => record.operation === 'session_titles:content')
      .reduce((sum, record) => sum + (record.rows ?? 0), 0),
    records,
  }
}

async function boot(label, profile, manifest, { pair, phase, observeMaintenance = false }) {
  await assertNoConcurrentBuilds(label, pair, phase, 'before-launch')
  const result = { label, pair, phase, ok: false, pageErrors: [], rpc: { requests: [], responses: [], changes: [] } }
  report.boots.push(result)
  await persist()
  let app
  let page
  let identity
  let failure
  let startedAt
  let startupCheckpoint
  let owner
  let beforeQuitLog = ''
  let lastDesktopLog = ''
  const firstExpected = manifest.sessions[0]
  const expected = new Map(manifest.sessions.map(session => [session.key, session]))
  const logPath = join(profile.userData, 'logs', 'desktop.log')
  const previousLog = await readFile(logPath, 'utf8').catch(() => '')
  const gatewayLogPath = join(profile.userData, 'logs', 'gateway.log')
  const previousGatewayLog = await readFile(gatewayLogPath, 'utf8').catch(() => '')
  const ownershipDirectory = join(profile.userData, 'gateway-ownership', desktopProfileFingerprint(profile.profile))
  const attachedPages = new WeakSet()
  function attachPage(candidatePage) {
    if (attachedPages.has(candidatePage)) return
    attachedPages.add(candidatePage)
    candidatePage.on('pageerror', error => result.pageErrors.push(error.message))
    candidatePage.on('websocket', socket => {
      const pending = new Map()
      socket.on('framesent', ({ payload }) => {
        const frame = parseFrame(payload)
        if (frame?.type !== 'req' || frame.method !== 'sessions.list') return
        const request = {
          elapsedMs: Date.now() - startedAt, method: frame.method,
          cursor: Boolean(frame.params?.cursor), limit: frame.params?.limit ?? null,
          view: frame.params?.view ?? null,
        }
        pending.set(frame.id, request)
        result.rpc.requests.push(request)
      })
      socket.on('framereceived', ({ payload }) => {
        const frame = parseFrame(payload)
        if (frame?.type === 'event' && frame.event === 'sessions.changed') {
          result.rpc.changes.push({
            elapsedMs: Date.now() - startedAt, reason: frame.payload?.reason ?? null,
            key: frame.payload?.key ?? null, scope: frame.payload?.scope ?? null,
          })
        }
        if (frame?.type !== 'res' || !pending.has(frame.id)) return
        const request = pending.get(frame.id)
        pending.delete(frame.id)
        const rows = (frame.payload?.sessions ?? []).filter(row => expected.has(row.key ?? row.session_key))
        result.rpc.responses.push({
          elapsedMs: Date.now() - startedAt, durationMs: Date.now() - startedAt - request.elapsedMs,
          view: request.view,
          ok: frame.ok === true, fixtureRows: rows.length,
          correctTitles: rows.filter(row => row.title === expected.get(row.key ?? row.session_key).expectedTitle).length,
          webChatTitles: rows.filter(row => row.title === 'WebChat').length,
        })
      })
    })
  }
  try {
    startedAt = Date.now()
    app = await electron.launch({
      executablePath: executables[label],
      args: ['--use-mock-keychain', `--user-data-dir=${profile.userData}`],
      env: profile.env, timeout,
    })
    app.context().on('page', attachPage)
    for (const candidatePage of app.context().pages()) attachPage(candidatePage)
    await app.context().addInitScript(installDomProbe, { expected: firstExpected })
    identity = await captureElectronProcessIdentity(app)
    result.identity = identity
    result.launchReturnedMs = Date.now() - startedAt
    const actual = await app.evaluate(({ app }) => ({ version: app.getVersion(), userData: app.getPath('userData') }))
    assert.equal(resolve(actual.userData).toLowerCase(), profile.userData.toLowerCase())
    result.version = actual.version
    page = await app.firstWindow({ timeout })
    await page.waitForURL(url => url.protocol === 'opensquilla-app:' && url.hostname === 'desktop', { timeout })
    // Desktop can preload its landing document before the launch handshake
    // returns. Attach to that document only if none of the measured boundaries
    // has occurred; never reload or accept a late observation as first paint.
    if (!await page.evaluate(() => Boolean(window.__sessionTitleStartupProbe))) {
      // Guard and install in one renderer task: there is no cross-RPC gap in
      // which connection or list visibility can precede the observer.
      await page.evaluate(installDomProbe, { expected: firstExpected, requireBeforeBoundaries: true })
      result.domProbeAttachment = 'preloaded document before measured boundaries'
    } else {
      result.domProbeAttachment = 'document start'
    }
    await page.waitForFunction(() => {
      const probe = window.__sessionTitleStartupProbe
      return probe?.connectedAt && probe.connectedComposerAt && probe.listVisibleAt
    }, undefined, { timeout })
    const firstListResponse = await waitFor(() => result.rpc.responses.find(response =>
      response.view === 'session-list-v1' && response.fixtureRows > 0),
      'passive first sessions.list response', 10_000)
    const dom = await page.evaluate(() => window.__sessionTitleStartupProbe)
    result.timings = Object.fromEntries(Object.entries(dom)
      .filter(([name]) => name.endsWith('At')).map(([name, value]) => [name.replace(/At$/, 'Ms'), value - startedAt]))
    result.firstVisibleTitle = dom.firstTitle
    lastDesktopLog = (await readFile(logPath, 'utf8')).slice(previousLog.length)
    const ready = parseDesktopLog(lastDesktopLog).find(item => item.event === 'gateway_ready')
    assert.ok(ready, 'desktop must log its own Gateway ready boundary')
    result.timings.gatewayReadyMs = Date.parse(ready.at) - startedAt
    result.timings.desktopReadyElapsedMs = ready.elapsedMs
    result.timings.firstListResponseMs = firstListResponse.elapsedMs
    result.firstResponse = { ...firstListResponse }
    result.profileStartup = parseDesktopLog(lastDesktopLog)
      .filter(item => ['profile_inspect_started', 'profile_inspect_finished', 'recovery_child_spawned'].includes(item.event))
    startupCheckpoint = Date.now()
    await assertNoConcurrentBuilds(label, pair, phase, 'after-startup')
    result.protocolAfterStartup = protocolGuard('restore')
    assert.equal(result.protocolAfterStartup.matchesOriginal, true)
    const ownership = loadDesktopGatewayOwnershipRecord(ownershipDirectory)
    assert.equal(ownership.status, 'valid')
    assert.equal(await verifyDesktopGatewayOwnership(ownership.record), true)
    owner = ownership.record
    result.gateway = { pid: owner.pid, startIdentity: owner.start_identity, identityVerified: true }
    if (label === 'candidate' || phase === 'ready-restart') {
      const first = firstListResponse
      assert.equal(first.fixtureRows, manifest.sessions.length)
      const requiredCorrect = phase === 'legacy-first' ? manifest.named : manifest.sessions.length
      assert.equal(first.correctTitles, requiredCorrect, 'first list must use the expected title source')
      if (phase === 'ready-restart' || firstExpected.storedTitle) {
        assert.equal(result.firstVisibleTitle, firstExpected.expectedTitle,
          'the first visible sidebar title must match the valid saved or indexed title')
      }
    }
    if (observeMaintenance) {
      // Keep the unchanged 60s production delay. Readiness does not wait on this.
      const endAt = startedAt + Math.max(result.timings.gatewayReadyMs, result.timings.listVisibleMs) + 68_000
      while (Date.now() < endAt) {
        await delay(Math.min(500, endAt - Date.now()))
      }
      result.maintenance = {
        observedAfterStartupMs: Date.now() - startupCheckpoint,
        changes: result.rpc.changes.length,
        listRequestsAfterStartup: result.rpc.requests.filter(item =>
          item.view === 'session-list-v1' && item.elapsedMs > startupCheckpoint - startedAt).length,
        finalResponse: result.rpc.responses.filter(item => item.view === 'session-list-v1').at(-1),
        visibleTitles: await page.evaluate(sessions => {
          const expected = new Map(sessions.map(session => [session.key, session]))
          return [...document.querySelectorAll('[data-session-key] .sidebar-history-title')].flatMap(node => {
            const key = node.closest('[data-session-key]')?.getAttribute('data-session-key')
            const fixture = expected.get(key)
            const rect = node.getBoundingClientRect()
            if (!fixture || !node.getClientRects().length
              || rect.width <= 0 || rect.height <= 0 || rect.top >= innerHeight || rect.bottom <= 0) return []
            return [{
              key, title: node.textContent?.trim(), expectedTitle: fixture.expectedTitle,
              hasStoredTitle: Boolean(fixture.storedTitle),
            }]
          })
        }, manifest.sessions),
      }
      if (label === 'candidate') {
        const expectedChanges = phase === 'legacy-first' ? 1 : 0
        assert.equal(result.maintenance.changes, expectedChanges,
          'one completed maintenance pass must emit at most one directory invalidation')
        assert.equal(result.maintenance.listRequestsAfterStartup, expectedChanges,
          'maintenance must trigger one refresh when rows changed and none otherwise')
        assert.equal(result.maintenance.finalResponse.correctTitles, manifest.sessions.length)
        assert.ok(result.maintenance.visibleTitles.length > 0, 'fixture rows must be visible after maintenance')
        assert.ok(result.maintenance.visibleTitles.some(row => !row.hasStoredTitle),
          'at least one genuinely untitled fixture must be visible for the refresh check')
        for (const row of result.maintenance.visibleTitles) {
          assert.equal(row.title, row.expectedTitle,
            `visible sidebar title must refresh without focus or manual reload: ${row.key}`)
        }
      }
    }
    await page.screenshot({ path: join(profile.root, `${phase}.png`) })
    beforeQuitLog = await readFile(logPath, 'utf8')
  } catch (error) {
    failure = error
    result.error = error.stack || String(error)
    await persist()
    if (page) await page.screenshot({ path: join(profile.root, `${phase}-failed.png`) }).catch(() => {})
    // A failed observer assertion can precede Gateway readiness. Let the owned
    // launch settle before requesting ordinary quit, which otherwise asks the
    // user to confirm unknown startup activity. This never rescues the sample.
    if (page && app) {
      await page.waitForSelector('.topbar[data-connection-state="connected"]', { timeout })
        .catch(() => {})
      if (!owner) {
        const ownership = loadDesktopGatewayOwnershipRecord(ownershipDirectory)
        if (ownership.status === 'valid' && await verifyDesktopGatewayOwnership(ownership.record)) {
          owner = ownership.record
          result.gateway = { pid: owner.pid, startIdentity: owner.start_identity, identityVerified: true }
        }
      }
      beforeQuitLog = await readFile(logPath, 'utf8').catch(() => '')
    }
  } finally {
    try {
      await cleanupPackagedFirstSend({
        app, processIdentity: identity, diagnostics: () => ({ label, phase, pageErrors: result.pageErrors }),
      })
      if (owner) {
        assert.equal(gatewayProcessSnapshot(owner).alive, false)
        assert.equal(loadDesktopGatewayOwnershipRecord(ownershipDirectory).status, 'missing')
        assert.deepEqual(desktopShutdownEvidenceSince(beforeQuitLog, await readFile(logPath, 'utf8')), {
          gatewayExitLogged: true, committedExitLogged: true,
        })
      }
      result.normalQuitVerified = Boolean(owner)
    } catch (error) {
      failure ??= error
      result.cleanupError = error.stack || String(error)
    }
    try { result.protocolAfterQuit = protocolGuard('restore') } catch (error) { failure ??= error }
    for (const type of ['desktop', 'gateway']) {
      const content = await readFile(join(profile.userData, 'logs', `${type}.log`), 'utf8').catch(() => '')
      await writeFile(join(profile.root, `${phase}-${type}.log`), content)
      if (type === 'gateway' && readDiagnostics) {
        result.titleReadCounters = titleReadCounters(content.slice(previousGatewayLog.length))
      }
    }
    if (readDiagnostics && !failure && label === 'baseline'
      && result.titleReadCounters.candidateSelects === 0) {
      failure = new Error('Baseline title-read diagnostics missing; zero rows cannot count as optimization evidence')
    }
    result.ok = !failure && result.pageErrors.length === 0
    if (failure) result.error = failure.stack || String(failure)
    await persist()
  }
  console.log(JSON.stringify({ label, pair, phase, ok: result.ok, timings: result.timings, error: result.error }))
  if (!result.ok) throw failure ?? new Error(`Renderer page errors: ${result.pageErrors.join('; ')}`)
  return result
}

function percentile(values, fraction) {
  const sorted = [...values].sort((a, b) => a - b)
  return sorted[Math.max(0, Math.ceil(sorted.length * fraction) - 1)]
}
function summarize() {
  const summaries = []
  for (const phase of ['legacy-first', 'ready-restart']) {
    const boots = report.boots.filter(boot => boot.pair >= 0 && boot.phase === phase && boot.ok)
    for (const metric of ['gatewayReadyMs', 'connectedMs', 'connectedComposerMs', 'listVisibleMs', 'firstListResponseMs']) {
      const baseline = boots.filter(boot => boot.label === 'baseline').map(boot => boot.timings[metric])
      const candidate = boots.filter(boot => boot.label === 'candidate').map(boot => boot.timings[metric])
      if (!baseline.length || baseline.length !== candidate.length) continue
      const baselineP50 = percentile(baseline, .5)
      const candidateP50 = percentile(candidate, .5)
      const baselineP95 = percentile(baseline, .95)
      const candidateP95 = percentile(candidate, .95)
      const thresholdMs = Math.max(maxRegressionMs, baselineP50 * maxRegressionRatio)
      summaries.push({
        phase, metric, samplesPerVariant: baseline.length,
        baselineP50, candidateP50, p50DeltaMs: candidateP50 - baselineP50,
        baselineP95, candidateP95, p95DeltaMs: candidateP95 - baselineP95, thresholdMs,
        withinObservedNoiseBudget: candidateP50 - baselineP50 <= thresholdMs
          && candidateP95 - baselineP95 <= Math.max(maxRegressionMs, baselineP95 * maxRegressionRatio),
      })
    }
  }
  return summaries
}

let protocolCaptured = false
try {
  report.protocolSnapshot = protocolGuard('snapshot')
  protocolCaptured = true
  const seedPath = join(workdir, 'seed.db')
  report.fixture = runSeed(['--database', seedPath, '--source', source, '--named', String(named), '--unnamed', String(unnamed)])
  for (let pair = 0; pair < pairs; pair++) {
    const order = pair % 2 ? ['candidate', 'baseline'] : ['baseline', 'candidate']
    for (const label of order) {
      const profile = await prepareProfile(join(workdir, `p${pair}-${label[0]}`), seedPath)
      await boot(label, profile, report.fixture, { pair, phase: 'legacy-first' })
      const completed = runSeed(['--database', profile.database, '--complete-metadata'])
      assert.equal(completed.completedRows, report.fixture.pendingRows,
        'startup must finish before the real delayed backfill runs')
      await boot(label, profile, report.fixture, { pair, phase: 'ready-restart' })
    }
  }
  if (verifyBackfill) {
    const seedPath = join(workdir, 'maintenance-seed.db')
    const manifest = runSeed(['--database', seedPath, '--source', source, '--named', '5', '--unnamed', '5'])
    report.maintenanceFixture = manifest
    for (const label of ['baseline', 'candidate']) {
      const profile = await prepareProfile(join(workdir, `maintenance-${label[0]}`), seedPath)
      await boot(label, profile, manifest, { pair: -1, phase: 'legacy-first', observeMaintenance: true })
      // The preceding live run, not an offline fixture edit, must have indexed all rows.
      const completed = runSeed(['--database', profile.database, '--complete-metadata'])
      assert.equal(completed.completedRows, 0, 'live maintenance must complete the fixture')
      await boot(label, profile, manifest, { pair: -1, phase: 'ready-restart', observeMaintenance: true })
    }
  }
  report.summary = readDiagnostics ? [] : summarize()
  report.observedStartupGatePassed = pairs > 0 && !readDiagnostics
    ? report.summary.length === 10 && report.summary.every(item => item.withinObservedNoiseBudget)
    : null
  report.ok = report.boots.every(boot => boot.ok) && report.observedStartupGatePassed !== false
} catch (error) {
  report.error = error.stack || String(error)
} finally {
  if (protocolCaptured) {
    try { report.protocolFinal = protocolGuard('restore') } catch (error) {
      report.ok = false
      report.protocolFinalError = error.stack || String(error)
    }
  }
  await persist()
}
console.log(JSON.stringify({ output, ok: report.ok, error: report.error, summary: report.summary }))
if (!report.ok) process.exitCode = 1
