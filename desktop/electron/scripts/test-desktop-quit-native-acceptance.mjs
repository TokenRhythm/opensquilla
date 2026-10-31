import assert from 'node:assert/strict'
import { mkdir, mkdtemp, readFile, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { _electron as electron } from 'playwright'
import { completeClosingDialogClick } from './fixtures/desktop-quit-acceptance/dialog-click.mjs'

const manual = process.argv.includes('--manual')
const themesOnly = process.argv.includes('--themes-only')
const exitOnly = process.argv.includes('--exit-only')
const locales = ['en', 'zh', 'ja', 'fr', 'de', 'es']
const supportsBackgroundClose = process.platform === 'win32' || process.platform === 'darwin'
const notApplicable = supportsBackgroundClose ? [] : [{
  feature: 'X background, three-choice Ask and remembered close preference',
  reason: 'The production Linux mainWindowCloseAction routes X to Quit; background/tray close preferences are unavailable.',
}]
const artifactArg = process.argv.find(value => value.startsWith('--artifacts='))
const output = resolve(artifactArg ? artifactArg.slice('--artifacts='.length)
  : process.env.CI_REPORT_DIR ? join(process.env.CI_REPORT_DIR, 'desktop-quit-native-acceptance')
    : await mkdtemp(join(tmpdir(), 'opensquilla-pr1793-native-')))
await mkdir(output, { recursive: true })
const profile = await mkdtemp(join(output, 'profile-'))
const results = []
let nativeApp
const pause = ms => new Promise(resolve => setTimeout(resolve, ms))
async function themePalette(theme) {
  const css = await readFile(new URL(`../../../opensquilla-webui/src/themes/${theme}/tokens.css`, import.meta.url), 'utf8')
  return Object.fromEntries(['--bg-surface', '--bg-hover', '--text', '--text-muted', '--border',
    '--accent', '--accent-hover', '--accent-foreground'].map(token => [token,
    css.match(new RegExp(`${token}:\\s*(#[a-f0-9]{6});`, 'i'))[1]]))
}
async function assertDialogPalette(prompt, palette) {
  const rgb = token => `rgb(${palette[token].slice(1).match(/../g).map(hex => parseInt(hex, 16)).join(', ')})`
  const color = (selector, property) => prompt.locator(selector).evaluate((node, name) => getComputedStyle(node)[name], property)
  await prompt.mouse.move(1, 1)
  assert.equal(await color('.card', 'backgroundColor'), rgb('--bg-surface'))
  assert.equal(await color('.card', 'borderTopColor'), rgb('--border'))
  assert.equal(await color('#title', 'color'), rgb('--text'))
  assert.equal(await color('#message', 'color'), rgb('--text-muted'))
  const primary = supportsBackgroundClose ? '#background' : '#confirm'
  assert.equal(await color(primary, 'backgroundColor'), rgb('--accent'))
  assert.equal(await color(primary, 'color'), rgb('--accent-foreground'))
  assert.equal(await color(':focus', 'outlineColor'), rgb('--accent'))
  await prompt.locator(primary).hover()
  assert.equal(await color(primary, 'backgroundColor'), rgb('--accent-hover'))
  await prompt.locator('#keep').hover()
  assert.equal(await color('#keep', 'backgroundColor'), rgb('--bg-hover'))
  await prompt.mouse.move(1, 1)
}
async function pressClosingKey(page, key) {
  try { await page.keyboard.press(key) }
  catch (error) {
    // Electron can destroy the confirmation WebContents before CDP ACKs keyup.
    // The caller still checks the production lifecycle result after it closes.
    if (!page.isClosed()) throw error
  }
}
async function waitFor(check, label, timeout = 10_000) {
  const end = Date.now() + timeout
  while (Date.now() < end) {
    const result = await check()
    if (result) return result
    await pause(50)
  }
  throw new Error(`Timed out waiting for ${label}`)
}
try {
  nativeApp = await electron.launch({
    // Preserve Electron nativeTheme; Playwright otherwise forces light media.
    colorScheme: null,
    ...(process.env.OPENSQUILLA_ACCEPTANCE_ELECTRON ? { executablePath: process.env.OPENSQUILLA_ACCEPTANCE_ELECTRON } : {}),
    args: [fileURLToPath(new URL('./fixtures/desktop-quit-acceptance', import.meta.url)),
      `--acceptance-profile=${profile}`, ...(process.argv.includes('--manual-native-errors') ? ['--manual-native-errors'] : [])],
    env: { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: 'true', NO_PROXY: '127.0.0.1,localhost', no_proxy: '127.0.0.1,localhost' },
  })
  nativeApp.process().stderr?.on('data', chunk => { if (/Error|failed|Exception/i.test(String(chunk))) process.stderr.write(chunk) })
  await waitFor(() => nativeApp.evaluate(() => Boolean(globalThis.__opensquillaQuitAcceptance)), 'native fixture readiness', 20_000)
  const controller = await waitFor(async () => {
    for (const page of nativeApp.windows()) if (await page.title() === 'OpenSquilla PR1793 Acceptance') return page
    return null
  }, 'acceptance controller')
  const nativeProcess = nativeApp.process()
  const isApplicationRunning = () => !controller.isClosed()
    && nativeProcess.exitCode === null && nativeProcess.signalCode === null
  async function clickClosingButton(page, selector, injectLateAck = false) {
    const expectedResponse = { '#keep': 'cancel', '#background': 'background', '#confirm': 'confirm' }[selector]
    assert.ok(expectedResponse, 'the closing button has an explicit production response')
    const dialogWebContentsId = (await nativeDialogState(page)).webContentsId
    const receiptOptions = { dialogWebContentsId, expectedResponse,
      readDecisionReceipts: async () => (await state()).dialogResponseReceipts }
    let injected = false
    await completeClosingDialogClick({
      page, isApplicationRunning, ...receiptOptions,
      click: async () => {
        const click = () => page.locator(selector).click()
        if (!injectLateAck) return await click()
        // A deterministic fixture fault: the real click and production decision
        // finish first, then only its protocol acknowledgement is reported lost.
        await completeClosingDialogClick({ page, click, isApplicationRunning, ...receiptOptions })
        await waitFor(() => page.isClosed(), 'the real click closes its confirmation before ACK fault injection')
        const error = new Error('locator.click: Target page, context or browser has been closed')
        error.log = ['  - performing click action']
        injected = true
        throw error
      },
    })
    if (injectLateAck) assert.equal(injected, true, 'the late click ACK fault was actually injected')
  }
  const state = () => controller.evaluate(() => window.quitAcceptance.snapshot())
  const command = (action, options) => controller.evaluate(([name, payload]) => window.quitAcceptance.command(name, payload), [action, options])
  const nativeDialogState = async page => {
    const handle = await nativeApp.browserWindow(page)
    try {
      const native = await handle.evaluate(window => ({ id: window.id, webContentsId: window.webContents.id,
        visible: window.isVisible(), focused: window.isFocused(),
        modal: window.isModal(), enabled: window.isEnabled(), opacity: window.getOpacity(), bounds: window.getBounds(),
        contentBounds: window.getContentBounds(), parentEnabled: window.getParentWindow()?.isEnabled() ?? null,
        parentBounds: window.getParentWindow()?.getBounds() ?? null }))
      native.workArea = await nativeApp.evaluate(({ screen }, bounds) => screen.getDisplayMatching(bounds).workArea,
        native.parentBounds ?? native.bounds)
      return native
    } finally { await handle.dispose() }
  }
  const dialog = () => waitFor(async () => {
    for (const page of nativeApp.windows()) {
      if (page.isClosed()) continue
      if (await page.locator('#confirm').count()) {
        await page.locator('#confirm').waitFor({ state: 'visible' })
        const { native, events, ready, shown } = await waitFor(async () => {
          const native = await nativeDialogState(page)
          const events = (await state()).nativeWindowEvents.filter(entry => entry.id === native.id)
          const ready = events.findIndex(entry => entry.event === 'ready-to-show')
          const shown = events.findIndex(entry => entry.event === 'show' && entry.visible)
          // AppKit can make the window focused before Electron emits `show`.
          // Wait for both native state and receipts within the same deadline.
          if (!native.visible || !native.focused || ready < 0 || shown <= ready
            || !events.some(entry => entry.event === 'focus' && entry.focused)) return null
          return { native, events, ready, shown }
        }, 'the real native dialog is visible and focused with ready/show/focus receipts')
        assert.equal(native.modal, true)
        assert.equal(native.enabled, true)
        assert.equal(native.parentEnabled, false, 'the native owner is disabled while its modal is open')
        assert.equal(native.opacity, 1)
        assert.ok(native.bounds.width > 0 && native.bounds.height > 0)
        const { bounds, parentBounds, workArea } = native
        const center = { x: bounds.x + bounds.width / 2, y: bounds.y + bounds.height / 2 }
        assert.ok(center.x >= parentBounds.x && center.x <= parentBounds.x + parentBounds.width
          && center.y >= parentBounds.y && center.y <= parentBounds.y + parentBounds.height,
        'the visible native dialog center remains inside its owner')
        assert.ok(bounds.x >= workArea.x && bounds.y >= workArea.y
          && bounds.x + bounds.width <= workArea.x + workArea.width
          && bounds.y + bounds.height <= workArea.y + workArea.height,
        'the complete native dialog remains inside its display work area')
        assert.ok(ready >= 0 && shown > ready, 'production ready-to-show causes an actual native show event')
        assert.ok(events.some(entry => entry.event === 'focus' && entry.focused), 'the native window receives focus')
        return page
      }
    }
    return null
  }, 'production confirmation window')
  async function screenshot(page, name) {
    await page.screenshot({ path: join(output, `${name}.png`) })
    const geometry = await page.evaluate(() => {
      const targets = [...document.querySelectorAll('h1,p,button,label')]
      return { viewport: { width: innerWidth, height: innerHeight }, scroll: { width: document.documentElement.scrollWidth, height: document.documentElement.scrollHeight },
        prefersDark: matchMedia('(prefers-color-scheme: dark)').matches,
        cardBackground: getComputedStyle(document.querySelector('.card')).backgroundColor,
        focus: document.activeElement?.id, controls: targets.map(node => ({ text: node.textContent, rect: node.getBoundingClientRect().toJSON() })) }
    })
    geometry.nativeWindow = await nativeDialogState(page)
    await writeFile(join(output, `${name}.json`), JSON.stringify(geometry, null, 2))
    assert.ok(geometry.scroll.width <= geometry.viewport.width, `${name}: no horizontal overflow`)
    assert.ok(geometry.scroll.height <= geometry.viewport.height + 1, `${name}: no vertical overflow`)
  }
  async function reset(behavior = 'ask') {
    const previousGeneration = (await state()).resetGeneration
    await controller.locator('#reset').click()
    await waitFor(async () => { const value = await state(); return value.resetGeneration > previousGeneration && !value.resetting
      && value.taskAlive && value.ticks > 0 && value.pages === 1 && !value.confirmationPending && value.phase === 'running' }, 'fresh synthetic session')
    await controller.locator('#behavior').selectOption(behavior)
    // CDP can click the controller while macOS has another application active.
    // Establish the same foreground owner as a real user before requesting a
    // modal; never repair focus after the production dialog has opened.
    await nativeApp.evaluate(({ app }) => {
      app.focus({ steal: true })
      globalThis.__opensquillaQuitAcceptance.owner.focus()
    })
    await waitFor(() => nativeApp.evaluate(() => globalThis.__opensquillaQuitAcceptance.owner.isFocused()),
      'the reset owner is foreground before the user action')
  }
  async function canceled(label, click = '#keep', injectLateAck = false) {
    const prompt = await dialog()
    await clickClosingButton(prompt, click, injectLateAck)
    await waitFor(async () => !(await state()).confirmationPending, label)
    const value = await state()
    assert.equal(value.ownerVisible, true, `${label}: the owner remains visible`)
    assert.equal(value.phase, 'running', `${label}: the app remains running`)
    assert.equal(value.quitConfirmed, false, `${label}: quit was not authorized`)
    assert.equal(value.writerClosed, false, `${label}: writer admission remains open`)
    assert.equal(value.ledger.acceptedExit, 0, `${label}: no exit was accepted`)
    assert.equal(value.pages, 1, `${label}: page retained`)
    assert.equal(value.taskAlive, true, `${label}: actual synthetic process retained`)
    assert.equal(value.ledger.drain, 0, `${label}: no cleanup started`)
    const drafts = await nativeApp.evaluate(async () => await Promise.all(
      [...globalThis.__opensquillaQuitAcceptance.manager.surfaces.values()]
        .filter(record => record.kind === 'url-preview' && !record.disposed)
        .map(record => record.contents.executeJavaScript('document.querySelector("textarea").value')),
    ))
    assert.deepEqual(drafts, ['Do not lose this draft — 验收草稿'], `${label}: the actual Chromium document retains its draft`)
    return value
  }
  async function verifyRealExit() {
    // Finish with a real Electron exit, rather than only observing the
    // adapter boundary used to reset the earlier dialog scenarios.
    await command('remove-pages')
    await command('configure', { activity: 'idle', cleanupFailure: true })
    const finalTaskPid = (await state()).taskPid
    assert.ok(Number.isSafeInteger(finalTaskPid) && finalTaskPid > 0)
    await command('exit-on-quit')
    const realExitStarted = performance.now()
    await nativeApp.evaluate(() => {
      setTimeout(() => { void globalThis.__opensquillaQuitAcceptance.command('quit') }, 50)
    })
    await waitFor(() => nativeProcess.exitCode !== null || nativeProcess.signalCode !== null,
      'the actual Electron main process exits after unconfirmed cleanup', 17_000)
    assert.equal(nativeProcess.exitCode, 0)
    const realExitMs = Math.round(performance.now() - realExitStarted)
    await waitFor(() => {
      try { process.kill(finalTaskPid, 0); return false }
      catch (error) { if (error.code === 'ESRCH') return true; throw error }
    }, 'the synthetic child is gone after the real main-process exit')
    const finalEvents = JSON.parse(await readFile(join(profile, 'events.json'), 'utf8'))
    assert.equal(finalEvents.ledger.acceptedExit, 1)
    assert.equal(finalEvents.ledger.errorBoxes.length, 0)
    assert.ok(finalEvents.logs.some(entry => entry.event === 'fixture_exit_committed' && entry.realExit === true))
    assert.ok(finalEvents.logs.some(entry => entry.event === 'quit_cleanup_finished' && entry.cleanupConfirmed === false))
    results.push({ name: 'real-electron-exit-with-unconfirmed-cleanup', passed: true, elapsedMs: realExitMs })
    nativeApp = null
  }
  if (manual) {
    console.log(JSON.stringify({ mode: 'manual', title: 'OpenSquilla PR1793 Acceptance', output, profile,
      controls: 'Use the visible controller; dialogs execute production code. Stop fixture cleans its retained synthetic child.' }))
    await new Promise(resolve => nativeApp.process().once('exit', resolve))
    nativeApp = null
  } else if (exitOnly) {
    await verifyRealExit()
    await writeFile(join(output, 'results.json'), JSON.stringify({ platform: process.platform, results, profile,
      limits: 'Production quit/finalizer with a synthetic child and injected cleanup failure; verifies actual Electron exit, not full Gateway/package or dialog focus.' }, null, 2))
    console.log(JSON.stringify({ passed: results.length, output, profile }))
  } else {
    await reset('background')
    const beforeBackground = await state()
    await controller.locator('#closeMain').click()
    if (supportsBackgroundClose) {
      await pause(550)
      const background = await state()
      assert.equal(background.ownerVisible, false)
      assert.equal(background.dialogCount, 0)
      assert.equal(background.ledger.drain, 0)
      assert.equal(background.pages, 1)
      assert.ok(background.ticks > beforeBackground.ticks, 'the actual synthetic child keeps running in background')
      results.push({ name: 'X-background', passed: true, ticksBefore: beforeBackground.ticks, ticksAfter: background.ticks })
      await controller.locator('#showMain').click()
    } else {
      const prompt = await dialog()
      assert.equal(await prompt.locator('#background').count(), 0, 'Linux X follows its actual Quit policy')
      assert.equal(await prompt.locator('button').count(), 2)
      const value = await canceled('cancel Linux X quit')
      assert.equal(value.ownerVisible, true)
      assert.equal(value.dialogCount, 1)
      results.push({ name: 'linux-X-quit-policy', passed: true })
    }

    for (const locale of locales) for (const theme of ['light', 'dark']) {
      await reset('ask')
      await controller.locator('#locale').selectOption(locale)
      await controller.locator('#theme').selectOption(theme)
      await controller.locator(supportsBackgroundClose ? '#closeMain' : '#quit').click()
      const prompt = await dialog()
      assert.equal(await prompt.locator('html').getAttribute('lang'), locale === 'zh' ? 'zh-Hans' : locale)
      assert.equal(await prompt.locator('#background').count(), supportsBackgroundClose ? 1 : 0)
      assert.equal(await prompt.locator('button').count(), supportsBackgroundClose ? 3 : 2)
      const actualNativeTheme = await nativeApp.evaluate(({ nativeTheme }) => ({
        source: nativeTheme.themeSource, shouldUseDarkColors: nativeTheme.shouldUseDarkColors,
      }))
      assert.equal(actualNativeTheme.source, theme)
      assert.equal(actualNativeTheme.shouldUseDarkColors, theme === 'dark')
      await waitFor(async () => await prompt.evaluate(() => matchMedia('(prefers-color-scheme: dark)').matches) === (theme === 'dark'), 'the actual native theme reaches the confirmation renderer')
      await assertDialogPalette(prompt, await themePalette(theme))
      const variant = supportsBackgroundClose ? 'ask' : 'quit'
      await screenshot(prompt, `${variant}-${locale}-${theme}`)
      await pressClosingKey(prompt, 'Escape')
      await waitFor(async () => !(await state()).confirmationPending, 'Escape cancels Ask')
      assert.equal((await state()).pages, 1)
      assert.equal((await state()).ledger.drain, 0)
      results.push({ name: `${variant}-${locale}-${theme}-escape`, passed: true, nativeTheme: actualNativeTheme })
    }

    for (const theme of ['arctic', 'crt-green', 'ember', 'miami', 'synthwave', 'terminal', 'vapor']) {
      await reset('ask')
      const palette = await themePalette(theme)
      await command('configure', { theme: theme === 'miami' ? 'light' : 'dark', palette })
      await controller.locator(supportsBackgroundClose ? '#closeMain' : '#quit').click()
      const prompt = await dialog()
      await assertDialogPalette(prompt, palette)
      await screenshot(prompt, `ask-custom-${theme}`)
      await pressClosingKey(prompt, 'Escape')
      await waitFor(async () => !(await state()).confirmationPending, 'custom-theme Escape cancels')
      assert.equal((await state()).ledger.drain, 0)
      results.push({ name: `custom-${theme}-palette`, passed: true })
    }

    await reset('ask')
    await command('configure', { theme: 'dark', palette: await themePalette('vapor') })
    await command('configure', { theme: 'system' })
    await controller.locator(supportsBackgroundClose ? '#closeMain' : '#quit').click()
    {
      const prompt = await dialog()
      const dark = await nativeApp.evaluate(({ nativeTheme }) => nativeTheme.shouldUseDarkColors)
      await assertDialogPalette(prompt, await themePalette(dark ? 'dark' : 'light'))
      // Simulate a native scheme change inside this isolated Electron process;
      // never change the user's macOS appearance preference.
      await nativeApp.evaluate(({ nativeTheme }, source) => { nativeTheme.themeSource = source }, dark ? 'light' : 'dark')
      await waitFor(async () => await prompt.evaluate(() => matchMedia('(prefers-color-scheme: dark)').matches) !== dark, 'open system-mode dialog follows native scheme')
      await assertDialogPalette(prompt, await themePalette(dark ? 'light' : 'dark'))
      await pressClosingKey(prompt, 'Escape')
      await waitFor(async () => !(await state()).confirmationPending, 'system-theme Escape cancels')
      results.push({ name: 'system-palette-reset-and-live-scheme', passed: true })
    }

    if (themesOnly) {
      await writeFile(join(output, 'theme-results.json'), JSON.stringify({ platform: process.platform, results, notApplicable, profile }, null, 2))
      console.log(JSON.stringify({ mode: 'themes-only', passed: results.length, output, profile }))
    } else {
      let prompt
      let value
      if (supportsBackgroundClose) {
        await reset('ask')
        await controller.locator('#closeMain').click()
        prompt = await dialog()
        assert.equal(await prompt.evaluate(() => document.activeElement.id), 'background')
        await pressClosingKey(prompt, 'Enter')
        await waitFor(async () => { const value = await state(); return !value.confirmationPending && !value.ownerVisible }, 'safe Ask default keeps the task running in background')
        assert.equal((await state()).taskAlive, true)
        assert.equal((await state()).pages, 1)
        assert.equal((await state()).ledger.drain, 0)
        results.push({ name: 'ask-keyboard-enter-background', passed: true })
        await controller.locator('#showMain').click()

        await reset('ask')
        await controller.locator('#closeMain').click()
        prompt = await dialog()
        await prompt.locator('#remember').check()
        await clickClosingButton(prompt, '#background')
        await waitFor(async () => { const value = await state(); return !value.confirmationPending && !value.ownerVisible }, 'remember background')
        value = await state()
        assert.equal(value.behavior, 'background')
        assert.equal(value.pages, 1)
        assert.equal(value.taskAlive, true)
        assert.equal(value.ledger.drain, 0)
        assert.equal(value.dialogCount, 1)
        results.push({ name: 'ask-background-remember', passed: true })
        await controller.locator('#showMain').click()
      }

      await reset('ask')
      await controller.locator('#repeatQuit').click()
      prompt = await dialog()
      assert.equal(await prompt.locator('#background').count(), 0, 'real Quit has the unified two-choice dialog')
      await screenshot(prompt, 'quit-active-pages')
      value = await canceled('cancel repeated menu Quit', '#keep', true)
      assert.equal(value.dialogCount, 1, 'repeated quits share one decision')
      results.push({ name: 'menu-quit-cancel-single-flight', passed: true, lateClickAckFaultInjected: true })

      for (const activity of ['idle', 'unknown']) {
        await reset('ask')
        await controller.locator('#activity').selectOption(activity)
        await controller.locator('#quit').click()
        prompt = await dialog()
        await screenshot(prompt, `quit-${activity}-pages`)
        await canceled(`cancel ${activity} task-state quit`)
        assert.equal((await state()).dialogCount, 1)
        results.push({ name: `menu-quit-${activity}-pages`, passed: true })
      }

      await reset('ask')
      await controller.locator('#quit').click()
      prompt = await dialog()
      assert.equal(await prompt.evaluate(() => document.activeElement.id), 'keep')
      await pressClosingKey(prompt, 'Enter')
      await waitFor(async () => !(await state()).confirmationPending, 'safe Quit default cancels')
      assert.equal((await state()).taskAlive, true)
      assert.equal((await state()).pages, 1)
      assert.equal((await state()).ledger.drain, 0)
      results.push({ name: 'quit-keyboard-enter-cancel', passed: true })

      await reset('ask')
      await controller.locator('#holdWriter').click()
      await controller.locator('#quit').click()
      prompt = await dialog()
      await clickClosingButton(prompt, '#confirm')
      await waitFor(async () => (await state()).phase === 'deferred', 'accepted decision waits for profile writer')
      assert.equal((await state()).pages, 1)
      await command('add-page')
      await controller.locator('#finishWriter').click()
      await waitFor(async () => !(await state()).confirmationPending, 'writer snapshot cancellation')
      value = await state()
      assert.equal(value.pages, 2)
      assert.equal(value.phase, 'running')
      assert.equal(value.writerClosed, false)
      assert.equal(value.ledger.drain, 0)
      results.push({ name: 'writer-new-page-aborts-quit', passed: true })

      await reset('ask')
      await controller.locator('#cleanupFailure').check()
      await controller.locator('#quit').click()
      prompt = await dialog()
      await clickClosingButton(prompt, '#confirm')
      await waitFor(async () => (await state()).ledger.acceptedExit === 1 && !(await state()).taskAlive,
        'unconfirmed cleanup still ends the owned child and commits exit')
      value = await state()
      assert.equal(value.phase, 'committed')
      assert.equal(value.taskAlive, false)
      assert.equal(value.ledger.acceptedExit, 1)
      assert.equal(value.ledger.errorBoxes.length, 0, 'ordinary quit never asks for another quit attempt')
      assert.ok(value.logs.some(entry => entry.event === 'quit_cleanup_finished' && entry.cleanupConfirmed === false),
        'exit does not turn incomplete cleanup into a successful receipt')
      results.push({ name: 'unconfirmed-cleanup-commits-exit', passed: true })

      for (const locale of locales) {
        await reset('ask')
        await controller.locator('#locale').selectOption(locale)
        await controller.locator('#longCopy').click()
        prompt = await dialog()
        await screenshot(prompt, `longest-copy-${locale}`)
        await pressClosingKey(prompt, 'Escape')
        await waitFor(async () => (await state()).logs.some(entry => entry.event === 'fixture_long_copy_decision'), 'longest-copy Escape')
        results.push({ name: `longest-copy-${locale}`, passed: true })
      }

      for (const locale of ['en', 'zh']) {
        for (const entry of supportsBackgroundClose ? ['closeMain', 'quit'] : ['quit']) {
          await reset('ask')
          await command('remove-pages')
          await controller.locator('#locale').selectOption(locale)
          await controller.locator('#activity').selectOption('unknown')
          await controller.locator(`#${entry}`).click()
          prompt = await dialog()
          assert.equal(await prompt.locator('#detail').count(), 0, 'no page warning without open pages')
          assert.equal(await prompt.locator('[role="dialog"]').getAttribute('aria-describedby'), 'message')
          await screenshot(prompt, `${entry}-no-pages-${locale}`)
          await pressClosingKey(prompt, 'Escape')
          await waitFor(async () => !(await state()).confirmationPending, 'cancel without open pages')
          const value = await state()
          assert.equal(value.taskAlive, true)
          assert.equal(value.pages, 0)
          assert.equal(value.ledger.drain, 0)
          results.push({ name: `${entry}-no-pages-${locale}`, passed: true })
        }
      }

      if (supportsBackgroundClose) {
        await reset('ask')
        await controller.locator('#closeMain').click()
        prompt = await dialog()
        await prompt.locator('#remember').check()
        await clickClosingButton(prompt, '#confirm')
        await waitFor(async () => (await state()).ledger.acceptedExit === 1, 'Ask Quit reaches the production exit boundary')
        value = await state()
        assert.equal(value.behavior, 'quit')
        assert.equal(value.dialogCount, 1, 'Ask Quit does not add another interruption dialog')
        assert.equal(value.taskAlive, false)
        assert.equal(value.pages, 0)
        results.push({ name: 'ask-quit-remember-single-dialog', passed: true })
      }

      await reset('ask')
      await controller.locator('#quit').click()
      prompt = await dialog()
      await prompt.keyboard.press('Tab')
      assert.equal(await prompt.evaluate(() => document.activeElement.id), 'confirm')
      await pressClosingKey(prompt, 'Enter')
      await waitFor(async () => (await state()).ledger.acceptedExit === 1, 'accepted real quit reaches production exit boundary')
      value = await state()
      assert.equal(value.taskAlive, false, 'the actual retained synthetic process is reaped')
      assert.equal(value.pages, 0, 'the real native page was closed')
      assert.equal(value.dialogCount, 1)
      results.push({ name: 'menu-quit-accept', passed: true })

      await reset('ask')
      await verifyRealExit()
      await writeFile(join(output, 'results.json'), JSON.stringify({ platform: process.platform, results, notApplicable, profile, final: value,
        limits: 'Production quit handlers/dialogs with real browser pages and an isolated synthetic child; the final scenario exits the actual Electron main process. Gateway activity/cleanup remain fixture adapters. Full Gateway/package acceptance is separate.' }, null, 2))
      console.log(JSON.stringify({ passed: results.length, output, profile }))
    }
  }
} finally {
  if (nativeApp) {
    try { await nativeApp.evaluate(async () => { await globalThis.__opensquillaQuitAcceptance?.command('stop-fixture') }) } catch {}
    await nativeApp.close().catch(() => {})
  }
}
