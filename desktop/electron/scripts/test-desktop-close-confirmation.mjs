import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
// TypeScript 7's package root exposes the new API only; the compatibility
// parser is installed as the TypeScript 6 alias and provides the AST helpers.
import ts from '@typescript/typescript6'

import { DesktopQuitConfirmation } from '../dist/desktop-quit.js'
import { DesktopWriterAdmission } from '../dist/desktop-writer-admission.js'
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

function createScenario({ platform = 'darwin', behavior = 'background', activity = 'idle', pages = 0 } = {}) {
  const events = []
  const logs = []
  const saves = []
  const activityResult = deferred()
  const dialogResult = deferred()
  const quitDialogResult = deferred()
  const saveResult = deferred()
  let remainingPages = pages
  let pageCloseRequests = 0
  let quitDialogOptions
  let activityChecks = 0
  let hidden = 0
  let quit = 0
  let shownQuitDialog = 0
  let shownCloseDialog = 0
  let preventions = 0
  let windowDestroyed = false
  let activityState = activity
  let closeBehavior = behavior
  let savePending = false
  const window = {
    isDestroyed: () => windowDestroyed,
    isMinimized: () => false,
    restore: () => events.push('restore'),
    show: () => events.push('show'),
    focus: () => events.push('focus'),
  }
  const closeEvent = () => ({ preventDefault: () => { preventions += 1 } })
  const child = { pid: 54321 }
  const env = {
    appExitPhase: 'running',
    isQuitting: false,
    desktopWriters: new DesktopWriterAdmission(),
    quitWriterAdmission: null,
    setAppExitPhase: phase => { env.appExitPhase = phase },
    waitForDesktopWriterOperations: () => env.desktopWriters.waitForAtMost(0),
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
    gatewayState: { status: 'ready' },
    window,
    currentMainWindow: () => window,
    currentOnboardingWindow: () => null,
    focusOnboardingWindow: () => events.push('focus-onboarding'),
    hideMainWindow: () => { hidden += 1; events.push('hidden') },
    loadDesktopPreferencesRecord: () => ({ value: { main_window_close_behavior: closeBehavior } }),
    mainWindowCloseAction,
    desktopT: key => key,
    dialog: {
      showMessageBox: async () => {
        shownCloseDialog += 1
        return await dialogResult.promise
      },
    },
    saveDesktopPreferences: async request => {
      saves.push(request.mainWindowCloseBehavior)
      savePending = true
      await saveResult.promise
      savePending = false
      closeBehavior = request.mainWindowCloseBehavior
      return {}
    },
    app: { quit: () => { quit += 1; events.push('quit') } },
    desktopLog: (event, detail) => { events.push(event); logs.push({ event, ...detail }) },
    liveLifecycleOwnedGatewayProcesses: () => [child],
    gatewayProcessCleanupContexts: new Map(),
    gatewayProcessCleanupOperations: new WeakMap(),
    hasGatewayProcessExited: () => false,
    ownedGatewayRecord: () => ({ instance_nonce: 'synthetic' }),
    readVerifiedDesktopGatewayActivity: async () => {
      activityChecks += 1
      if (activityState === 'pending') return await activityResult.promise
      return activityState === 'unknown'
        ? { kind: 'unreachable' }
        : { kind: 'ok', activeCount: activityState === 'active' ? 1 : 0 }
    },
    showDesktopQuitConfirmation: async options => {
      quitDialogOptions = options
      shownQuitDialog += 1
      const answer = await quitDialogResult.promise
      return typeof answer === 'boolean'
        ? { response: answer ? 'confirm' : 'cancel', remember: false } : answer
    },
    nativeWorkbenchSurfaces: {
      hasBrowserTabs: () => remainingPages > 0,
      closeBrowserTabs: async ({ confirm }) => {
        pageCloseRequests += 1
        if (!await confirm({ tabCount: remainingPages })) return false
        remainingPages = 0
        return true
      },
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
    get remainingPages() { return remainingPages },
    get pageCloseRequests() { return pageCloseRequests },
    get quitDialogOptions() { return quitDialogOptions },
    get activityChecks() { return activityChecks },
    get hidden() { return hidden },
    get quit() { return quit },
    get shownQuitDialog() { return shownQuitDialog },
    get shownCloseDialog() { return shownCloseDialog },
    get preventions() { return preventions },
    get savePending() { return savePending },
    events,
    logs,
    saves,
  }
}

async function settleConfirmation(scenario, answer) {
  scenario.quitDialogResult.resolve(answer)
  await until(() => scenario.env.quitConfirmationPromise === null)
  await flush()
}

// Background closure hides immediately, including active/unknown work and open
// browser pages. It never needs consent to stop or discard either resource.
for (const platform of ['darwin', 'win32']) {
  for (const activity of ['idle', 'active', 'unknown', 'pending']) {
    const h = createScenario({ platform, activity, pages: 2 })
    h.handlers.handleMainWindowClose(h.window, h.closeEvent())
    assert.equal(h.hidden, 1)
    assert.equal(h.activityChecks, 0)
    assert.equal(h.pageCloseRequests, 0)
    assert.equal(h.shownQuitDialog, 0)
    assert.equal(h.remainingPages, 2)
    assert.equal(h.quit, 0)
    assert.equal(h.env.desktopWriters.closed, false)
  }
}

// Ask uses one close-choice dialog for every task state. Its Quit choice is
// already the task/page consent; Background and Cancel keep all pages intact.
for (const owner of ['update', 'confirmed-quit']) {
  const h = createScenario({ platform: 'win32', activity: 'active', pages: 2 })
  if (owner === 'update') {
    h.env.updateApplying = true
    h.env.appExitPhase = 'deferred'
  } else {
    h.env.quitConfirmed = true
    h.env.appExitPhase = 'draining'
  }
  h.handlers.handleMainWindowClose(h.window, h.closeEvent())
  assert.equal(h.hidden, 0, `${owner}: preserve the protected operation's visible progress`)
  assert.equal(h.pageCloseRequests, 0)
  assert.equal(h.activityChecks, 0)
  assert.equal(h.shownQuitDialog, 0)
}

for (const activity of ['idle', 'active', 'unknown']) {
  for (const response of ['cancel', 'background', 'confirm']) {
    const h = createScenario({ behavior: 'ask', activity, pages: 2 })
    h.handlers.handleMainWindowClose(h.window, h.closeEvent())
    await until(() => h.shownQuitDialog === 1)
    assert.equal(h.quitDialogOptions.background, 'closePrompt.background')
    assert.equal(h.quitDialogOptions.keepRunning, 'closePrompt.cancel')
    assert.equal(h.quitDialogOptions.remember, 'closePrompt.remember')
    assert.ok(h.quitDialogOptions.detail.includes('quit.pagesDetail'))
    assert.equal(h.quitDialogOptions.message,
      activity === 'idle' ? 'closePrompt.message' : `closePrompt.${activity}`)
    assert.equal(h.remainingPages, 2)
    assert.equal(h.env.desktopWriters.closed, false)
    h.handlers.handleMainWindowClose(h.window, h.closeEvent())
    await flush()
    assert.equal(h.shownQuitDialog, 1, 'Repeated X shares the pending decision')
    await settleConfirmation(h, { response, remember: false })
    assert.equal(h.shownQuitDialog, 1, 'Ask Quit must not prompt for task interruption again')
    assert.equal(h.shownCloseDialog, 0, 'The old native close-preference dialog is not used')
    assert.equal(h.quit, response === 'confirm' ? 1 : 0)
    assert.equal(h.remainingPages, response === 'confirm' ? 0 : 2)
    assert.equal(h.hidden, response === 'background' ? 1 : 0)
    assert.deepEqual(h.saves, [])
    assert.equal(h.env.desktopWriters.closed, response === 'confirm')
  }
}

// Ask also offers all choices with no owned Gateway and no open pages.
{
  const h = createScenario({ behavior: 'ask' })
  h.env.liveLifecycleOwnedGatewayProcesses = () => []
  h.handlers.handleMainWindowClose(h.window, h.closeEvent())
  await until(() => h.shownQuitDialog === 1)
  await settleConfirmation(h, { response: 'cancel', remember: true })
  assert.deepEqual(h.saves, [], 'Cancel never changes the close preference')
  assert.equal(h.quit, 0)
}

// Remembering Background or Quit completes its protected write before the
// window hides, pages close, or quit admission is fenced.
for (const response of ['background', 'confirm']) {
  const h = createScenario({ behavior: 'ask', activity: 'active', pages: 2 })
  h.handlers.handleMainWindowClose(h.window, h.closeEvent())
  await until(() => h.shownQuitDialog === 1)
  h.quitDialogResult.resolve({ response, remember: true })
  await until(() => h.savePending)
  assert.equal(h.remainingPages, 2)
  assert.equal(h.hidden, 0)
  assert.equal(h.quit, 0)
  assert.equal(h.env.desktopWriters.closed, false)
  h.saveResult.resolve()
  await until(() => h.env.quitConfirmationPromise === null)
  assert.deepEqual(h.saves, [response === 'background' ? 'background' : 'quit'])
  assert.equal(h.hidden, response === 'background' ? 1 : 0)
  assert.equal(h.quit, response === 'confirm' ? 1 : 0)
  assert.equal(h.shownQuitDialog, 1)
  if (response === 'background') {
    h.handlers.handleMainWindowClose(h.window, h.closeEvent())
    assert.equal(h.hidden, 2, 'A remembered Background choice hides the next X without another dialog')
    assert.equal(h.shownQuitDialog, 1)
  }
}

// Configured Quit routes X through the same explicit app.quit entry point.
{
  const h = createScenario({ platform: 'win32', behavior: 'quit', activity: 'active', pages: 2 })
  h.handlers.handleMainWindowClose(h.window, h.closeEvent())
  assert.equal(h.quit, 1)
  assert.equal(h.hidden, 0)
  assert.equal(h.pageCloseRequests, 0)
  // before-quit then obtains the one risk decision before changing resources.
  h.handlers.holdQuitForTaskConfirmation(h.closeEvent())
  await until(() => h.shownQuitDialog === 1)
  assert.equal(h.quitDialogOptions.background, undefined)
  assert.equal(h.quitDialogOptions.message, 'quit.active')
  assert.ok(h.quitDialogOptions.detail.includes('quit.pagesDetail'))
  await settleConfirmation(h, false)
  assert.equal(h.remainingPages, 2)
  assert.equal(h.quit, 1)
}

// Diagnostics distinguish launch mismatches and unavailable activity without
// retaining private launch values. A dead scope cannot prove its tools idle.
for (const mode of ['active', 'unknown', 'unmatched', 'retired']) {
  const h = createScenario({ activity: mode === 'active' ? 'active' : 'idle' })
  const secret = 'synthetic-private-value'
  h.env.ownedGatewayRecord = () => mode === 'unmatched' ? null : { instance_nonce: secret }
  if (mode === 'unknown') h.setActivity('unknown')
  if (mode === 'retired') {
    const retired = { pid: 54322 }
    h.env.gatewayProcessCleanupContexts.set(retired, { instanceNonce: secret })
    h.env.hasGatewayProcessExited = child => child === retired
  }
  h.handlers.holdQuitForTaskConfirmation(h.closeEvent())
  await until(() => h.shownQuitDialog === 1)
  assert.ok(!JSON.stringify(h.logs).includes(secret))
  assert.equal(h.logs.find(entry => entry.event === 'quit_confirmation_required').activity,
    mode === 'active' ? 'active' : 'unknown')
  await settleConfirmation(h, false)
  assert.equal(h.quit, 0)
}

// Pages require consent even without an owned Gateway. Signals preserve their
// legacy task policy while still warning before discarding browser pages.
for (const signal of [false, true]) {
  const h = createScenario({ pages: 2 })
  if (!signal) h.env.liveLifecycleOwnedGatewayProcesses = () => []
  h.env.quitFromSignal = signal
  h.handlers.holdQuitForTaskConfirmation(h.closeEvent())
  await until(() => h.shownQuitDialog === 1)
  assert.equal(h.activityChecks, 0)
  await settleConfirmation(h, false)
  assert.equal(h.remainingPages, 2)
  assert.equal(h.quit, 0)
  assert.equal(h.env.quitFromSignal, false)
}

// A later explicit Quit queued during a remembered Background choice needs a
// fresh activity check; the stale Background decision must not hide the parent.
{
  const h = createScenario({ behavior: 'ask', activity: 'idle', pages: 2 })
  let quitRequests = 0, shutdowns = 0, nextPrompts = 0
  const nextAnswer = deferred()
  h.env.app.quit = () => {
    quitRequests++
    if (!h.handlers.holdQuitForTaskConfirmation(h.closeEvent())) shutdowns++
  }
  h.handlers.handleMainWindowClose(h.window, h.closeEvent())
  await until(() => h.shownQuitDialog === 1)
  h.quitDialogResult.resolve({ response: 'background', remember: true })
  await until(() => h.savePending)
  h.env.showDesktopQuitConfirmation = () => { nextPrompts++; return nextAnswer.promise }
  h.env.app.quit()
  h.setActivity('active')
  h.saveResult.resolve()
  await until(() => nextPrompts === 1)
  assert.equal(h.activityChecks, 2)
  assert.equal(quitRequests, 2)
  assert.equal(h.hidden, 0)
  assert.equal(shutdowns, 0)
  nextAnswer.resolve({ response: 'cancel', remember: false })
  await until(() => h.env.quitConfirmationPromise === null)
  assert.equal(h.remainingPages, 2)
  assert.equal(shutdowns, 0)
}

// System/update ownership supersedes a pending dialog or preference write.
for (const owner of ['system', 'update', 'destroyed']) {
  for (const phase of ['dialog', 'remember']) {
    const h = createScenario({ behavior: 'ask', activity: 'active', pages: 2 })
    h.handlers.handleMainWindowClose(h.window, h.closeEvent())
    await until(() => h.shownQuitDialog === 1)
    if (phase === 'remember') {
      h.quitDialogResult.resolve({ response: 'background', remember: true })
      await until(() => h.savePending)
    }
    if (owner === 'system') h.env.systemSessionEnding = true
    else if (owner === 'update') h.env.updateApplying = true
    else h.window.isDestroyed = () => true
    if (phase === 'dialog') h.quitDialogResult.resolve({ response: 'confirm', remember: false })
    else h.saveResult.resolve()
    await until(() => h.env.quitConfirmationPromise === null)
    assert.equal(h.hidden, 0)
    assert.equal(h.quit, 0)
    assert.equal(h.remainingPages, 2)
  }
}

// Unsupported background platforms retain their quit policy; OS session ending
// permits native closure without another desktop decision.
{
  const linux = createScenario({ platform: 'linux', activity: 'active' })
  linux.handlers.handleMainWindowClose(linux.window, linux.closeEvent())
  assert.equal(linux.quit, 1)
  assert.equal(linux.hidden, 0)
  const ending = createScenario({ behavior: 'ask', activity: 'active' })
  ending.env.systemSessionEnding = true
  ending.handlers.handleMainWindowClose(ending.window, ending.closeEvent())
  assert.equal(ending.preventions, 0)
  assert.equal(ending.shownQuitDialog, 0)
}

console.log('Desktop close confirmation checks passed: background hides; Ask and Quit share one task/page decision.')
