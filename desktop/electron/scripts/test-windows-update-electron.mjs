import assert from 'node:assert/strict'
import { createHash, randomUUID } from 'node:crypto'
import { createRequire } from 'node:module'
import { mkdir, mkdtemp, readFile, realpath, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, join, relative, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { _electron as electron } from 'playwright'
import ts from 'typescript'
import { createWindowsUpdateCacheDescriptor, loadWindowsUpdateCache, saveWindowsUpdateCache } from '../dist/windows-update-cache.js'
import { environmentWithoutProviderSecrets, waitFor as waitForPackaged } from './packaged-smoke-helpers.mjs'
import { closeElectronWithDeadline } from './e2e-shutdown-helpers.mjs'

// Process/IPC/UI evidence for the production handoff functions. Signature and
// registry are explicit test seams. This is not a signed/packaged upgrade test.
if (process.platform !== 'win32') {
  console.log('Windows update Electron fixture skipped on this platform.')
  process.exit(0)
}
const packageRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const webRoot = resolve(packageRoot, '../../opensquilla-webui')
const option = name => process.argv.includes(name) ? process.argv[process.argv.indexOf(name) + 1] : null
const executablePath = option('--electron-executable') || process.env.OPENSQUILLA_TEST_ELECTRON_EXECUTABLE
const outputRoot = resolve(option('--output-dir') || (process.env.CI_REPORT_DIR
  ? join(process.env.CI_REPORT_DIR, 'windows-update-electron')
  : await mkdtemp(join(tmpdir(), 'opensquilla-update-electron-evidence-'))))
const waitFor = (check, label, timeoutMs = 10_000) => waitForPackaged(check, label, timeoutMs)
await mkdir(outputRoot, { recursive: true })
const reportPath = join(outputRoot, 'report.json')
const runId = randomUUID()
await writeFile(reportPath, `${JSON.stringify({ ok: false, status: 'running', runId, startedAt: new Date().toISOString() }, null, 2)}\n`)
const isolationRoot = await mkdtemp(join(tmpdir(), 'opensquilla-update-electron-'))
let runningApp
const results = []
try {
const source = await readFile(join(packageRoot, 'src/main.ts'), 'utf8')
const parsed = ts.createSourceFile('main.ts', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS)
const functions = new Map(parsed.statements.filter(ts.isFunctionDeclaration).filter(node => node.name)
  .map(node => [node.name.text, node.getText(parsed)]))
const names = [
  'windowsInstallerActionsSupported', 'windowsUpdateDownloadDirectory', 'clearWindowsUpdateCache',
  'publishVerifiedWindowsInstaller', 'restoreWindowsUpdateCache', 'revalidateReadyWindowsInstaller',
  'desktopUpdateSnapshot', 'publishDesktopUpdateState', 'setDesktopUpdateState',
  'restoreDownloadedUpdateRetryState', 'classifyDesktopUpdateError', 'desktopUpdateErrorMessage',
  'applyWindowsInstaller', 'applyDownloadedUpdate', 'handleMainWindowClose', 'trustedMainWindowControlIpc',
  'desktopUpdateCheckAllowed', 'runDesktopUpdateCheck', 'checkForUpdates', 'showUpdateError',
  'desktopUpdatePlatform', 'resolveDesktopUpdate',
]
for (const name of names) assert.ok(functions.has(name), `production ${name} must exist`)
const statements = []
const wantedChannels = new Set(['desktop:update:managed', 'desktop:update:supported', 'desktop:update:state', 'desktop:update:relaunch', 'desktop:update:check'])
for (const node of parsed.statements) {
  if (!ts.isExpressionStatement(node) || !ts.isCallExpression(node.expression)) continue
  const call = node.expression
  if (!ts.isPropertyAccessExpression(call.expression) || !ts.isStringLiteral(call.arguments[0])) continue
  const owner = call.expression.expression.getText(parsed)
  if ((owner === 'app' && call.expression.name.text === 'on' && call.arguments[0].text === 'before-quit')
    || (owner === 'ipcMain' && call.expression.name.text === 'handle' && wantedChannels.has(call.arguments[0].text))) {
    statements.push(node.getText(parsed))
  }
}
assert.equal(statements.length, wantedChannels.size + 1, 'use all production update IPC handlers and the real quit callback')
const schedulerStatement = parsed.statements.find(statement => ts.isVariableStatement(statement)
  && statement.declarationList.declarations.some(declaration => declaration.name.getText(parsed) === 'desktopUpdateCheckScheduler'))
assert.ok(schedulerStatement, 'extract the actual production scheduler wiring')
const extracted = ts.transpileModule([...names.map(name => functions.get(name)), schedulerStatement.getText(parsed), ...statements].join('\n'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
}).outputText
const template = await readFile(join(packageRoot, 'scripts/fixtures/windows-update-electron/main.template.mjs'), 'utf8')
const fixtureRoot = join(isolationRoot, 'fixture')
await mkdir(fixtureRoot)
await writeFile(join(fixtureRoot, 'main.mjs'), template.replace('// PRODUCTION_DECLARATIONS', extracted))
await writeFile(join(fixtureRoot, 'package.json'), JSON.stringify({ name: 'opensquilla-update-fixture', version: '0.5.4', type: 'module', main: 'main.mjs' }))

const webRequire = createRequire(join(webRoot, 'package.json'))
const { build } = await import(pathToFileURL(webRequire.resolve('vite')).href)
const { default: vue } = await import(pathToFileURL(webRequire.resolve('@vitejs/plugin-vue')).href)
const rendererRoot = join(isolationRoot, 'renderer')
const rendererHtml = join(isolationRoot, 'renderer.html')
const rendererEntry = join(isolationRoot, 'renderer-entry.mjs')
const sourceImport = path => JSON.stringify(join(webRoot, path).replaceAll('\\', '/'))
await writeFile(rendererHtml, '<!doctype html><html lang="en" data-theme="light"><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>OpenSquilla update lifecycle fixture</title><div id="app"></div><script type="module" src="./renderer-entry.mjs"></script></html>')
await writeFile(rendererEntry, `
import { createApp, h } from 'vue'
import { createI18n } from 'vue-i18n'
import Indicator from ${sourceImport('src/components/DesktopUpdateIndicator.vue')}
import Panel from ${sourceImport('src/components/settings/SettingsUpdatePanel.vue')}
import en from ${sourceImport('src/locales/en.json')}
import ${sourceImport('src/assets/base.css')}
import ${sourceImport('src/themes/tokens.ts')}
import ${sourceImport('src/styles/apple-modern.css')}
import ${sourceImport('src/styles/control-visual-system.css')}
import ${sourceImport('src/styles/settings-forms.css')}
createApp({ render: () => h('main', { style: 'padding:48px;max-width:900px;margin:auto' }, [
 h('p', { style:'margin-bottom:24px;opacity:0.65' }, 'Isolated lifecycle test · synthetic installer and Gateway'),
 h('header', { style:'display:flex;justify-content:space-between;margin-bottom:32px' }, [h('h1', 'Desktop updates'), h(Indicator)]),
 h(Panel),
]) }).use(createI18n({ legacy: false, locale: 'en', messages: { en } })).mount('#app')
`)
await build({
  configFile: false, root: isolationRoot, base: './', plugins: [vue()], logLevel: 'warn',
  resolve: { alias: { '@': join(webRoot, 'src'), vue: webRequire.resolve('vue/dist/vue.esm-bundler.js'), 'vue-i18n': webRequire.resolve('vue-i18n') } },
  define: { __VUE_I18N_FULL_INSTALL__: 'true', __VUE_I18N_LEGACY_API__: 'false', __INTLIFY_PROD_DEVTOOLS__: 'false' },
  build: { outDir: rendererRoot, emptyOutDir: true, rollupOptions: { input: rendererHtml } },
})

async function launchCase(name, { installEnabled = true } = {}) {
  const userData = join(isolationRoot, name, 'userData')
  const home = join(isolationRoot, name, 'home')
  const logPath = join(outputRoot, `${name}.jsonl`)
  await writeFile(logPath, '')
  await mkdir(userData, { recursive: true })
  await mkdir(home, { recursive: true })
  const directory = join(userData, 'update-downloads')
  await mkdir(directory)
  const candidate = { tag: 'v0.5.5', version: '0.5.5', installer: 'OpenSquilla-0.5.5-win-x64.exe' }
  const bytes = Buffer.from('Synthetic installer bytes; never executed by this fixture')
  await writeFile(join(directory, candidate.installer), bytes)
  await saveWindowsUpdateCache(directory, createWindowsUpdateCacheDescriptor(candidate, createHash('sha256').update(bytes).digest('hex'), bytes.length))
  const configPath = join(isolationRoot, `${name}.json`)
  await writeFile(configPath, JSON.stringify({ packageRoot, rendererRoot, logPath, isolationRoot, nodePath: process.execPath }))
  const childEnv = { ...environmentWithoutProviderSecrets(process.env), HOME: home, USERPROFILE: home,
    OPENSQUILLA_TEST_FIXTURE_CONFIG: configPath,
    ELECTRON_DISABLE_SECURITY_WARNINGS: 'true', NO_PROXY: '127.0.0.1,localhost' }
  if (installEnabled) delete childEnv.OPENSQUILLA_DESKTOP_ENABLE_WIN_INSTALL
  else childEnv.OPENSQUILLA_DESKTOP_ENABLE_WIN_INSTALL = '0'
  runningApp = await electron.launch({
    ...(executablePath ? { executablePath: resolve(executablePath) } : {}),
    args: [`--user-data-dir=${userData}`, fixtureRoot],
    env: childEnv,
    timeout: 30_000,
  })
  const app = runningApp
  const page = await app.firstWindow()
  await waitFor(() => app.evaluate(() => Boolean(globalThis.windowsUpdateFixture)), 'real Electron fixture readiness', 30_000)
  const snapshot = () => app.evaluate(() => globalThis.windowsUpdateFixture.snapshot())
  const initial = await snapshot()
  assert.equal(await realpath(initial.userData), await realpath(userData))
  assert.equal(resolve(initial.home), resolve(home))
  assert.equal(initial.state.status, 'downloaded')
  assert.equal(initial.state.canInstall, installEnabled)
  assert.equal(initial.installFlagPresent, !installEnabled)
  assert.equal(initial.gatewayPids.length, 1)
  await page.locator('[data-testid="settings-update-download"]').waitFor({ state: 'visible' })
  await page.locator('[data-testid="settings-update-relaunch"]').waitFor({ state: installEnabled ? 'visible' : 'hidden' })
  return { app, page, snapshot, name, logPath, directory,
    configure: patch => app.evaluate((_electron, patch) => globalThis.windowsUpdateFixture.configure(patch), patch),
    screenshot: label => page.screenshot({ path: join(outputRoot, `${name}-${label}.png`), fullPage: true }),
  }
}

  {
    const f = await launchCase('cache-refresh-explicitly-disabled', { installEnabled: false })
    assert.ok(await loadWindowsUpdateCache(f.directory))
    await f.screenshot('cached-b')
    await f.page.getByRole('button', { name: 'Check', exact: true }).click()
    await waitFor(async () => {
      const current = await f.snapshot()
      return current.state.status === 'available' && current.state.latestVersion === '0.5.6'
    }, 'a newer controlled channel candidate through real Check IPC')
    await f.page.locator('.settings-update__meta').getByText('0.5.6', { exact: false }).waitFor({ state: 'visible' })
    const refreshed = await f.snapshot()
    assert.equal(refreshed.state.canInstall, false)
    assert.equal(refreshed.verifiedInstallerPath, null)
    assert.equal(refreshed.cacheDescriptor, null)
    assert.equal(await loadWindowsUpdateCache(f.directory), null)
    assert.equal(refreshed.counts.channels, 1)
    assert.equal(refreshed.counts.stops, 0)
    assert.equal(refreshed.counts.launches, 0)
    await f.page.locator('[data-testid="settings-update-relaunch"]').waitFor({ state: 'hidden' })
    await f.screenshot('available-c')
    const closed = f.app.waitForEvent('close', { timeout: 15_000 })
    await f.app.evaluate(({ app }) => app.quit())
    await closed
    runningApp = null
    results.push({ case: f.name, passed: true, canInstall: false, from: '0.5.5', to: '0.5.6' })
  }

  {
    const f = await launchCase('signature-and-quit')
    await f.screenshot('downloaded')
    const original = await f.snapshot()
    await f.configure({ signature: 'signature_unavailable', holdVerification: true })
    await f.page.locator('[data-testid="settings-update-relaunch"]').click()
    await waitFor(async () => (await f.snapshot()).verificationHeld, 'signature boundary reached through renderer IPC')
    assert.equal((await f.snapshot()).state.status, 'applying')
    await f.page.locator('[data-testid="settings-update-download"]').waitFor({ state: 'hidden' })
    const duplicateVerification = await f.page.evaluate(async () => await Promise.all([
      window.opensquillaDesktop.relaunchToUpdate(), window.opensquillaDesktop.relaunchToUpdate(),
    ]))
    assert.ok(duplicateVerification.every(state => state.status === 'applying'))
    assert.equal((await f.snapshot()).counts.launches, 0, 'duplicate real renderer IPC cannot launch during verification')
    await f.screenshot('applying')
    await f.app.evaluate(() => globalThis.windowsUpdateFixture.releaseVerification())
    await waitFor(async () => (await f.snapshot()).state.errorCode === 'signature_unavailable', 'signature failure rendered after IPC')
    await f.page.getByText(/could not verify|unable to verify|cannot verify/i).first().waitFor({ state: 'visible' })
    const failed = await f.snapshot()
    assert.deepEqual(failed.gatewayPids, original.gatewayPids)
    assert.equal(failed.counts.stops, 0)
    assert.equal(failed.counts.launches, 0)
    await waitFor(
      () => f.page.locator('[data-testid="settings-update-relaunch"]').isEnabled(),
      'retry action re-enabled after the renderer IPC settles',
    )
    await f.screenshot('signature-unavailable')
    await f.configure({ holdVerification: true })
    await f.page.locator('[data-testid="settings-update-relaunch"]').click()
    await waitFor(async () => (await f.snapshot()).verificationHeld, 'retry verification before Quit')
    await f.app.evaluate(({ app }) => { globalThis.windowsUpdateFixture.closeWindow(); app.quit() })
    const deferred = await f.snapshot()
    assert.equal(deferred.visible, false)
    assert.equal(deferred.destroyed, false)
    assert.equal(deferred.quitRequestedDuringUpdateDrain, true)
    const closed = f.app.waitForEvent('close', { timeout: 15_000 })
    await f.app.evaluate(() => globalThis.windowsUpdateFixture.releaseVerification())
    await closed
    runningApp = null
    const logs = (await readFile(f.logPath, 'utf8')).trim().split('\n').map(JSON.parse)
    assert.ok(logs.some(row => row.event === 'before_quit_observed' && row.prevented))
    assert.equal(logs.filter(row => row.event === 'gateway_spawn').length, 1, 'remembered Quit must not recover a Gateway')
    assert.equal(logs.filter(row => row.event === 'gateway_exit').length, 1)
    results.push({ case: f.name, passed: true, runtime: original.runtime, originalGatewayPid: original.gatewayPids[0] })
  }
  {
    const f = await launchCase('spawn-and-quit')
    await f.configure({ signature: 'ok', launch: 'missing' })
    await f.page.locator('[data-testid="settings-update-relaunch"]').click()
    await waitFor(async () => {
      const state = await f.snapshot()
      return state.state.errorCode === 'install_failed' && state.gatewayPids.length === 1 && state.counts.resumes === 1
    }, 'real asynchronous spawn failure and Gateway recovery')
    const failed = await f.snapshot()
    assert.equal(failed.state.status, 'downloaded')
    assert.equal(failed.counts.launches, 1)
    await f.screenshot('spawn-failed')
    await f.configure({ launch: 'node', holdDrain: true })
    await f.page.locator('[data-testid="settings-update-relaunch"]').click()
    await waitFor(async () => (await f.snapshot()).drainHeld, 'real owned Gateway held during drain')
    const duplicateDrain = await f.page.evaluate(async () => await Promise.all([
      window.opensquillaDesktop.relaunchToUpdate(), window.opensquillaDesktop.relaunchToUpdate(),
    ]))
    assert.ok(duplicateDrain.every(state => state.status === 'applying'))
    assert.equal((await f.snapshot()).counts.stops, 2, 'duplicate real renderer IPC cannot begin another drain')
    await f.app.evaluate(({ app }) => { globalThis.windowsUpdateFixture.closeWindow(); app.quit(); app.quit() })
    const deferred = await f.snapshot()
    assert.equal(deferred.visible, false)
    assert.equal(deferred.destroyed, false)
    assert.equal(deferred.gatewayPids.length, 1)
    assert.equal(deferred.counts.launches, 1)
    assert.equal(deferred.updateInstallHandoffReady, false)
    const closed = f.app.waitForEvent('close', { timeout: 15_000 })
    await f.app.evaluate(() => globalThis.windowsUpdateFixture.releaseDrain())
    await closed
    runningApp = null
    const logs = (await readFile(f.logPath, 'utf8')).trim().split('\n').map(JSON.parse)
    assert.equal(logs.filter(row => row.event === 'synthetic_spawn_succeeded').length, 1)
    assert.ok(logs.some(row => row.event === 'before_quit_observed' && !row.prevented && row.handoffReady))
    assert.equal(logs.filter(row => row.event === 'gateway_spawn').length, 2)
    assert.equal(logs.filter(row => row.event === 'gateway_exit').length, 2)
    results.push({ case: f.name, passed: true })
  }
  const report = {
    ok: true, status: 'passed', runId, sourceSha256: createHash('sha256').update(source).digest('hex'),
    sourceFiles: Object.fromEntries(await Promise.all([
      'desktop/electron/src/main.ts', 'desktop/electron/src/preload.cts',
      'desktop/electron/src/update-channel.ts', 'desktop/electron/src/update-check-scheduler.ts',
      'desktop/electron/src/windows-update-cache.ts', 'desktop/electron/src/windows-update-coordinator.ts',
      'desktop/electron/src/windows-update-handoff.ts', 'desktop/electron/src/desktop-window-lifecycle.ts',
      'opensquilla-webui/src/components/DesktopUpdateIndicator.vue',
      'opensquilla-webui/src/components/settings/SettingsUpdatePanel.vue',
      'opensquilla-webui/src/composables/useDesktopUpdate.ts',
      'opensquilla-webui/src/composables/useDesktopUpdatePresentation.ts',
    ].map(async name => [name, createHash('sha256').update(await readFile(resolve(packageRoot, '../..', name))).digest('hex')]))),
    evidence: 'Real Electron IPC, production preload, production Vue update components, AST-extracted production lifecycle functions',
    limits: 'Signature, registry, channel and asset-probe are test seams; Node fake Gateway and node.exe --updated only; no NSIS, signed-upgrade or full packaged-main claim.',
    results,
  }
  await writeFile(reportPath, `${JSON.stringify(report, null, 2)}\n`)
  console.log(JSON.stringify({ ...report, outputRoot }, null, 2))
} catch (error) {
  await writeFile(reportPath, `${JSON.stringify({ ok: false, status: 'failed', runId, error: String(error), results }, null, 2)}\n`).catch(() => {})
  throw error
} finally {
  if (runningApp) await closeElectronWithDeadline({ app: runningApp, phase: 'Windows update fixture cleanup', timeoutMs: 10_000 }).catch(() => {})
  const cleanup = resolve(isolationRoot)
  const fromTemp = relative(resolve(tmpdir()), cleanup)
  assert.ok(fromTemp && !fromTemp.startsWith('..') && !fromTemp.includes(':'))
  await rm(cleanup, { recursive: true, force: true }).catch(() => {})
}
