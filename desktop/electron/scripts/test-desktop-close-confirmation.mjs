import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
// TypeScript 7's package root exposes the new API only; the compatibility
// parser is installed as the TypeScript 6 alias and provides the AST helpers.
import ts from '@typescript/typescript6'

import { DesktopQuitConfirmation } from '../dist/desktop-quit.js'
import { mainWindowCloseAction } from '../dist/desktop-window-lifecycle.js'

// Extract the production handlers from the compiled main process. This keeps
// the test tied to the event ordering shipped in the desktop bundle without
// booting Electron, a Gateway, or a real profile.
const source = ts.createSourceFile(
  'main.js',
  readFileSync(new URL('../dist/main.js', import.meta.url), 'utf8'),
  ts.ScriptTarget.Latest,
  true,
  ts.ScriptKind.JS,
)
const functions = new Map()
function collect(node) {
  if (ts.isFunctionDeclaration(node) && node.name
    && ['promptForMainWindowClose', 'handleMainWindowClose', 'holdQuitForTaskConfirmation'].includes(node.name.text)) {
    functions.set(node.name.text, node.getText(source))
  }
  ts.forEachChild(node, collect)
}
collect(source)
for (const name of ['promptForMainWindowClose', 'handleMainWindowClose', 'holdQuitForTaskConfirmation']) {
  assert.ok(functions.has(name), `Expected production ${name} handler.`)
}

const makeHandlers = () => new Function('env', `with (env) {
  ${functions.get('promptForMainWindowClose')}
  ${functions.get('handleMainWindowClose')}
  ${functions.get('holdQuitForTaskConfirmation')}
  return { promptForMainWindowClose, handleMainWindowClose, holdQuitForTaskConfirmation }
}`)

function deferred() {
  let resolve
  const promise = new Promise(accept => { resolve = accept })
  return { promise, resolve }
}

const flush = () => new Promise(resolve => setImmediate(resolve))
async function until(predicate, message = 'condition did not settle') {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (predicate()) return
    await flush()
  }
  assert.fail(message)
}

function createScenario({ platform = 'darwin', behavior = 'background', activity = 'idle' } = {}) {
  const events = []
  const activityResult = deferred()
  const dialogResult = deferred()
  const quitDialogResult = deferred()
  const saveResult = deferred()
  let activityChecks = 0
  let hidden = 0
  let quit = 0
  let shownQuitDialog = 0
  let shownCloseDialog = 0
  let preventions = 0
  let windowDestroyed = false
  let activityState = activity
  let savePending = false
  const window = {
    isDestroyed: () => windowDestroyed,
    isMinimized: () => false,
    restore: () => events.push('restore'),
    show: () => events.push('show'),
    focus: () => events.push('focus'),
  }
  const closeEvent = () => ({ preventDefault: () => { preventions += 1 } })
  const child = {}
  const env = {
    appExitPhase: 'running',
    systemSessionEnding: false,
    updateApplying: false,
    quitConfirmed: false,
    quitFromSignal: false,
    quitConfirmationPromise: null,
    quitConfirmationWantsExit: false,
    mainWindowClosePrompt: null,
    windowsTray: platform === 'win32' ? {} : null,
    mainWindow: window,
    onboardingWindow: null,
    process: { platform },
    window,
    currentMainWindow: () => window,
    currentOnboardingWindow: () => null,
    focusOnboardingWindow: () => events.push('focus-onboarding'),
    hideMainWindow: () => { hidden += 1; events.push('hidden') },
    loadDesktopPreferencesRecord: () => ({ value: { main_window_close_behavior: behavior } }),
    mainWindowCloseAction,
    desktopT: key => key,
    dialog: {
      showMessageBox: async () => {
        shownCloseDialog += 1
        return await dialogResult.promise
      },
    },
    saveDesktopPreferences: async () => {
      savePending = true
      await saveResult.promise
      savePending = false
      return {}
    },
    app: { quit: () => { quit += 1; events.push('quit') } },
    desktopLog: (event) => events.push(event),
    liveLifecycleOwnedGatewayProcesses: () => [child],
    hasGatewayProcessExited: () => false,
    ownedGatewayRecord: () => ({ instance_nonce: 'synthetic' }),
    readVerifiedDesktopGatewayActivity: async () => {
      activityChecks += 1
      if (activityState === 'pending') return await activityResult.promise
      return activityState === 'unknown'
        ? { kind: 'unreachable' }
        : { kind: 'ok', activeCount: activityState === 'active' ? 1 : 0 }
    },
    showDesktopQuitConfirmation: async () => {
      shownQuitDialog += 1
      return await quitDialogResult.promise
    },
    desktopQuitConfirmation: new DesktopQuitConfirmation(),
  }
  const handlers = makeHandlers()(env)
  return {
    env,
    handlers,
    window,
    closeEvent,
    activityResult,
    dialogResult,
    quitDialogResult,
    saveResult,
    setActivity: value => { activityState = value },
    get activityChecks() { return activityChecks },
    get hidden() { return hidden },
    get quit() { return quit },
    get shownQuitDialog() { return shownQuitDialog },
    get shownCloseDialog() { return shownCloseDialog },
    get preventions() { return preventions },
    get savePending() { return savePending },
    events,
  }
}

async function settleConfirmation(scenario, answer) {
  scenario.quitDialogResult.resolve(answer)
  await until(() => scenario.env.quitConfirmationPromise === null)
  await flush()
}

// Active work keeps the parent visible until the interruption prompt resolves.
for (const platform of ['darwin', 'win32']) {
  const scenario = createScenario({ platform, activity: 'pending' })
  const event = scenario.closeEvent()
  scenario.handlers.handleMainWindowClose(scenario.window, event)
  assert.equal(scenario.hidden, 0, `${platform}: close must not hide while checking activity`)
  assert.ok(scenario.preventions >= 1)
  scenario.activityResult.resolve({ kind: 'ok', activeCount: 1 })
  await until(() => scenario.shownQuitDialog === 1)
  assert.equal(scenario.hidden, 0, `${platform}: active task prompt must keep window visible`)
  scenario.handlers.handleMainWindowClose(scenario.window, scenario.closeEvent())
  await flush()
  assert.equal(scenario.hidden, 0, `${platform}: repeated close must not hide an open prompt`)
  assert.equal(scenario.shownQuitDialog, 1)
  assert.equal(scenario.activityChecks, 1)
  await settleConfirmation(scenario, false)
  assert.equal(scenario.hidden, 0, `${platform}: cancelling interruption must keep window visible`)
  assert.equal(scenario.quit, 0)
}

// Acceptance commits the same normal app.quit path exactly once, including
// repeated native closes and a tray/menu Quit arriving during the check.
for (const activity of ['active', 'unknown']) {
  const scenario = createScenario({ activity: 'pending' })
  scenario.handlers.handleMainWindowClose(scenario.window, scenario.closeEvent())
  scenario.handlers.handleMainWindowClose(scenario.window, scenario.closeEvent())
  const trayEvent = scenario.closeEvent()
  scenario.handlers.holdQuitForTaskConfirmation(trayEvent)
  scenario.setActivity(activity)
  scenario.activityResult.resolve(activity === 'active'
    ? { kind: 'ok', activeCount: 1 } : { kind: 'unreachable' })
  await until(() => scenario.shownQuitDialog === 1)
  assert.equal(scenario.activityChecks, 1, `${activity}: activity check should be single-flight`)
  await settleConfirmation(scenario, true)
  assert.equal(scenario.quit, 1, `${activity}: accepted prompt should quit once`)
  assert.equal(scenario.hidden, 0)
}

// An idle runtime preserves the configured close behavior.
{
  const background = createScenario({ behavior: 'background', activity: 'idle' })
  background.handlers.handleMainWindowClose(background.window, background.closeEvent())
  await until(() => background.hidden === 1)
  assert.equal(background.shownQuitDialog, 0)
  assert.equal(background.quit, 0)

  const ask = createScenario({ behavior: 'ask', activity: 'idle' })
  ask.handlers.handleMainWindowClose(ask.window, ask.closeEvent())
  await until(() => ask.shownCloseDialog === 1)
  assert.equal(ask.hidden, 0)
  ask.dialogResult.resolve({ response: 2, checkboxChecked: false })
  await until(() => ask.env.quitConfirmationPromise === null)
  assert.equal(ask.hidden, 0)
  assert.equal(ask.quit, 0)
}

// A tray Quit arriving while a native close is checking idle work escalates to
// quit and must not open the close-preference dialog or hide the parent first.
{
  const scenario = createScenario({ behavior: 'ask', activity: 'pending' })
  scenario.handlers.handleMainWindowClose(scenario.window, scenario.closeEvent())
  scenario.handlers.holdQuitForTaskConfirmation(scenario.closeEvent())
  scenario.activityResult.resolve({ kind: 'ok', activeCount: 0 })
  await until(() => scenario.quit === 1)
  assert.equal(scenario.shownCloseDialog, 0)
  assert.equal(scenario.hidden, 0)
}

// A preference write that is pending when tray Quit arrives cannot apply a
// stale hide/quit decision. The quit path is retried after the write settles.
{
  const scenario = createScenario({ behavior: 'ask', activity: 'pending' })
  let quitRequests = 0
  let shutdowns = 0
  scenario.env.app.quit = () => {
    quitRequests += 1
    if (!scenario.handlers.holdQuitForTaskConfirmation(scenario.closeEvent())) shutdowns += 1
  }
  scenario.handlers.handleMainWindowClose(scenario.window, scenario.closeEvent())
  scenario.activityResult.resolve({ kind: 'ok', activeCount: 0 })
  await until(() => scenario.shownCloseDialog === 1)
  scenario.dialogResult.resolve({ response: 0, checkboxChecked: true })
  await until(() => scenario.savePending)
  scenario.env.app.quit()
  scenario.setActivity('active')
  scenario.saveResult.resolve()
  await until(() => scenario.shownQuitDialog === 1)
  assert.equal(scenario.activityChecks, 2, 'queued Quit must recheck after a preference write')
  assert.equal(quitRequests, 2, 'the queued Quit should resume once after the preference write')
  assert.equal(shutdowns, 0, 'fresh active work must await interruption confirmation')
  assert.ok(scenario.events.includes('show'))
  assert.equal(scenario.hidden, 0)
  await settleConfirmation(scenario, false)
  assert.equal(shutdowns, 0, 'cancelling the fresh interruption prompt must prevent shutdown')
  assert.equal(scenario.env.quitConfirmed, false)
  assert.equal(scenario.hidden, 0)
}

// Unsupported platforms still take the direct quit path; OS session ending
// bypasses policy and allows native closure.
{
  const linux = createScenario({ platform: 'linux', behavior: 'background', activity: 'active' })
  linux.handlers.handleMainWindowClose(linux.window, linux.closeEvent())
  assert.equal(linux.quit, 1)
  assert.equal(linux.preventions, 1)

  const ending = createScenario({ platform: 'darwin', behavior: 'ask', activity: 'active' })
  ending.env.systemSessionEnding = true
  const event = ending.closeEvent()
  ending.handlers.handleMainWindowClose(ending.window, event)
  assert.equal(ending.preventions, 0)
  assert.equal(ending.hidden, 0)
  assert.equal(ending.quit, 0)
}

console.log('desktop close confirmation checks passed')
