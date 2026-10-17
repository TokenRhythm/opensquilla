import { strict as assert } from 'node:assert'
import {
  lstat,
  mkdir,
  mkdtemp,
  readFile,
  readdir,
  realpath,
  rm,
  writeFile,
} from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'
import { fileURLToPath } from 'node:url'
import { _electron as electron } from 'playwright'
import { withDeadline } from './e2e-shutdown-helpers.mjs'

const scriptDir = dirname(fileURLToPath(import.meta.url))
const packageRoot = resolve(scriptDir, '..')
const repoRoot = resolve(packageRoot, '../..')

async function exists(path) {
  try {
    await lstat(path)
    return true
  } catch (error) {
    if (error?.code === 'ENOENT') return false
    throw error
  }
}

async function waitFor(check, label, timeoutMs = 90_000) {
  const startedAt = Date.now()
  let lastError
  while (Date.now() - startedAt < timeoutMs) {
    try {
      const value = await check()
      if (value) return value
    } catch (error) {
      lastError = error
    }
    await delay(250)
  }
  throw new Error(`Timed out waiting for ${label}: ${lastError?.message || lastError || ''}`)
}

function launchEnvironment(isolatedHome) {
  const inherited = { ...process.env }
  for (const name of Object.keys(inherited)) {
    // XAUTHORITY matches the credential pattern below, but stripping it leaves
    // Electron unable to authenticate against the xvfb display on Linux CI.
    if (name === 'DISPLAY' || name === 'XAUTHORITY') continue
    const upperName = name.toUpperCase()
    if (
      upperName.startsWith('OPENSQUILLA_')
      || ['HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY'].includes(upperName)
      || /(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH)/i.test(name)
    ) delete inherited[name]
  }
  return {
    ...inherited,
    HOME: isolatedHome,
    USERPROFILE: isolatedHome,
    APPDATA: join(isolatedHome, 'AppData', 'Roaming'),
    LOCALAPPDATA: join(isolatedHome, 'AppData', 'Local'),
    TEMP: join(isolatedHome, 'AppData', 'Local', 'Temp'),
    TMP: join(isolatedHome, 'AppData', 'Local', 'Temp'),
    OPENSQUILLA_DESKTOP_REPO_ROOT: repoRoot,
    OPENSQUILLA_DESKTOP_SECRET_STORAGE: 'plain',
    OPENSQUILLA_USER_STATE_DIR: join(isolatedHome, 'user-state'),
    OPENSQUILLA_TEST_PROFILE_LOCK_ROOT: '1',
    OPENSQUILLA_DESKTOP_DISABLE_AUTO_UPDATE: '1',
    OPENSQUILLA_OPENROUTER_LIVE_PRICING: '0',
    OPENSQUILLA_GATEWAY_WORKSPACE_DIR: '',
    OPENSQUILLA_WORKSPACE_DIR: '',
    OPENSQUILLA_GATEWAY_STATE_DIR: '',
    HTTP_PROXY: 'http://127.0.0.1:1',
    HTTPS_PROXY: 'http://127.0.0.1:1',
    ALL_PROXY: 'http://127.0.0.1:1',
    NO_PROXY: '127.0.0.1,localhost',
  }
}

const root = await realpath(await mkdtemp(join(tmpdir(), 'opensquilla-cleanup-e2e-')))
const userData = join(root, 'user-data')
const isolatedHome = join(root, 'home')
const primaryHome = join(userData, 'opensquilla')
const workspace = join(primaryHome, 'workspace')
const state = join(primaryHome, 'state')
const consolidationBackupRoot = join(
  userData,
  'backups',
  'profile-consolidation',
  'synthetic-transaction',
)
const consolidationBackupMarker = join(
  consolidationBackupRoot,
  'recovery-profiles',
  '31234567-89ab-4cde-8fab-0123456789ab',
  'opensquilla',
  'archived-recovery-data.txt',
)
const cleanupJournal = join(userData, '.opensquilla.profile-cleanup.json')
// Keep diagnostic output outside userData: delete-all removes every approved
// userData entry, and its detached helper outlives Electron's event listeners.
const helperDiagnostics = join(root, 'cleanup-helper-diagnostics')
const deleteAllHelperTimeoutMs = process.platform === 'win32' ? 90_000 : 30_000
let desktopApp

async function observeDeleteAllHelper(app) {
  await mkdir(helperDiagnostics, { recursive: true })
  await app.evaluate((_electron, directory) => {
    // Playwright's evaluate VM has no dynamic-import callback. Update the
    // built-in named spawn binding used by main.ts without a product test hook.
    const fs = process.getBuiltinModule('fs')
    const childProcess = process.getBuiltinModule('child_process')
    const { syncBuiltinESMExports } = process.getBuiltinModule('module')
    const { join } = process.getBuiltinModule('path')
    const originalSpawn = childProcess.spawn
    const record = (event, detail = {}) => fs.appendFileSync(
      join(directory, 'events.jsonl'),
      `${JSON.stringify({ at: new Date().toISOString(), event, ...detail })}\n`,
    )
    childProcess.spawn = function (command, args, options) {
      if (!Array.isArray(args) || !args.includes('cleanup-apply') || !args.includes('--after-parent-exit')) {
        return originalSpawn.apply(this, arguments)
      }
      childProcess.spawn = originalSpawn
      syncBuiltinESMExports()
      record('spawn_requested', { command, args, cwd: options.cwd, stdio: options.stdio })
      // Regular inherited file descriptors preserve output after app.exit
      // without adding pipes, readers, or references that keep Electron alive.
      const descriptors = []
      let child
      try {
        descriptors.push(fs.openSync(join(directory, 'stdout.log'), 'w'))
        descriptors.push(fs.openSync(join(directory, 'stderr.log'), 'w'))
        child = originalSpawn.call(this, command, args, {
          ...options,
          stdio: [options.stdio[0], ...descriptors],
        })
      } finally {
        for (const descriptor of descriptors) fs.closeSync(descriptor)
      }
      record('spawn_returned', { pid: child.pid, electronPid: process.pid })
      child.once('error', (error) => record('spawn_error', { message: error.message, code: error.code }))
      child.once('exit', (code, signal) => record('exit_before_electron', { code, signal }))
      child.stdin.once('error', (error) => record('stdin_error', { message: error.message, code: error.code }))
      const originalWrite = child.stdin.write
      child.stdin.write = function (...values) {
        record('approval_write', { bytes: Buffer.byteLength(values[0], 'utf8') })
        const last = values.length - 1
        if (typeof values[last] === 'function') {
          const callback = values[last]
          values[last] = function (error) {
            record('approval_write_callback', { error: error?.message ?? null })
            return callback.apply(this, arguments)
          }
        }
        return originalWrite.apply(this, values)
      }
      return child
    }
    syncBuiltinESMExports()
    record('observer_installed')
  }, helperDiagnostics)
}

async function reportDeleteAllHelperFailure() {
  const files = [
    ['events', join(helperDiagnostics, 'events.jsonl')],
    ['electronExit', join(helperDiagnostics, 'electron-exit.json')],
    ['stdout', join(helperDiagnostics, 'stdout.log')],
    ['stderr', join(helperDiagnostics, 'stderr.log')],
    ['cleanupJournal', cleanupJournal],
  ]
  const diagnostics = Object.fromEntries(await Promise.all(files.map(async ([name, path]) => [
    name,
    (await readFile(path, 'utf8').catch(() => '')).slice(0, 65_536),
  ])))
  console.error(JSON.stringify({ event: 'delete_all_helper_failure', ...diagnostics }))
}

async function pendingDeleteAllTargets() {
  const targets = [
    ['primary-home', primaryHome],
    ['consolidation-backup', consolidationBackupRoot],
    ['desktop-logs', join(userData, 'logs')],
  ]
  const states = await Promise.all(targets.map(async ([name, path]) => ({
    name,
    exists: await exists(path),
  })))
  return states.filter((state) => state.exists).map((state) => state.name)
}

async function seedProfileContext(updatedAt) {
  await writeFile(join(userData, 'desktop-locale'), 'en', 'utf8')
  await writeFile(join(userData, 'desktop-profile-context.json'), `${JSON.stringify({
    schema_version: 1,
    active_profile_kind: 'primary',
    active_recovery_id: null,
    attention_acknowledgement: null,
    updated_at: updatedAt,
  }, null, 2)}\n`, 'utf8')
}

async function waitForRepairPage(app, label) {
  return waitFor(async () => {
    for (const candidate of app.windows()) {
      if (candidate.isClosed()) continue
      if (await candidate.locator('#recoveryPanel.visible').count().catch(() => 0)) {
        return candidate
      }
    }
    return null
  }, label)
}

try {
  await mkdir(workspace, { recursive: true })
  await mkdir(state, { recursive: true })
  await mkdir(dirname(consolidationBackupMarker), { recursive: true })
  await Promise.all([
    mkdir(join(isolatedHome, 'AppData', 'Roaming'), { recursive: true }),
    mkdir(join(isolatedHome, 'AppData', 'Local', 'Temp'), { recursive: true }),
  ])
  await writeFile(join(workspace, 'IDENTITY.md'), 'synthetic cleanup identity\n', 'utf8')
  await writeFile(join(state, 'sessions.db.keep'), 'synthetic chat state\n', 'utf8')
  await writeFile(consolidationBackupMarker, 'recorded backup must remain\n', 'utf8')
  await writeFile(
    join(primaryHome, 'config.toml'),
    `workspace_dir = ${JSON.stringify(join(root, 'missing-workspace'))}\n`,
    'utf8',
  )
  await writeFile(cleanupJournal, 'synthetic interrupted cleanup authority\n', 'utf8')
  await seedProfileContext('2026-07-13T00:00:00.000Z')

  // Phase 1: an interrupted cleanup journal is a warning, not a startup gate.
  // Startup abandons it automatically — the journal record is archived and
  // every surviving file is preserved — with no repair page and no dialog.
  const identityBeforeAbandon = await readFile(join(workspace, 'IDENTITY.md'))
  const stateBeforeAbandon = await readFile(join(state, 'sessions.db.keep'))
  desktopApp = await electron.launch({
    args: ['--use-mock-keychain', `--user-data-dir=${userData}`, packageRoot],
    env: launchEnvironment(isolatedHome),
  })
  await waitFor(async () => !await exists(cleanupJournal), 'automatic cleanup journal abandonment')
  assert((await readdir(userData)).some((name) => (
    name.startsWith('.opensquilla.profile-cleanup.abandoned.') && name.endsWith('.json')
  )))
  assert.deepEqual(await readFile(join(workspace, 'IDENTITY.md')), identityBeforeAbandon)
  assert.deepEqual(await readFile(join(state, 'sessions.db.keep')), stateBeforeAbandon)
  await desktopApp.close()
  desktopApp = null
  await delay(200)

  // Phase 2: force the repair page through the remaining hard startup gate
  // (config authored by a newer build), then run the trusted
  // delete-current-profile flow from that page.
  await writeFile(
    join(primaryHome, 'config.toml'),
    `workspace_dir = ${JSON.stringify(join(root, 'missing-workspace'))}\nconfig_version = 999\n`,
    'utf8',
  )
  await seedProfileContext('2026-07-13T00:00:01.000Z')
  desktopApp = await electron.launch({
    args: ['--use-mock-keychain', `--user-data-dir=${userData}`, packageRoot],
    env: launchEnvironment(isolatedHome),
  })
  const page = await waitForRepairPage(desktopApp, 'cleanup test primary repair page')
  assert.equal(
    await page.locator('#recoveryCode').textContent(),
    'config_schema_too_new',
  )
  await desktopApp.evaluate(({ dialog }) => {
    dialog.showMessageBox = async () => ({ response: 1, checkboxChecked: false })
  })

  const preview = await page.evaluate(() => (
    window.opensquillaDesktop.inspectDesktopCleanup({ mode: 'delete-current-profile' })
  ))
  assert.equal(preview.ok, true)
  assert.equal(preview.report.mode, 'delete-current-profile')
  assert(preview.report.items.some((item) => (
    item.kind === 'primary-home' && resolve(item.path) === resolve(primaryHome)
  )), JSON.stringify(preview.report.items))
  assert(preview.report.items.some((item) => item.kind === 'primary-logs'))

  const process = desktopApp.process()
  const exited = new Promise((resolveExit) => process.once('exit', resolveExit))
  void page.evaluate((previewId) => (
    window.opensquillaDesktop.applyDesktopCleanup({
      previewId,
      acknowledged: true,
      confirmation: '',
    })
  ), preview.previewId).catch(() => {})
  await withDeadline(() => exited, 'Desktop exit after deleting the current profile', 30_000)

  assert.equal(await exists(primaryHome), false)
  assert.equal(await exists(join(userData, 'desktop-credential.json')), false)
  assert.equal(await exists(join(userData, 'desktop-profile-context.json')), false)
  assert.equal(await exists(join(userData, 'logs')), false, 'app.exit must not recreate desktop logs')
  assert.equal(
    await readFile(consolidationBackupMarker, 'utf8'),
    'recorded backup must remain\n',
  )

  // Phase 3: recreate a synthetic blocked primary profile, then verify
  // delete-all is handed to the detached offline helper and does not begin
  // until this Electron process has actually exited.
  desktopApp = null
  await mkdir(workspace, { recursive: true })
  await mkdir(state, { recursive: true })
  await writeFile(join(workspace, 'IDENTITY.md'), 'synthetic delete-all identity\n', 'utf8')
  await writeFile(
    join(primaryHome, 'config.toml'),
    `workspace_dir = ${JSON.stringify(join(root, 'still-missing-workspace'))}\nconfig_version = 999\n`,
    'utf8',
  )
  await seedProfileContext('2026-07-13T00:00:02.000Z')
  await delay(200)

  desktopApp = await electron.launch({
    args: ['--use-mock-keychain', `--user-data-dir=${userData}`, packageRoot],
    env: launchEnvironment(isolatedHome),
  })
  const deleteAllPage = await waitForRepairPage(desktopApp, 'delete-all test primary repair page')
  await desktopApp.evaluate(({ dialog }) => {
    dialog.showMessageBox = async () => ({ response: 1, checkboxChecked: false })
  })
  const deleteAllPreview = await deleteAllPage.evaluate(() => (
    window.opensquillaDesktop.inspectDesktopCleanup({ mode: 'delete-all-user-data' })
  ))
  assert.equal(deleteAllPreview.ok, true)
  assert.equal(await exists(primaryHome), true, 'inspection must remain read-only')
  assert.equal(
    await exists(consolidationBackupRoot),
    true,
    'inspection must not touch recorded consolidation backups',
  )
  await observeDeleteAllHelper(desktopApp)
  const deleteAllProcess = desktopApp.process()
  deleteAllProcess.once('exit', (code, signal) => {
    void writeFile(join(helperDiagnostics, 'electron-exit.json'), JSON.stringify({
      at: new Date().toISOString(), code, signal,
    })).catch(() => {})
  })
  const deleteAllExited = new Promise((resolveExit) => deleteAllProcess.once('exit', resolveExit))
  void deleteAllPage.evaluate((previewId) => (
    window.opensquillaDesktop.applyDesktopCleanup({
      previewId,
      acknowledged: true,
      confirmation: 'DELETE ALL OPENSQUILLA DATA',
    })
  ), deleteAllPreview.previewId).catch(() => {})
  await withDeadline(() => deleteAllExited, 'Desktop exit before the delete-all helper handoff', 30_000)
  try {
    await waitFor(async () => (
      !await exists(primaryHome)
      && !await exists(consolidationBackupRoot)
      && !await exists(join(userData, 'logs'))
    ), 'post-exit delete-all helper completion', deleteAllHelperTimeoutMs)
  } catch (error) {
    const pendingTargets = await pendingDeleteAllTargets()
    throw new Error(
      `${error.message}; pending synthetic targets: ${pendingTargets.join(', ') || 'none'}`,
      { cause: error },
    )
  }

  console.log(JSON.stringify({
    ok: true,
    trustedPreviewVerified: true,
    currentProfileDeleted: true,
    recordedBackupPreservedByCurrentDelete: true,
    noLogWritebackAfterDelete: true,
    automaticCleanupAbandonVerified: true,
    deleteAllStartedAfterExit: true,
    allPrimaryDataAndBackupsDeletedByOfflineHelper: true,
  }))
} catch (error) {
  await reportDeleteAllHelperFailure()
  const desktopLog = await readFile(join(userData, 'logs', 'desktop.log'), 'utf8').catch(() => '')
  if (desktopLog) console.error(desktopLog)
  throw error
} finally {
  await desktopApp?.close().catch(() => {})
  await rm(root, { recursive: true, force: true }).catch(() => {})
}
