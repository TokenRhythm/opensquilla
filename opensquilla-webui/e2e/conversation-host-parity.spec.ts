import { expect, test, type Locator, type Page } from '@playwright/test'
import { mkdir, writeFile } from 'node:fs/promises'
import { existsSync, readFileSync, readdirSync } from 'node:fs'
import { createHash } from 'node:crypto'
import { fileURLToPath } from 'node:url'
import { MAIN, SIDE, OTHER, logs, installGateway } from './support/real-conversation-gateway'

// These tests enter the real routed ChatView and open the real workbench panel.
// Only the external Gateway is replaced. Neither conversation, its composables,
// nor its renderer is stubbed or manually reconstructed for a policy comparison.
// Match the registry's drop-a-folder manifest discovery, so new value themes
// automatically join the real-host visual parity matrix.
const themeDirectory = new URL('../src/themes/', import.meta.url)
const registeredThemes = readdirSync(themeDirectory, { withFileTypes: true }).filter(entry => entry.isDirectory())
  .flatMap(entry => {
    const manifestPath = new URL(`${entry.name}/manifest.ts`, themeDirectory)
    if (!existsSync(manifestPath)) return []
    const manifest = readFileSync(manifestPath, 'utf8')
    if (!/kind:\s*['"]value['"]/.test(manifest)) return []
    const id = manifest.match(/\bid:\s*['"]([^'"]+)['"]/)?.[1]
    if (!id) throw new Error(`Theme manifest has no literal id: ${entry.name}`)
    return [id]
  }).sort()


test.afterEach(async ({ page }, info) => {
  if (info.status !== info.expectedStatus) {
    const path = info.outputPath('gateway-and-render-errors.json')
    await writeFile(path, JSON.stringify(logs.get(page), null, 2))
    await info.attach('gateway-and-render-errors', { contentType: 'application/json', path })
  }
})


async function openHosts(page: Page) {
  await page.goto(`/control/chat?session=${encodeURIComponent(MAIN)}`)
  const main = page.locator('#app-main .chat').first()
  await expect(main).toContainText('Earlier main answer')
  await expect.poll(() => page.locator('.sidebar-brand-mark').evaluate(image => (image as HTMLImageElement).naturalWidth)).toBeGreaterThan(0)
  await page.getByTestId('topbar-workbench-toggle').click()
  await page.getByRole('button', { name: 'Assistant chat', exact: true }).click()
  const side = page.getByTestId('assistant-chat-panel')
  await expect(composer(side)).toBeVisible()
  for (const host of [main, side]) await expect(composer(host)).toHaveAttribute('contenteditable', 'true')
  return { main, side }
}

function composer(host: Locator) {
  return host.getByRole('textbox', { name: 'Message to send', exact: true })
}

async function captureReview(page: Page, name: string) {
  // These images show real production UI with the explicitly synthetic Gateway.
  const directory = new URL('../../../../.tmp/main-side-review-20261016/automated-screenshots/', import.meta.url)
  await mkdir(directory, { recursive: true })
  await page.screenshot({ path: fileURLToPath(new URL(`${name}.png`, directory)), fullPage: true })
}

async function send(host: Locator, text: string) {
  await composer(host).fill(text)
  await host.getByRole('button', { name: 'Send', exact: true }).click()
}

async function assertNoSettledExtras(host: Locator) {
  await expect(host.locator('.sources-row')).toHaveCount(0)
  await expect(host.locator('.conversation-live-edge .msg-ai-footer')).toHaveCount(0)
  await expect(host.locator('.chat-stop-btn')).toBeVisible()
  await expect(host.locator('.chat-stop-btn')).toBeEnabled()
}

async function assertLiveHistoryColumnAlignment(host: Locator) {
  const history = host.getByTestId('chat-message-row').locator('.msg-ai').first()
  const live = host.locator('.conversation-live-edge.msg-ai')
  await expect(history).toBeVisible()
  await expect(live).toBeVisible()
  await expect(live.locator('.live-answer')).toBeVisible()
  await expect.poll(async () => {
    const [historyRow, historyBody, liveRow, liveBody, liveAnswer] = await Promise.all([
      history.boundingBox(), history.locator('.msg-ai-main').boundingBox(),
      live.boundingBox(), live.locator('.msg-ai-main').boundingBox(), live.locator('.live-answer').boundingBox(),
    ])
    if (!historyRow || !historyBody || !liveRow || !liveBody || !liveAnswer) return Infinity
    return Math.max(
      Math.abs(historyRow.x - liveRow.x), Math.abs(historyRow.width - liveRow.width),
      Math.abs(historyBody.x - liveBody.x), Math.abs(historyBody.width - liveBody.width),
      Math.abs(historyBody.x - liveAnswer.x), Math.abs(historyBody.width - liveAnswer.width),
    )
  }, { message: 'Live activity and answer must share the historical assistant content column' }).toBeLessThan(1)
}

async function equalizeConversationWidths(page: Page, main: Locator, side: Locator) {
  const separator = page.getByRole('separator', { name: 'Resize workbench', exact: true })
  await expect(separator).toBeVisible()
  for (let attempt = 0; attempt < 3; attempt++) {
    const [left, right, handle] = await Promise.all([
      main.boundingBox(), side.locator('.chat').boundingBox(), separator.boundingBox(),
    ])
    if (!left || !right || !handle) throw new Error('Both conversation frames and their resize handle must be visible')
    if (Math.abs(left.width - right.width) < 2) return
    const startX = handle.x + handle.width / 2
    const y = handle.y + Math.min(120, handle.height / 2)
    await page.mouse.move(startX, y)
    await page.mouse.down()
    await page.mouse.move(startX - (left.width - right.width) / 2, y, { steps: 4 })
    await page.mouse.up()
  }
  await expect.poll(async () => Math.abs((await main.boundingBox())!.width - (await side.locator('.chat').boundingBox())!.width)).toBeLessThan(2)
}

async function semanticStyle(locator: Locator) {
  return locator.evaluate(element => {
    const properties = ['background-color', 'background-image', 'color', 'font-family', 'font-size', 'font-weight',
      'font-style', 'line-height', 'letter-spacing', 'text-transform', 'border-top-width', 'border-right-width',
      'border-bottom-width', 'border-left-width', 'border-top-color', 'border-radius', 'box-shadow',
      'outline-width', 'outline-color', 'outline-style', 'outline-offset', 'padding-top', 'padding-right',
      'padding-bottom', 'padding-left']
    const style = getComputedStyle(element)
    const styles = Object.fromEntries(properties.map(property => [property, style.getPropertyValue(property)]))
    let backdrop: Element | null = element
    let effectiveBackground = ''
    while (backdrop) {
      const background = getComputedStyle(backdrop).backgroundColor
      if (background !== 'rgba(0, 0, 0, 0)' && background !== 'transparent') {
        effectiveBackground = background
        break
      }
      backdrop = backdrop.parentElement
    }
    return { ...styles, effectiveBackground }
  })
}

async function awaitStyleTransition(locator: Locator) {
  await locator.evaluate(async element => {
    await Promise.all(element.getAnimations().filter(animation => animation.effect?.getComputedTiming().endTime !== Infinity)
      .map(animation => animation.finished.catch(() => undefined)))
  })
}

test.describe('real routed main and workbench side conversation hosts', () => {
  for (const kind of ['main', 'side'] as const) {
    test(`${kind} Stop remains directly clickable with a visible failure toast`, async ({ page }, info) => {
      const gateway = await installGateway(page, false, false, false, false, { audioConfigured: true })
      await page.addInitScript(() => {
        Object.defineProperty(window, 'MediaRecorder', { value: undefined, configurable: true })
      })
      await page.setViewportSize({ width: 1440, height: 900 })
      await page.goto(`/control/chat?session=${encodeURIComponent(MAIN)}`)
      const main = page.locator('#app-main .chat').first()
      await expect(main).toContainText('Earlier main answer')
      let host = main
      if (kind === 'side') {
        await page.getByTestId('topbar-workbench-toggle').click()
        await page.getByRole('button', { name: 'Assistant chat', exact: true }).click()
        host = page.getByTestId('assistant-chat-panel')
      }
      await send(host, `Keep ${kind} running while a notification is visible`)
      await expect.poll(() => gateway.requests.filter(request => request.method === 'chat.send').length).toBe(1)
      const stop = host.getByRole('button', { name: 'Stop current response', exact: true })
      await expect(stop).toBeVisible()
      // Real shared-composer action, with only the external browser capability
      // removed. No injected toast, manually reconstructed host or forced click.
      await host.getByRole('button', { name: 'Add', exact: true }).click()
      await page.getByRole('menuitem', { name: 'Record voice input', exact: true }).click()
      const toast = page.getByTestId('toast').filter({ hasText: 'Voice input is not supported in this browser' })
      await expect(toast).toBeVisible()
      expect(await stop.evaluate(element => {
        const rect = element.getBoundingClientRect()
        const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2)
        return hit === element || element.contains(hit)
      }), 'Visible notification must leave the actual Stop hit target uncovered').toBe(true)
      await page.screenshot({ path: info.outputPath(`${kind}-toast-stop.png`) })
      await stop.click()
      await expect(toast).toBeVisible()
      await expect.poll(() => gateway.requests.filter(request => request.method === 'chat.abort').length).toBe(1)
      expect(gateway.requests.find(request => request.method === 'chat.abort')?.params)
        .toMatchObject({ sessionKey: kind === 'main' ? MAIN : SIDE })
      await expect(stop).toHaveCount(0)
      await expect(host).toContainText('Interrupted answer.')
      expect(gateway.errors).toEqual([])
    })
  }

  test('aligns live activity and answers with history in wide main and side panes', async ({ page }) => {
    const gateway = await installGateway(page)
    await page.setViewportSize({ width: 2200, height: 1100 })
    await page.goto(`/control/chat?session=${encodeURIComponent(MAIN)}`)
    const main = page.locator('#app-main .chat').first()
    await expect(main).toContainText('Earlier main answer')
    const streamActivity = (key: string) => {
      gateway.emit(key, 'session.event.text_delta', { text: 'Checking the content column.', presentation: 'intermediate' })
      gateway.emit(key, 'session.event.tool_use_start', { tool_use_id: 'column-read', tool_name: 'read_file', input: { path: 'README.md' } })
      gateway.emit(key, 'session.event.text_delta', { text: 'The live answer stays in the same column.', presentation: 'answer' })
    }
    await send(main, 'Check the wide main column')
    await expect.poll(() => gateway.requests.filter(request => request.method === 'chat.send').length).toBe(1)
    streamActivity(MAIN)
    await assertLiveHistoryColumnAlignment(main)
    expect((await main.locator('.conversation-live-edge.msg-ai').boundingBox())!.width).toBeLessThanOrEqual(800)

    await page.getByTestId('topbar-workbench-toggle').click()
    await page.getByRole('button', { name: 'Assistant chat', exact: true }).click()
    const side = page.getByTestId('assistant-chat-panel')
    await send(side, 'Create side history')
    await expect.poll(() => gateway.requests.filter(request => request.method === 'chat.send').length).toBe(2)
    gateway.finish(SIDE, 'Earlier side answer')
    await expect(side).toContainText('Earlier side answer')
    await expect(side.locator('.chat-stop-btn')).toHaveCount(0)
    await send(side, 'Check the side column')
    await expect.poll(() => gateway.requests.filter(request => request.method === 'chat.send').length).toBe(3)
    streamActivity(SIDE)
    for (const width of [2200, 1200]) {
      await page.setViewportSize({ width, height: 1100 })
      for (const host of [main, side]) await assertLiveHistoryColumnAlignment(host)
    }
    // Shrinking prioritizes main. Measure each visible mobile pane separately;
    // two hidden zero-width columns must never satisfy this comparison.
    await page.setViewportSize({ width: 390, height: 1100 })
    await expect(side).not.toBeVisible()
    await assertLiveHistoryColumnAlignment(main)
    await page.getByTestId('topbar-workbench-toggle').click()
    await expect(main).not.toBeVisible()
    await assertLiveHistoryColumnAlignment(side)
    for (const key of [MAIN, SIDE]) gateway.finish(key, 'Column check complete.')
    for (const host of [main, side]) {
      await expect(host.locator('.conversation-live-edge.msg-ai')).toHaveCount(0)
      await expect(host.locator('.assistant-answer').last()).toContainText('Column check complete.')
    }
    expect(gateway.errors).toEqual([])
  })

  test('explains temporary chat only while empty, preserves it on hide and ends it on tab close', async ({ page }) => {
    const gateway = await installGateway(page, false, false, true)
    const { main, side } = await openHosts(page)
    const hint = side.getByTestId('temporary-conversation-empty')
    await expect(hint).toBeVisible()
    await expect(hint).toContainText('This is a temporary chat and won’t appear in your history.')
    await expect(hint).toContainText('Closing this tab, refreshing the page, or closing the app ends access to this chat.')
    await expect(hint).toContainText('Collapsing the sidebar keeps this chat available.')
    await expect(main.getByTestId('temporary-conversation-empty')).toHaveCount(0)
    await composer(side).fill('Retained temporary draft')
    await page.getByTestId('topbar-workbench-toggle').click()
    await expect(side).not.toBeVisible()
    await page.getByTestId('topbar-workbench-toggle').click()
    await expect(hint).toBeVisible()
    await expect(composer(side)).toHaveText('Retained temporary draft')
    expect(gateway.requests.filter(request => request.method === 'sessions.sidechat.create')).toHaveLength(0)
    await side.getByRole('button', { name: 'Send', exact: true }).click()
    await expect.poll(() => gateway.requests.filter(request => request.method === 'sessions.sidechat.create').length).toBe(1)
    await expect(hint).toHaveCount(0)
    gateway.releaseCreate()
    await expect.poll(() => gateway.requests.filter(request => request.method === 'chat.send').length).toBe(1)
    gateway.finish(SIDE, 'Temporary answer remains while collapsed.')
    await expect(side).toContainText('Temporary answer remains while collapsed.')
    await expect(hint).toHaveCount(0)
    await page.getByTestId('topbar-workbench-toggle').click()
    await expect(side).not.toBeVisible()
    expect(gateway.requests.filter(request => request.method === 'sessions.sidechat.close')).toHaveLength(0)
    await page.getByTestId('topbar-workbench-toggle').click()
    await expect(side).toContainText('Temporary answer remains while collapsed.')
    await expect(hint).toHaveCount(0)
    expect(gateway.requests.filter(request => request.method === 'sessions.sidechat.create')).toHaveLength(1)
    await page.getByRole('button', { name: 'Close tab: Assistant chat', exact: true }).click()
    await expect(side).toHaveCount(0)
    await expect.poll(() => gateway.requests.filter(request => request.method === 'sessions.sidechat.close').length).toBe(1)
    expect(gateway.requests.find(request => request.method === 'sessions.sidechat.close')?.params?.key).toBe(SIDE)
    expect(new URL(page.url()).searchParams.get('session')).toBe(MAIN)
    expect(gateway.errors).toEqual([])
  })

  for (const theme of registeredThemes) for (const viewportWidth of [1200, 1800]) {
    test(`matches main and side semantic styles in ${theme} at ${viewportWidth}px`, async ({ page }, info) => {
      const gateway = await installGateway(page)
      await page.setViewportSize({ width: viewportWidth, height: 1100 })
      await page.addInitScript(theme => {
        if (window === window.top) localStorage.setItem('opensquilla-theme', theme)
      }, theme)
      const { main, side } = await openHosts(page)
      // Focus style comparison needs equal-width panes. Responsive geometry with
      // navigation kept open is separately exercised by workbench-responsive-layout.
      if (viewportWidth === 1200) await page.getByRole('button', { name: 'Collapse sidebar', exact: true }).click()
      await expect(page.locator('html')).toHaveAttribute('data-theme', theme)
      // Initial loading includes the real theme's lazy world stylesheet/fonts.
      await page.waitForLoadState('networkidle')
      await page.evaluate(() => document.fonts.ready)
      await equalizeConversationWidths(page, main, side)
      const observations: Array<{ part: string; main: unknown; side: unknown }> = []
      const compare = async (part: string, left: Locator, right: Locator) => {
        await expect(left).toBeVisible()
        await expect(right).toBeVisible()
        const expected = await semanticStyle(left)
        await expect.poll(() => semanticStyle(right), { message: `${theme} ${viewportWidth}px ${part}` }).toEqual(expected)
        observations.push({ part, main: expected, side: await semanticStyle(right) })
      }
      await composer(main).fill('The same composer content')
      await composer(side).fill('The same composer content')
      await page.locator('.topbar h1').click()
      await compare('conversation frame', main, side.locator('.chat'))
      await compare('composer frame', main.locator('.chat-composer'), side.locator('.chat-composer'))
      await compare('composer input panel', main.locator('.chat-input-panel'), side.locator('.chat-input-panel'))
      await compare('editor typography', composer(main), composer(side))
      const mainHeader = await semanticStyle(page.locator('.topbar'))
      const sideHeader = await semanticStyle(page.locator('.workbench-host__chrome'))
      expect(sideHeader.effectiveBackground, `${theme} workbench header background`).toBe(mainHeader.effectiveBackground)
      observations.push({ part: 'header effective background', main: mainHeader.effectiveBackground, side: sideHeader.effectiveBackground })
      await composer(main).focus()
      await awaitStyleTransition(main.locator('.chat-input-panel'))
      const mainFocused = { panel: await semanticStyle(main.locator('.chat-input-panel')), editor: await semanticStyle(composer(main)) }
      await composer(side).focus()
      await awaitStyleTransition(side.locator('.chat-input-panel'))
      await expect.poll(async () => ({ panel: await semanticStyle(side.locator('.chat-input-panel')),
        editor: await semanticStyle(composer(side)) }), { message: `${theme} equal-width focus styles` }).toEqual(mainFocused)
      observations.push({ part: 'focused composer', main: mainFocused,
        side: { panel: await semanticStyle(side.locator('.chat-input-panel')), editor: await semanticStyle(composer(side)) } })
      await send(main, 'Show the same themed answer')
      await send(side, 'Show the same themed answer')
      await expect.poll(() => gateway.requests.filter(request => request.method === 'chat.send').length).toBe(2)
      for (const key of [MAIN, SIDE]) {
        gateway.emit(key, 'session.event.text_delta', { text: 'Checking shared evidence.', presentation: 'intermediate' })
        gateway.emit(key, 'session.event.tool_use_start', { tool_use_id: 'theme-web', tool_name: 'web_fetch', input: { url: 'https://example.com/theme' } })
      }
      for (const selector of ['.conversation-live-edge', '.assistant-activity--live', '.assistant-activity__live-head', '.tool-row']) {
        await compare(`live ${selector}`, main.locator(selector).last(), side.locator(selector).last())
      }
      await compare('user message', main.locator('.msg-user-bubble').last(), side.locator('.msg-user-bubble').last())
      const answer = '**Themed answer**\n\nThe same readable body and [evidence](https://example.com/theme).'
      const tool = { type: 'tool_use', tool_use_id: 'theme-web', name: 'web_fetch', input: { url: 'https://example.com/theme' },
        result: JSON.stringify({ url: 'https://example.com/theme', title: 'Theme evidence', content: 'Evidence body' }), execution_status: { status: 'success' } }
      for (const key of [MAIN, SIDE]) {
        gateway.emit(key, 'session.event.tool_result', { ...tool, tool_name: tool.name })
        gateway.emit(key, 'session.event.text_delta', { text: answer, presentation: 'answer' })
        gateway.finish(key, answer, 'succeeded', [{ type: 'text', text: 'Checking shared evidence.' }, tool, { type: 'text', text: answer }])
      }
      for (const host of [main, side]) await expect(host.locator('.chat-stop-btn')).toHaveCount(0)
      for (const selector of ['.assistant-answer', '.assistant-answer p', '.assistant-activity--settled', '.assistant-activity__summary', '.sources-row', '.msg-ai-footer']) {
        await compare(`settled ${selector}`, main.locator(selector).last(), side.locator(selector).last())
      }
      const styleEvidence = info.outputPath('theme-semantic-style-parity.json')
      await writeFile(styleEvidence, JSON.stringify({ theme, viewportWidth, observations }, null, 2))
      await info.attach('theme-semantic-style-parity', { path: styleEvidence, contentType: 'application/json' })
      expect(gateway.errors).toEqual([])
    })
  }

  test('preserves each accepted router card when an earlier call receipt settles after a later route', async ({ page }) => {
    const gateway = await installGateway(page)
    const { main, side } = await openHosts(page)
    const firstRoute = { tier: 'c1', model: 'shared-model', provider: 'profile-a', source: 'classifier',
      accepted_routing_mode: 'router', routing_applied: true,
      router_tier_snapshot: { version: 1, request_kind: 'image', tiers: [
        { tier: 'c0', model: 'shared-model', provider: 'profile-z', execution_kind: 'single_model' },
        { tier: 'c1', model: 'shared-model', provider: 'profile-a', execution_kind: 'single_model' },
      ] } }
    const laterRoute = { tier: 'c1', model: 'later/alpha', source: 'classifier', accepted_routing_mode: 'router',
      router_tier_snapshot: { version: 1, request_kind: 'text', tiers: [
        { tier: 'c0', model: 'later/zeta', execution_kind: 'single_model' },
        { tier: 'c1', model: 'later/alpha', execution_kind: 'single_model' },
      ] } }
    for (const host of [main, side]) {
      await host.getByRole('button', { name: 'Models & routing', exact: true }).click()
      await page.getByRole('menuitemradio', { name: /Intelligent model routing/ }).click()
      await send(host, 'Compare the accepted routes')
    }
    await expect.poll(() => gateway.requests.filter(request => request.method === 'chat.send').length).toBe(2)
    for (const key of [MAIN, SIDE]) {
      gateway.emit(key, 'session.event.router_decision', { ...firstRoute, model_call_id: '1.0', iteration: 1 })
      gateway.emit(key, 'session.event.text_delta', { text: 'Checking the first route.\n\n', model_call_id: '1.0', iteration: 1, presentation: 'intermediate' })
    }
    for (const host of [main, side]) {
      await expect(host.locator('.router-fx')).toHaveCount(1)
      await expect(host.locator('.router-fx .nm-base')).toHaveText(['shared-model · profile-z', 'shared-model · profile-a'])
    }
    const tool = { type: 'tool_use', tool_use_id: 'route-tool', name: 'web_fetch',
      input: { url: 'https://example.com/routes' },
      result: JSON.stringify({ url: 'https://example.com/routes', title: 'Route evidence', content: 'Evidence retained' }),
      execution_status: { status: 'success' } }
    for (const key of [MAIN, SIDE]) {
      gateway.emit(key, 'session.event.router_control_replay')
      gateway.emit(key, 'session.event.text_delta', { text: 'Checking the later route.\n\n', model_call_id: '2.0', iteration: 2, presentation: 'intermediate' })
      gateway.emit(key, 'session.event.router_decision', { ...laterRoute, model_call_id: '2.0', iteration: 2 })
      gateway.emit(key, 'session.event.tool_use_start', { tool_use_id: tool.tool_use_id, tool_name: tool.name, input: tool.input })
      gateway.emit(key, 'session.event.tool_result', { ...tool, tool_name: tool.name })
      gateway.emit(key, 'session.event.text_delta', { text: 'Both accepted routes retained.', model_call_id: '2.0', iteration: 2, presentation: 'answer' })
      for (const [index, route] of [firstRoute, laterRoute].entries()) gateway.appendHistory(key, {
        role: 'router', id: `router-${key}-${index}`, message_id: `router-${key}-${index}`, text: '',
        provenance_kind: 'router_decision', router_decision: route,
        usage: { router_model_call_id: `${index + 1}.0`, router_iteration: index + 1 },
      })
    }
    for (const host of [main, side]) {
      await expect(host.locator('.router-fx')).toHaveCount(2)
      await expect(host.locator('.conversation-live-edge .tool-row')).toHaveCount(1)
      await expect(host.locator('.conversation-live-edge')).toContainText('Both accepted routes retained.')
    }
    for (const key of [MAIN, SIDE]) gateway.finish(key, 'Both accepted routes retained.', 'succeeded', [tool], {
      // A late call-1 receipt must reconcile with its own accepted snapshot,
      // while the assistant body, tools and billing remain ordinary content.
      router_model_call_id: '1.0', model: 'shared-model', input_tokens: 321, output_tokens: 45,
      total_tokens: 366, cost_usd: 0.009,
      route_plan: { ...firstRoute, router_tier_snapshot: null },
    })
    const checkCards = async (host: Locator) => {
      await expect(host.locator('.router-fx')).toHaveCount(2)
      await expect(host.locator('.router-fx').nth(0).locator('.nm-base')).toHaveText(['shared-model · profile-z', 'shared-model · profile-a'])
      await expect(host.locator('.router-fx').nth(0).locator('.win .nm-base')).toHaveText('shared-model · profile-a')
      await expect(host.locator('.router-fx').nth(1).locator('.nm-base')).toHaveText(['zeta', 'alpha'])
      await expect(host.locator('.assistant-answer').last()).toHaveText('Both accepted routes retained.')
      await expect(host.locator('.sources-row')).toHaveCount(1)
    }
    for (const host of [main, side]) {
      await expect(host.locator('.chat-stop-btn')).toHaveCount(0)
      await checkCards(host)
      await host.getByRole('button', { name: 'Usage details', exact: true }).last().click()
      await expect(host.locator('.msg-meta-popover').last()).toContainText('shared-model')
      await expect(host.locator('.msg-meta-popover').last()).toContainText('321')
      await expect(host.locator('.msg-meta-popover').last()).toContainText('45')
      await host.getByRole('button', { name: 'Usage details', exact: true }).last().click()
    }
    const configReads = gateway.requests.filter(request => request.method === 'config.get').length
    gateway.changeRouterConfig({ c0: { model: 'current/replacement-z', image_only: false },
      c1: { model: 'current/replacement-a', image_only: false } })
    await expect.poll(() => gateway.requests.filter(request => request.method === 'config.get').length).toBeGreaterThan(configReads)
    for (const host of [main, side]) await checkCards(host)
    expect(new URL(page.url()).searchParams.get('session')).toBe(MAIN)
    expect(gateway.errors).toEqual([])
  })

  test('keeps simultaneous main and side slash palettes and their editor ARIA references independent', async ({ page }) => {
    const gateway = await installGateway(page)
    const { main, side } = await openHosts(page)
    await composer(main).fill('/')
    await expect(main.locator('.chat-slash')).toBeVisible()
    await composer(side).fill('/')
    const paletteIds: string[] = []
    for (const host of [main, side]) {
      const editor = composer(host)
      await expect(editor).toHaveAttribute('aria-expanded', 'true')
      const paletteId = await editor.getAttribute('aria-controls')
      const activeOptionId = await editor.getAttribute('aria-activedescendant')
      expect(paletteId).toBeTruthy()
      expect(activeOptionId).toBeTruthy()
      paletteIds.push(paletteId!)
      const palette = page.locator(`[id="${paletteId}"]`)
      await expect(palette).toHaveCount(1)
      await expect(palette).toHaveAttribute('role', 'listbox')
      await expect(palette).toBeVisible()
      await expect(page.locator(`[id="${activeOptionId}"]`)).toHaveCount(1)
      await expect(palette.locator(`[id="${activeOptionId}"]`)).toHaveAttribute('role', 'option')
    }
    expect(new Set(paletteIds).size).toBe(2)
    await composer(side).press('Escape')
    await expect(composer(side)).toHaveAttribute('aria-expanded', 'false')
    await expect(main.locator('.chat-slash')).toBeVisible()
    expect(gateway.requests.filter(request => request.method === 'sessions.sidechat.create')).toHaveLength(0)
    expect(gateway.errors).toEqual([])
  })

  test('carries immutable visualize receipts through both real live edges into interactive canvases', async ({ page }) => {
    const gateway = await installGateway(page)
    const documents = [MAIN, SIDE].map((key, index) => {
      const label = index === 0 ? 'Main live canvas' : 'Side live canvas'
      const html = `<!doctype html><html><body><h1>${label}</h1><button onclick="this.textContent='Clicked'">Try canvas</button></body></html>`
      const documentId = `doc_host_${index}`
      const revisionId = 'rev_live'
      const receipt = { documentId, revisionId, artifactId: documentId, widgetId: `host_${index}`,
        version: 1, name: `${label}.html`, mime: 'text/html', size: Buffer.byteLength(html),
        sha256: createHash('sha256').update(html).digest('hex'), marker: `visualize:${documentId}:${revisionId}` }
      return { key, label, html, receipt, callId: `widget-${index}` }
    })
    const reads: Array<{ documentId: string; revisionId: string | null; sessionKey: string }> = []
    await page.route('**/api/v1/artifact-documents/*?**', route => {
      const url = new URL(route.request().url())
      const documentId = url.pathname.split('/').pop()!
      const document = documents.find(item => item.receipt.documentId === documentId)
      if (!document) return route.fulfill({ status: 404 })
      reads.push({ documentId, revisionId: url.searchParams.get('revisionId'),
        sessionKey: route.request().headers()['x-opensquilla-session-key'] || '' })
      return route.fulfill({ contentType: 'text/html', body: document.html })
    })
    const { main, side } = await openHosts(page)
    await send(main, 'Show an interactive canvas')
    await send(side, 'Show an interactive canvas')
    await expect.poll(() => gateway.requests.filter(request => request.method === 'chat.send').length).toBe(2)
    for (const document of documents) {
      gateway.emit(document.key, 'session.event.tool_use_start', {
        tool_use_id: document.callId, tool_name: 'show_widget', input: { title: document.label },
      })
      gateway.emit(document.key, 'session.event.tool_result', {
        tool_use_id: document.callId, tool_name: 'show_widget', name: 'show_widget',
        result: JSON.stringify(document.receipt), execution_status: { status: 'success' },
      })
      gateway.emit(document.key, 'session.event.text_delta', {
        text: `Before canvas.\n\n\`${document.receipt.marker}\`\n\nAfter canvas.\n\n`, presentation: 'answer',
      })
    }
    for (const [index, host] of [main, side].entries()) {
      const document = documents[index]!
      const live = host.locator('.conversation-live-edge')
      await expect(host.locator('.chat-stop-btn')).toBeVisible()
      await expect(live.locator('[data-visualize-canvas]')).toHaveCount(1)
      await expect(live).not.toContainText(document.receipt.marker)
      const frame = live.frameLocator('[data-visualize-canvas] iframe')
      await expect(frame.getByRole('heading', { name: document.label, exact: true })).toBeVisible()
      await frame.getByRole('button', { name: 'Try canvas', exact: true }).click()
      await expect(frame.getByRole('button', { name: 'Clicked', exact: true })).toBeVisible()
      expect(reads).toContainEqual({ documentId: document.receipt.documentId,
        revisionId: document.receipt.revisionId, sessionKey: document.key })
    }
    expect(new URL(page.url()).searchParams.get('session')).toBe(MAIN)
    expect(gateway.errors).toEqual([])
  })

  test('repairs failed expanded visualize canvases through the owning main and side composers without sending', async ({ page }, info) => {
    const gateway = await installGateway(page)
    await page.setViewportSize({ width: 1800, height: 1100 })
    const documents = [MAIN, SIDE].flatMap((key, index) => [1, 2].map(version => {
      const label = `${index === 0 ? 'Main' : 'Side'} repair canvas.html`
      // Let the real iframe finish mounting before failing from a user action
      // in its expanded presentation. This is a guest runtime error, not a
      // mocked host callback or an unavailable resource.
      const html = `<!doctype html><html><body><h1>${label} v${version}</h1><button onclick="const particles = undefined; for (const particle of particles) {}">Start broken canvas</button></body></html>`
      const documentId = `doc_repair_${index}`
      const revisionId = `rev_repair_${version}`
      const receipt = { documentId, revisionId, artifactId: `${documentId}_${version}`, widgetId: `repair_${index}`,
        version, name: label, mime: 'text/html', size: Buffer.byteLength(html),
        sha256: createHash('sha256').update(html).digest('hex'), marker: `visualize:${documentId}:${revisionId}` }
      return { key, label, html, receipt, callId: `repair-widget-${index}-${version}` }
    }))
    const reads: Array<{ documentId: string; revisionId: string | null; sessionKey: string }> = []
    await page.route('**/api/v1/artifact-documents/*?**', route => {
      const url = new URL(route.request().url())
      const documentId = url.pathname.split('/').pop()!
      const revisionId = url.searchParams.get('revisionId')
      const document = documents.find(item => item.receipt.documentId === documentId && item.receipt.revisionId === revisionId)
      if (!document) return route.fulfill({ status: 404 })
      reads.push({ documentId, revisionId, sessionKey: route.request().headers()['x-opensquilla-session-key'] || '' })
      return route.fulfill({ contentType: 'text/html', body: document.html })
    })
    const { main, side } = await openHosts(page)
    for (const host of [main, side]) await send(host, 'Show a canvas to review')
    await expect.poll(() => gateway.requests.filter(request => request.method === 'chat.send').length).toBe(2)
    const publish = (document: typeof documents[number]) => {
      const toolName = document.receipt.version === 1 ? 'show_widget' : 'edit_widget'
      gateway.emit(document.key, 'session.event.tool_use_start', {
        tool_use_id: document.callId, tool_name: toolName, input: { title: document.label },
      })
      gateway.emit(document.key, 'session.event.tool_result', {
        tool_use_id: document.callId, tool_name: toolName, name: toolName,
        result: JSON.stringify(document.receipt), execution_status: { status: 'success' },
      })
      gateway.emit(document.key, 'session.event.text_delta', {
        text: `\n\n\`${document.receipt.marker}\`\n\n`, presentation: 'answer',
      })
    }
    for (const document of documents.filter(item => item.receipt.version === 1)) publish(document)
    await composer(main).fill('Keep my main draft. 保留我的输入。')
    await composer(side).fill('Keep my side draft. 保留我的输入。')
    for (const [index, host] of [main, side].entries()) {
      const key = index === 0 ? MAIN : SIDE
      const otherEditor = composer(index === 0 ? side : main)
      const otherDraft = await otherEditor.innerText()
      const editor = composer(host)
      const originalDraft = await editor.innerText()
      let firstRepair = ''
      for (const version of [1, 2]) {
        const document = documents.find(item => item.key === key && item.receipt.version === version)!
        if (version === 2) publish(document)
        const canvas = host.locator('[data-visualize-canvas]:visible').last()
        await expect(canvas.frameLocator('iframe').getByRole('heading', { name: `${document.label} v${version}`, exact: true })).toBeVisible()
        await canvas.getByRole('button', { name: 'Expand', exact: true }).click()
        await expect(canvas).toHaveAttribute('aria-modal', 'true')
        expect(await canvas.evaluate(element => element.matches(':modal'))).toBe(true)
        await canvas.frameLocator('iframe').getByRole('button', { name: 'Start broken canvas', exact: true }).click()
        await expect(canvas).toContainText('This canvas could not run')
        const details = canvas.locator('details')
        await expect(details).toHaveCount(1)
        await expect(details).not.toHaveAttribute('open', '')
        await expect(details).toContainText('particles is not iterable')
        if (version === 1) await page.screenshot({ path: info.outputPath(`${index === 0 ? 'main' : 'side'}-expanded-canvas-error.png`) })
        const repair = canvas.getByRole('button', { name: 'Ask to repair', exact: true })
        await repair.click()
        await expect(page.locator('dialog:modal')).toHaveCount(0)
        await expect(editor).toBeFocused()
        await expect(otherEditor).toHaveText(otherDraft)
        const draft = await editor.innerText()
        expect(draft).toContain(originalDraft)
        for (const identity of [document.receipt.documentId, document.receipt.revisionId, document.receipt.sha256, document.receipt.widgetId]) {
          expect(draft).toContain(identity)
        }
        const pinnedSource = draft.split('\n').find(line => line.startsWith('{') && line.includes(document.receipt.revisionId))
        expect(JSON.parse(pinnedSource!)).toEqual({ documentId: document.receipt.documentId,
          revisionId: document.receipt.revisionId, sha256: document.receipt.sha256,
          widgetId: document.receipt.widgetId, version })
        expect(draft).toContain('particles is not iterable')
        await repair.click()
        await expect(editor).toHaveText(draft)
        await expect(editor).toBeFocused()
        if (version === 1) {
          firstRepair = draft
          await editor.fill('')
          await repair.click()
          await expect(editor).toBeFocused()
          const repairedAfterClear = await editor.innerText()
          expect(repairedAfterClear).toContain(document.receipt.revisionId)
          expect(repairedAfterClear).not.toContain(originalDraft)
          // Restore the actual user's text plus its first repair request before
          // the new immutable revision arrives; a different repair must append.
          await editor.fill(firstRepair)
        } else {
          expect(draft).toContain(firstRepair)
          expect(draft).toContain('rev_repair_1')
          expect(draft).toContain('rev_repair_2')
          expect(draft).not.toBe(firstRepair)
        }
        expect(reads).toContainEqual({ documentId: document.receipt.documentId,
          revisionId: document.receipt.revisionId, sessionKey: key })
        expect(gateway.requests.filter(request => request.method === 'chat.send')).toHaveLength(2)
      }
    }
    // Uncaught guest errors are the deliberate fixture; any host exception is
    // still a failure. A repair only drafts, including while a turn is running.
    expect(gateway.errors.filter(error => !error.includes('particles is not iterable'))).toEqual([])
    expect(new URL(page.url()).searchParams.get('session')).toBe(MAIN)
    expect(gateway.requests.filter(request => request.method === 'chat.send')).toHaveLength(2)
  })

  test('keeps an unsent side draft across panel changes and filters only policy actions', async ({ page }) => {
    const gateway = await installGateway(page)
    const { main, side } = await openHosts(page)
    await side.getByRole('button', { name: 'Execution mode: Full Access', exact: true }).click()
    await page.getByRole('radio', { name: /^Safe / }).click()
    await composer(side).fill('An unsent side draft')
    await send(main, 'Open a code file')
    await expect.poll(() => gateway.requests.filter(request => request.method === 'chat.send').length).toBe(1)
    gateway.finish(MAIN, '[Code](draft.ts)')
    await main.getByRole('link', { name: 'Open draft.ts', exact: true }).click()
    await expect(side).not.toBeVisible()
    await page.getByRole('tab', { name: 'Assistant chat', exact: true }).click()
    await expect(composer(side)).toHaveText('An unsent side draft')
    await expect(side.getByRole('button', { name: 'Execution mode: Safe', exact: true })).toBeVisible()
    await expect(main.getByRole('button', { name: 'Execution mode: Full Access', exact: true })).toBeVisible()
    expect(gateway.requests.filter(request => request.method === 'sessions.sidechat.create')).toHaveLength(0)
    await composer(main).fill('/')
    for (const name of ['/plan', '/goal']) await expect(page.getByRole('option', { name: new RegExp(`^${name} `) })).toBeVisible()
    await page.keyboard.press('Escape')
    await composer(side).fill('/')
    await expect(side.locator('.chat-slash')).toBeVisible()
    for (const name of ['/fork', '/plan', '/goal', '/new']) await expect(page.getByRole('option', { name: new RegExp(`^${name} `) })).toHaveCount(0)
    await composer(side).fill('An unsent side draft')
    expect(gateway.requests.filter(request => request.method === 'sessions.sidechat.create')).toHaveLength(0)
    expect(new URL(page.url()).searchParams.get('session')).toBe(MAIN)
    expect(gateway.errors).toEqual([])
  })

  test('restores the same side child and draft after New task and return to the parent route', async ({ page }) => {
    const gateway = await installGateway(page)
    const { main, side } = await openHosts(page)
    await send(side, 'Side question before navigation')
    await expect.poll(() => gateway.requests.filter(request => request.method === 'chat.send').length).toBe(1)
    gateway.finish(SIDE, 'Retained side answer')
    await expect(side).toContainText('Retained side answer')
    await composer(side).fill('Retained side draft')
    await page.getByRole('button', { name: 'New task', exact: true }).click()
    await expect(page).toHaveURL(/\/chat\/new/)
    await expect(side).not.toBeVisible()
    await page.getByRole('button', { name: 'Main parity conversation', exact: true }).click()
    await expect(main).toContainText('Earlier main answer')
    await expect(side).toContainText('Retained side answer')
    await expect(composer(side)).toHaveText('Retained side draft')
    expect(gateway.requests.filter(request => request.method === 'sessions.sidechat.create')).toHaveLength(1)
    expect(gateway.requests.filter(request => request.method === 'sessions.sidechat.close')).toHaveLength(0)
    expect(new URL(page.url()).searchParams.get('session')).toBe(MAIN)
    expect(gateway.errors).toEqual([])
  })

  test('applies a side routing choice before its first turn without changing the parent route setting', async ({ page }) => {
    const gateway = await installGateway(page)
    const { main, side } = await openHosts(page)
    await side.getByRole('button', { name: 'Models & routing', exact: true }).click()
    await page.getByRole('menuitemradio', { name: /Intelligent model routing/ }).click()
    expect(gateway.requests.filter(request => request.method === 'sessions.sidechat.create')).toHaveLength(0)
    await send(side, 'Use the selected routing mode')
    await expect.poll(() => gateway.requests.filter(request => request.method === 'chat.send').length).toBe(1)
    expect(gateway.requests.filter(request => request.method === 'sessions.routing.set')).toMatchObject([
      { params: { sessionKey: SIDE, mode: 'router', expectedRevision: 1 } },
    ])
    await expect(main.getByRole('button', { name: 'Models & routing', exact: true })).toContainText('Fixed model')
    expect(new URL(page.url()).searchParams.get('session')).toBe(MAIN)
    expect(gateway.errors).toEqual([])
  })

  for (const choice of ['routing', 'model'] as const) {
    test(`retries the side ${choice} choice on the same child before sending its preserved draft`, async ({ page }) => {
      const selectedModel = 'Retry model A'
      const gateway = await installGateway(page, false, true, false, false,
        choice === 'model' ? { draftModelName: selectedModel } : {})
      const { main, side } = await openHosts(page)
      const routingButton = side.getByRole('button', { name: 'Models & routing', exact: true })
      await routingButton.click()
      if (choice === 'model') {
        await page.getByRole('menuitem', { name: /^Fixed model/ }).click()
        await page.getByRole('option', { name: new RegExp(selectedModel) }).click()
      } else {
        await page.getByRole('menuitemradio', { name: /Intelligent model routing/ }).click()
      }
      const selectionLabel = choice === 'model' ? selectedModel : 'Intelligent model routing'
      await expect(routingButton).toContainText(selectionLabel)
      const text = 'Keep this draft after a routing conflict'
      const attachmentText = 'Keep the exact attachment across side routing retries'
      await side.locator('input[type="file"]').setInputFiles({
        name: 'routing-retry.txt', mimeType: 'text/plain', buffer: Buffer.from(attachmentText),
      })
      await expect(side.locator('.attachment-chip--busy')).toHaveCount(0)
      await composer(side).fill(text)
      for (const attempt of [1, 2]) {
        await side.getByRole('button', { name: 'Send', exact: true }).click()
        await expect.poll(() => gateway.requests.filter(request => request.method === 'sessions.routing.set').length).toBe(attempt)
        await expect(routingButton).toBeEnabled()
        await expect(routingButton).toContainText(selectionLabel)
        await expect(composer(side)).toHaveText(text)
        await expect(side.locator('.attachment-chip')).toContainText('routing-retry.txt')
        await expect(page.getByText('Routing changed in another window.', { exact: true }).last()).toBeVisible()
        expect(gateway.requests.filter(request => request.method === 'chat.send')).toHaveLength(0)
        expect(gateway.requests.filter(request => request.method === 'sessions.sidechat.create')).toHaveLength(1)
      }
      gateway.setRoutingConflict(false)
      await side.getByRole('button', { name: 'Send', exact: true }).click()
      await expect.poll(() => gateway.requests.filter(request => request.method === 'chat.send').length).toBe(1)
      const routingRequests = gateway.requests.filter(request => request.method === 'sessions.routing.set')
      expect(routingRequests).toHaveLength(3)
      for (const request of routingRequests) expect(request.params).toMatchObject({
        sessionKey: SIDE, mode: choice === 'model' ? 'direct' : 'router', expectedRevision: 1,
        modelSelection: choice === 'model' ? { model: selectedModel, provider: 'synthetic' } : null,
      })
      const submitted = gateway.requests.find(request => request.method === 'chat.send')!
      expect(gateway.requests.indexOf(routingRequests[2]!)).toBeLessThan(gateway.requests.indexOf(submitted))
      expect(submitted.params).toMatchObject({ sessionKey: SIDE, message: text, attachments: [{
        name: 'routing-retry.txt', mime: 'text/plain', data: Buffer.from(attachmentText).toString('base64'),
      }] })
      expect(gateway.requests.filter(request => request.method === 'sessions.sidechat.create')).toHaveLength(1)
      expect(gateway.requests.filter(request => request.method === 'sessions.sidechat.close')).toHaveLength(0)
      await expect(main.getByRole('button', { name: 'Models & routing', exact: true })).toContainText('Fixed model')
      expect(new URL(page.url()).searchParams.get('session')).toBe(MAIN)
      expect(gateway.errors).toEqual([])
    })
  }

  test('freezes only the preparing side settings until its child is ready', async ({ page }) => {
    const gateway = await installGateway(page, false, false, true)
    const { main, side } = await openHosts(page)
    await send(side, 'Wait for child preparation')
    await expect.poll(() => gateway.requests.filter(request => request.method === 'sessions.sidechat.create').length).toBe(1)
    await expect(side.getByRole('button', { name: 'Models & routing', exact: true })).toBeDisabled()
    await expect(side.getByRole('button', { name: /^Execution mode:/ })).toBeDisabled()
    await expect(main.getByRole('button', { name: 'Models & routing', exact: true })).toBeEnabled()
    expect(gateway.requests.filter(request => request.method === 'chat.send')).toHaveLength(0)
    gateway.releaseCreate()
    await expect.poll(() => gateway.requests.filter(request => request.method === 'chat.send').length).toBe(1)
    expect(gateway.requests.find(request => request.method === 'chat.send')?.params?._source).toMatchObject({ runMode: 'safe' })
    gateway.finish(SIDE)
    await expect(side.getByRole('button', { name: 'Models & routing', exact: true })).toBeEnabled()
    expect(gateway.errors).toEqual([])
  })

  test('shares the complete live-to-settled web-tool rendering path and isolates session ownership', async ({ page }) => {
    const gateway = await installGateway(page)
    const { main, side } = await openHosts(page)
    for (const frame of [main, side.locator('.chat')]) {
      await expect.poll(() => frame.evaluate(element => {
        const style = getComputedStyle(element)
        return { border: style.borderWidth, radius: style.borderRadius, shadow: style.boxShadow }
      })).toEqual({ border: '0px', radius: '0px', shadow: 'none' })
    }
    expect(gateway.requests.filter(request => request.method === 'sessions.sidechat.create')).toHaveLength(0)
    const initialUrl = page.url()
    const initialHeader = await page.locator('.topbar').textContent()
    const initialStoredSession = await page.evaluate(() => localStorage.getItem('opensquilla_active_session'))
    await send(main, 'Search and compare')
    await expect(main.locator('.chat-stop-btn')).toBeVisible()
    await expect(side.locator('.chat-stop-btn')).toHaveCount(0)
    await send(side, 'Search and compare')
    await expect.poll(() => gateway.requests.filter(request => request.method === 'chat.send').length).toBe(2)
    expect(gateway.requests.filter(request => request.method === 'sessions.sidechat.create')).toHaveLength(1)
    expect(gateway.requests.filter(request => request.method === 'chat.send').map(request => request.params?.sessionKey ?? request.params?.session_key ?? request.params?.key)).toEqual([MAIN, SIDE])
    expect(gateway.requests.filter(request => request.method === 'chat.send')[1]?.params?._source).toMatchObject({ runMode: 'safe' })
    expect(page.url()).toBe(initialUrl)
    expect(await page.locator('.topbar').textContent()).toBe(initialHeader)
    expect(await page.evaluate(() => localStorage.getItem('opensquilla_active_session'))).toBe(initialStoredSession)
    for (const key of [MAIN, SIDE]) {
      gateway.emit(key, 'session.event.text_delta', { text: 'Checking sources.', presentation: 'intermediate' })
      gateway.emit(key, 'session.event.tool_use_start', { tool_use_id: 'read-web', tool_name: 'web_fetch', input: { url: 'https://example.com/news' } })
    }
    for (const host of [main, side]) {
      await expect(host.locator('.conversation-live-edge')).toContainText('Checking sources.')
      await expect(host.locator('.conversation-live-edge .tool-row')).toHaveCount(1)
      await assertNoSettledExtras(host)
    }
    const tool = { type: 'tool_use', tool_use_id: 'read-web', name: 'web_fetch', input: { url: 'https://example.com/news' },
      result: JSON.stringify({ url: 'https://example.com/news', title: 'Synthetic news', content: 'News body' }), execution_status: { status: 'success' } }
    for (const key of [MAIN, SIDE]) {
      gateway.emit(key, 'session.event.tool_result', { ...tool, tool_name: tool.name })
      gateway.emit(key, 'session.event.text_delta', { text: 'Final answer.', presentation: 'answer' })
    }
    for (const host of [main, side]) {
      await expect(host.locator('.conversation-live-edge')).toContainText('Final answer.')
      await assertNoSettledExtras(host)
    }
    const stopControls = () => page.locator('.chat-stop-btn').evaluateAll(elements => elements.map(element => ({
      disabled: (element as HTMLButtonElement).disabled, busy: element.getAttribute('aria-busy'), hover: element.matches(':hover'),
      background: getComputedStyle(element).backgroundColor, color: getComputedStyle(element).color,
    })))
    const stopBeforePointerReset = await stopControls()
    for (const host of [main, side]) {
      const stop = host.locator('.chat-stop-btn')
      await stop.hover()
      await expect(stop).toBeEnabled()
      await awaitStyleTransition(stop)
      // Normalize the actual theme token and computed paint through Canvas's
      // CSS color parser without changing or stubbing the rendered button.
      const paint = await stop.evaluate(element => {
        const context = document.createElement('canvas').getContext('2d')!
        const style = getComputedStyle(element)
        context.fillStyle = style.getPropertyValue('--danger-fill').trim()
        const expected = context.fillStyle
        context.fillStyle = style.backgroundColor
        return { actual: context.fillStyle, expected }
      })
      expect(paint.actual).toBe(paint.expected)
    }
    await captureReview(page, '07-side-stop-hover-enabled')
    await page.mouse.move(0, 0)
    for (const host of [main, side]) await awaitStyleTransition(host.locator('.chat-stop-btn'))
    const stopAfterPointerReset = await stopControls()
    console.log('Stop controls before/after pointer reset', JSON.stringify({ before: stopBeforePointerReset, after: stopAfterPointerReset }))
    expect(stopAfterPointerReset[0]).toEqual(stopAfterPointerReset[1])
    await captureReview(page, '01-main-side-running')
    for (const key of [MAIN, SIDE]) gateway.finish(key, 'Final answer.', 'succeeded', [
      { type: 'text', text: 'Checking sources.' }, tool, { type: 'text', text: 'Final answer.' },
    ])
    for (const host of [main, side]) {
      await expect(host.locator('.chat-stop-btn')).toHaveCount(0)
      await expect(host.locator('.conversation-live-edge')).toHaveCount(0)
      await expect(host.locator('.sources-row')).toHaveCount(1)
      await expect(host.locator('.assistant-answer').last()).toHaveText('Final answer.')
      await expect(host.locator('.msg-ai-footer').last()).toBeVisible()
      await host.getByRole('button', { name: 'Usage details', exact: true }).last().click()
      await expect(host.locator('.msg-meta-popover').last()).toContainText('test-model')
      await expect(host.locator('.msg-meta-popover').last()).toContainText('120')
      await expect(host.locator('.msg-meta-popover').last()).toContainText('30')
      if (host === side) await captureReview(page, '03-side-settled-usage')
      await host.getByRole('button', { name: 'Usage details', exact: true }).last().click()
      await host.getByRole('button', { name: 'Models & routing', exact: true }).click()
      await expect(page.getByRole('menuitemradio', { name: /Intelligent model routing/ })).toBeVisible()
      if (host === side) await captureReview(page, '04-side-routing-menu')
      await page.keyboard.press('Escape')
    }
    expect(await main.locator('.sources-row').textContent()).toBe(await side.locator('.sources-row').textContent())
    for (const name of ['Fork from here', 'Edit', 'Regenerate']) {
      await expect(main.getByRole('button', { name, exact: true }).first()).toBeAttached()
      await expect(side.getByRole('button', { name, exact: true })).toHaveCount(0)
    }
    await side.getByRole('button', { name: 'Add', exact: true }).click()
    await expect(page.getByRole('menu')).toBeVisible()
    await expect(page.getByRole('menuitem', { name: /fork|plan mode|goal mode/i })).toHaveCount(0)
    await page.keyboard.press('Escape')
    expect(page.url()).toBe(initialUrl)
    expect(gateway.errors).toEqual([])
  })

  for (const modernSnapshots of [false, true]) test(`preserves both ${modernSnapshots ? 'modern segmented' : 'legacy'} live readers on reconnect and closes only the side owner`, async ({ page }) => {
    const gateway = await installGateway(page, false, false, false, modernSnapshots)
    const { main, side } = await openHosts(page)
    await send(main, 'Main recovery turn')
    await send(side, 'Side recovery turn')
    await expect.poll(() => gateway.requests.filter(request => request.method === 'chat.send').length).toBe(2)
    for (const key of [MAIN, SIDE]) gateway.emit(key, 'session.event.text_delta', { text: 'Retained across reconnect.' })
    for (const host of [main, side]) await expect(host.locator('.conversation-live-edge')).toContainText('Retained across reconnect.')
    const readsBefore = gateway.requests.filter(request => request.method === 'sessions.messages.subscribe').length
    gateway.reconnect()
    await expect.poll(() => gateway.requests.filter(request => request.method === 'sessions.messages.subscribe').length,
      { timeout: 15_000 }).toBeGreaterThan(readsBefore)
    for (const host of [main, side]) {
      await expect(host.locator('.conversation-live-edge')).toContainText('Retained across reconnect.')
      await expect(host.locator('.chat-stop-btn')).toBeVisible()
      await expect(host.locator('.chat-stop-btn')).toBeEnabled()
      await expect(host.locator('.conversation-live-edge')).not.toContainText('Retained across reconnect.Retained across reconnect.')
    }
    if (modernSnapshots) {
      await expect.poll(() => [...new Set(gateway.snapshotInstalls.filter(install => install.connection === 2).map(install => install.key))].sort())
        .toEqual([MAIN, SIDE].sort())
      expect(gateway.requests.filter(request => request.method === 'sessions.messages.snapshot')).toHaveLength(0)
    }
    expect(gateway.requests.filter(request => request.method === 'chat.send')).toHaveLength(2)
    expect(gateway.requests.filter(request => request.method === 'sessions.sidechat.create')).toHaveLength(1)
    await page.getByTestId('topbar-workbench-toggle').click()
    await expect(side).not.toBeVisible()
    await page.getByTestId('topbar-workbench-toggle').click()
    await expect(side.locator('.conversation-live-edge')).toContainText('Retained across reconnect.')
    expect(gateway.requests.filter(request => request.method === 'sessions.sidechat.create')).toHaveLength(1)
    await side.locator('.chat-stop-btn').click()
    await expect(side.locator('.chat-stop-btn')).toHaveCount(0)
    await composer(main).fill('Main draft survives closing the side')
    await composer(side).fill('Unsent dirty side draft at disposal')
    await page.getByRole('button', { name: 'Close tab: Assistant chat', exact: true }).click()
    await expect(side).toHaveCount(0)
    await expect.poll(() => gateway.requests.filter(request => request.method === 'sessions.sidechat.close').length).toBe(1)
    expect(gateway.requests.find(request => request.method === 'sessions.sidechat.close')?.params?.key).toBe(SIDE)
    await expect(main.locator('.chat-stop-btn')).toBeVisible()
    gateway.emit(MAIN, 'session.event.text_delta', { text: ' Main continued after side close.' })
    await expect(main.locator('.conversation-live-edge')).toContainText('Main continued after side close.')
    await expect(composer(main)).toHaveText('Main draft survives closing the side')
    await expect(page.getByText('Side conversation is closed', { exact: true })).toHaveCount(0)
    expect(new URL(page.url()).searchParams.get('session')).toBe(MAIN)
    gateway.finish(MAIN)
    await expect(main.locator('.chat-stop-btn')).toHaveCount(0)
    if (modernSnapshots) {
      expect(gateway.consoleWarnings.filter(message => /Snapshot installation is no longer available|Snapshot invalidated by a newer delivery gap/.test(message))).toEqual([])
    }
    expect(gateway.errors).toEqual([])
  })

  test('uses the same approval UI with session-isolated resolution', async ({ page }) => {
    const gateway = await installGateway(page)
    const { main, side } = await openHosts(page)
    await send(main, 'Main approval')
    await send(side, 'Side approval')
    await expect.poll(() => gateway.requests.filter(request => request.method === 'chat.send').length).toBe(2)
    const approvals = [MAIN, SIDE].map((key, index) => ({ id: `approval-${index}`, namespace: 'exec',
      sessionKey: key, toolName: 'shell', command: 'echo approval-check',
      args: { command: 'echo approval-check' }, displayKind: 'run_command', displayTarget: 'echo approval-check' }))
    gateway.pendingApprovals(approvals)
    for (const approval of approvals) gateway.emit(approval.sessionKey, 'exec.approval.requested', {
      ...approval, approval_id: approval.id, tool_name: approval.toolName,
    })
    for (const host of [main, side]) {
      await expect(host.getByTestId('approval-card')).toHaveCount(1)
      await expect(host.getByTestId('approval-card')).toContainText('echo approval-check')
      await expect(host.getByRole('button', { name: 'Allow once', exact: true })).toBeVisible()
    }
    expect(await main.getByTestId('approval-card').textContent()).toBe(await side.getByTestId('approval-card').textContent())
    await captureReview(page, '02-main-side-approval')
    await side.getByRole('button', { name: 'Allow once', exact: true }).click()
    await expect.poll(() => gateway.approvalResolutions.length).toBe(1)
    expect(gateway.approvalResolutions[0]).toMatchObject({ id: 'approval-1', approved: true })
    await expect(side.getByTestId('approval-card')).toHaveCount(0)
    await expect(side.getByRole('button', { name: 'Allow once', exact: true })).toHaveCount(0)
    await expect(main.getByTestId('approval-card')).toHaveCount(1)
    expect(gateway.requests.filter(request => request.method === 'chat.send')).toHaveLength(2)
    expect(gateway.errors).toEqual([])
  })

  test('opens HTML and code links from both actual transcripts under their own read authority', async ({ page }) => {
    const gateway = await installGateway(page)
    const { main, side } = await openHosts(page)
    const text = '[HTML document](demo.html), [Code file](demo.ts) and [Markdown](report.md)'
    await send(main, 'Show files')
    await send(side, 'Show files')
    await expect.poll(() => gateway.requests.filter(request => request.method === 'chat.send').length).toBe(2)
    for (const key of [MAIN, SIDE]) {
      gateway.emit(key, 'session.event.text_delta', { text })
      gateway.finish(key, text)
    }
    for (const [host, key] of [[main, MAIN], [side, SIDE]] as const) {
      for (const [path, text] of [['demo.html', 'Parity HTML document'], ['demo.ts', 'export const parity'], ['report.md', 'export const parity']] as const) {
        await page.getByRole('tab', { name: 'Assistant chat', exact: true }).click()
        const link = host.getByRole('link', { name: `Open ${path}`, exact: true })
        await expect(link).toBeVisible()
        await link.click()
        const panel = page.getByTestId('workspace-file-panel')
        const authority = key === SIDE ? 'Side resource authority' : 'Main resource authority'
        const otherAuthority = key === SIDE ? 'Main resource authority' : 'Side resource authority'
        if (path === 'demo.html') {
          // Use the ordinary transcript link, not a canonical artifact card or
          // a reconstructed preview component. Both hosts must reach the same
          // interactive HTML renderer and retain their own read authority.
          const frame = panel.frameLocator('.resource-preview[data-preview-kind="html"] iframe')
          await expect(frame.getByRole('heading', { name: text })).toBeVisible()
          await expect(frame.locator('body')).toContainText(authority)
          await expect(frame.locator('body')).not.toContainText(otherAuthority)
          await expect(panel.locator('.resource-preview iframe')).toHaveAttribute('sandbox', 'allow-scripts')
          await frame.getByRole('button', { name: 'Try interaction', exact: true }).click()
          await expect(frame.getByRole('button', { name: 'Clicked once', exact: true })).toBeVisible()
          if (key === SIDE) await captureReview(page, '05-side-html-file-preview')
          await panel.getByRole('button', { name: 'Source', exact: true }).click()
          await expect(panel.locator('.workspace-file__source')).toContainText(text)
          await expect(panel.locator('.workspace-file__source')).toContainText(authority)
          await expect(panel.locator('.workspace-file__source')).not.toContainText(otherAuthority)
          await panel.getByRole('button', { name: 'Preview', exact: true }).click()
          await expect(frame.getByRole('heading', { name: text })).toBeVisible()
        } else {
          await expect(panel).toContainText(text)
          await expect(panel).toContainText(authority)
          await expect(panel).not.toContainText(otherAuthority)
        }
        expect(gateway.fileRequests.some(request => request.sessionKey === key && request.path === path && request.action === 'content')).toBe(true)
        expect(new URL(page.url()).searchParams.get('session')).toBe(MAIN)
        await expect(page.getByRole('tab', { name: 'Assistant chat', exact: true })).toBeVisible()
      }
    }
    expect(gateway.errors).toEqual([])
  })

  test('keeps prior settled sources and footers stable throughout a second running turn', async ({ page }) => {
    const gateway = await installGateway(page)
    const { main, side } = await openHosts(page)
    const tool = { type: 'tool_use', tool_use_id: 'first-source', name: 'web_fetch', input: { url: 'https://example.com/first' },
      result: JSON.stringify({ url: 'https://example.com/first', title: 'First source', content: 'First body' }), execution_status: { status: 'success' } }
    for (const host of [main, side]) await send(host, 'First research turn')
    await expect.poll(() => gateway.requests.filter(request => request.method === 'chat.send').length).toBe(2)
    for (const key of [MAIN, SIDE]) gateway.finish(key, 'First settled answer.', 'succeeded', [tool])
    const counts = []
    for (const host of [main, side]) {
      await expect(host.locator('.chat-stop-btn')).toHaveCount(0)
      await expect(host.locator('.sources-row')).toHaveCount(1)
      await expect(host.locator('.msg-ai-footer').last()).toBeVisible()
      counts.push(await host.locator('.msg-ai-footer').count())
      await send(host, 'Second research turn')
    }
    await expect.poll(() => gateway.requests.filter(request => request.method === 'chat.send').length).toBe(4)
    const secondTool = { ...tool, tool_use_id: 'second-source', input: { url: 'https://example.com/second' },
      result: JSON.stringify({ url: 'https://example.com/second', title: 'Second source', content: 'Second body' }) }
    for (const key of [MAIN, SIDE]) {
      gateway.emit(key, 'session.event.text_delta', { text: 'Second ongoing commentary.', presentation: 'intermediate' })
      gateway.emit(key, 'session.event.tool_use_start', { tool_use_id: secondTool.tool_use_id, tool_name: secondTool.name, input: secondTool.input })
      gateway.emit(key, 'session.event.tool_result', { ...secondTool, tool_name: secondTool.name })
      gateway.emit(key, 'session.event.text_delta', { text: 'Second answer still streaming.', presentation: 'answer' })
    }
    for (const [index, host] of [main, side].entries()) {
      await expect(host.locator('.conversation-live-edge')).toContainText('Second answer still streaming.')
      await expect(host.locator('.sources-row')).toHaveCount(1)
      await expect(host.locator('.msg-ai-footer')).toHaveCount(counts[index])
      await expect(host.locator('.chat-stop-btn')).toBeVisible()
    }
    for (const key of [MAIN, SIDE]) gateway.finish(key, 'Second answer completed.', 'succeeded', [secondTool])
    for (const [index, host] of [main, side].entries()) {
      await expect(host.locator('.sources-row')).toHaveCount(2)
      await expect(host.locator('.msg-ai-footer')).toHaveCount(counts[index]! + 1)
      await expect(host.locator('.chat-stop-btn')).toHaveCount(0)
    }
    expect(gateway.errors).toEqual([])
  })

  test('retains a failed denial for retry and isolates allow versus deny between actual hosts', async ({ page }) => {
    const gateway = await installGateway(page)
    const { main, side } = await openHosts(page)
    await send(main, 'Main approval')
    await send(side, 'Side approval')
    await expect.poll(() => gateway.requests.filter(request => request.method === 'chat.send').length).toBe(2)
    const approvals = [MAIN, SIDE].map((key, index) => ({ id: `denial-${index}`, namespace: 'exec', sessionKey: key,
      toolName: 'shell', command: 'echo approval-check', args: { command: 'echo approval-check' }, displayKind: 'run_command', displayTarget: 'echo approval-check' }))
    gateway.pendingApprovals(approvals)
    for (const approval of approvals) gateway.emit(approval.sessionKey, 'exec.approval.requested', { ...approval, approval_id: approval.id, tool_name: approval.toolName })
    gateway.failApproval('denial-1', true)
    await side.getByRole('button', { name: 'Deny', exact: true }).click()
    await expect(side.getByTestId('approval-card').getByRole('alert')).toBeVisible()
    await expect(side.getByRole('button', { name: 'Deny', exact: true })).toBeEnabled()
    await expect(main.getByTestId('approval-card')).toBeVisible()
    expect(gateway.approvalResolutions).toHaveLength(1)
    expect(gateway.approvalResolutions[0]).toMatchObject({ id: 'denial-1', approved: false })
    gateway.failApproval('denial-1', false)
    await side.getByRole('button', { name: 'Deny', exact: true }).click()
    await expect(side.getByTestId('approval-card')).toHaveCount(0)
    await expect(main.getByTestId('approval-card')).toBeVisible()
    await main.getByRole('button', { name: 'Allow once', exact: true }).click()
    await expect(main.getByTestId('approval-card')).toHaveCount(0)
    expect(gateway.approvalResolutions.map(item => ({ id: item.id, approved: item.approved }))).toEqual([
      { id: 'denial-1', approved: false }, { id: 'denial-1', approved: false }, { id: 'denial-0', approved: true },
    ])
    expect(gateway.requests.filter(request => request.method === 'chat.send')).toHaveLength(2)
    expect(gateway.requests.filter(request => request.method === 'chat.abort')).toHaveLength(0)
    expect(gateway.errors).toEqual([])
  })

  for (const accepted of [true, false]) test(`waits for clarification ACK and isolates ${accepted ? 'accepted' : 'expired'} side replies`, async ({ page }) => {
    const gateway = await installGateway(page)
    const { main, side } = await openHosts(page)
    await send(main, 'Main needs detail')
    await send(side, 'Side needs detail')
    await expect.poll(() => gateway.requests.filter(request => request.method === 'chat.send').length).toBe(2)
    gateway.requestInput(MAIN)
    const sideRequest = gateway.requestInput(SIDE)
    for (const host of [main, side]) await expect(host.getByTestId('clarify-card')).toBeVisible()
    const mainField = main.getByTestId('clarify-card').locator('.clarify-field__input')
    const sideField = side.getByTestId('clarify-card').locator('.clarify-field__input')
    expect(await mainField.getAttribute('aria-labelledby')).not.toBe(await sideField.getAttribute('aria-labelledby'))
    await sideField.fill('Side-only topic')
    await sideField.press('Escape')
    expect(gateway.requests.filter(request => request.method === 'chat.abort')).toHaveLength(0)
    await side.getByRole('button', { name: 'Send reply', exact: true }).click()
    await expect.poll(() => gateway.requests.filter(request => request.method === 'chat.clarify_submit').length).toBe(1)
    await expect(side.getByTestId('clarify-submit-status')).toBeVisible()
    await expect(side.getByTestId('clarify-outcome')).toHaveCount(0)
    await expect(mainField).toBeEnabled()
    await expect(mainField).toHaveValue('')
    const submission = gateway.requests.find(request => request.method === 'chat.clarify_submit')?.params
    expect(submission).toMatchObject({ sessionKey: SIDE, requestId: `clarify-${SIDE}`, fields: { topic: 'Side-only topic' } })
    gateway.answerInput(SIDE, accepted)
    await expect(side.getByTestId('clarify-outcome')).toContainText(accepted ? 'Reply received' : 'Question expired')
    await expect(side.getByTestId('clarify-card')).toHaveCount(0)
    gateway.emit(SIDE, 'session.event.tool_result', { tool_use_id: 'call-questionnaire', name: 'request_user_input', result: JSON.stringify(sideRequest) })
    gateway.emit(SIDE, 'session.event.text_delta', { text: 'Clarification replay boundary.' })
    await expect(side.locator('.conversation-live-edge')).toContainText('Clarification replay boundary.')
    await expect(side.getByTestId('clarify-card')).toHaveCount(0)
    await expect(main.getByTestId('clarify-card')).toBeVisible()
    await mainField.fill('Main-only topic')
    await main.getByRole('button', { name: 'Send reply', exact: true }).click()
    await expect.poll(() => gateway.requests.filter(request => request.method === 'chat.clarify_submit').length).toBe(2)
    gateway.answerInput(MAIN, true)
    await expect(main.getByTestId('clarify-outcome')).toContainText('Reply received')
    expect(gateway.requests.filter(request => request.method === 'chat.send')).toHaveLength(2)
    expect(gateway.errors).toEqual([])
  })

  test('shows child resource denial without falling back to main content and recovers on explicit reopen', async ({ page }) => {
    const gateway = await installGateway(page)
    const { main, side } = await openHosts(page)
    const text = '[Code file](denied.ts)'
    await send(main, 'Main resource')
    await send(side, 'Side resource')
    await expect.poll(() => gateway.requests.filter(request => request.method === 'chat.send').length).toBe(2)
    for (const key of [MAIN, SIDE]) gateway.finish(key, text)
    await main.getByRole('link', { name: 'Open denied.ts', exact: true }).click()
    await expect(page.getByTestId('workspace-file-panel')).toContainText('Main resource authority')
    await page.getByRole('button', { name: 'Close tab: denied.ts', exact: true }).click()
    gateway.denyFile(SIDE, 'denied.ts', true)
    const requestsBefore = gateway.fileRequests.length
    await side.getByRole('link', { name: 'Open denied.ts', exact: true }).click()
    await expect(page.getByTestId('workspace-file-panel').getByRole('alert')).toBeVisible()
    await captureReview(page, '06-side-resource-access-error')
    await expect(page.getByTestId('workspace-file-panel')).not.toContainText('Main resource authority')
    expect(gateway.fileRequests.slice(requestsBefore).filter(request => request.action === 'content').map(request => request.sessionKey)).toEqual([SIDE])
    expect(new URL(page.url()).searchParams.get('session')).toBe(MAIN)
    await page.getByRole('button', { name: 'Close tab: denied.ts', exact: true }).click()
    gateway.denyFile(SIDE, 'denied.ts', false)
    await side.getByRole('link', { name: 'Open denied.ts', exact: true }).click()
    await expect(page.getByTestId('workspace-file-panel')).toContainText('Side resource authority')
    await expect(page.getByTestId('workspace-file-panel').getByRole('alert')).toHaveCount(0)
    expect(gateway.requests.filter(request => request.method === 'sessions.sidechat.create')).toHaveLength(1)
    expect(gateway.errors).toEqual([])
  })

  test('does not open a child resource after its owning side tab closes during resolution', async ({ page }) => {
    const gateway = await installGateway(page)
    const { main, side } = await openHosts(page)
    await send(side, 'Resolve slowly')
    await expect.poll(() => gateway.requests.filter(request => request.method === 'chat.send').length).toBe(1)
    const artifact = gateway.registerDocument(SIDE, 'doc_delayed', 'delayed.html')
    gateway.finish(SIDE, 'A generated HTML file.', 'succeeded', [], {}, [artifact])
    gateway.deferResource(SIDE, 'doc_delayed')
    await side.getByRole('button', { name: 'Open delayed.html', exact: true }).click()
    await expect.poll(() => gateway.requests.filter(request => request.method === 'workbench.resources.get').length).toBe(1)
    await page.getByRole('button', { name: 'Close tab: Assistant chat', exact: true }).click()
    await expect(side).toHaveCount(0)
    gateway.releaseResource(SIDE, 'doc_delayed')
    // A new main turn provides an observed render/event boundary after the late
    // response, rather than accepting an immediate negative DOM assertion.
    await send(main, 'Main remains usable')
    await expect.poll(() => gateway.requests.filter(request => request.method === 'chat.send').length).toBe(2)
    gateway.emit(MAIN, 'session.event.text_delta', { text: 'Main continues after child close.' })
    await expect(main.locator('.conversation-live-edge')).toContainText('Main continues after child close.')
    await expect(page.getByRole('tab', { name: 'delayed.html', exact: true })).toHaveCount(0)
    await expect(page.getByTestId('workspace-file-panel')).toHaveCount(0)
    expect(gateway.requests.filter(request => request.method === 'workbench.resources.open')).toHaveLength(0)
    expect(gateway.errors).toEqual([])
  })

  test('registers a hidden side preview without stealing another conversation workbench', async ({ page }) => {
    const gateway = await installGateway(page)
    gateway.enableOtherSession()
    const { side } = await openHosts(page)
    await send(side, 'Generate a background HTML preview')
    await expect.poll(() => gateway.requests.filter(request => request.method === 'chat.send').length).toBe(1)
    gateway.registerDocument(SIDE, 'doc_background', 'background.html')
    gateway.deferResource(SIDE, 'doc_background')
    const result = { documentId: 'doc_background', resourceId: 'document:doc_background', entrypoint: '/task/background.html', previewStatus: 'ready' }
    gateway.emit(SIDE, 'session.event.tool_use_start', { tool_use_id: 'background-preview', tool_name: 'open_workspace_preview', input: { path: 'background.html' } })
    gateway.emit(SIDE, 'session.event.tool_result', { tool_use_id: 'background-preview', name: 'open_workspace_preview', result: JSON.stringify(result) })
    await expect.poll(() => gateway.requests.filter(request => request.method === 'workbench.resources.get').length).toBeGreaterThan(0)
    await page.getByRole('button', { name: 'Other parity conversation', exact: true }).click()
    await expect(page.locator('.topbar')).toContainText('Other parity conversation')
    await page.locator('#app-main').getByRole('link', { name: 'Open other.ts', exact: true }).click()
    await expect(page.getByRole('tab', { name: 'other.ts', exact: true })).toHaveAttribute('aria-selected', 'true')
    await expect(page.getByTestId('workspace-file-panel')).toContainText('Main resource authority')
    const panelText = await page.getByTestId('workspace-file-panel').textContent()
    gateway.releaseResource(SIDE, 'doc_background')
    await expect.poll(() => gateway.requests.filter(request => request.method === 'workbench.resources.open').length).toBe(1)
    // A foreground turn confirms that the background open response has reached
    // the live app before checking the selected panel and its contents.
    const other = page.locator('#app-main .chat').first()
    await send(other, 'Continue in the other conversation')
    await expect.poll(() => gateway.requests.filter(request => request.method === 'chat.send').length).toBe(2)
    gateway.emit(OTHER, 'session.event.text_delta', { text: 'Other conversation stays in control.' })
    await expect(other.locator('.conversation-live-edge')).toContainText('Other conversation stays in control.')
    await expect(page.getByRole('tab', { name: 'other.ts', exact: true })).toHaveAttribute('aria-selected', 'true')
    await expect(page.getByTestId('workspace-file-panel')).toHaveText(panelText!)
    await expect(page.getByRole('tab', { name: 'background.html', exact: true })).toHaveCount(0)
    await page.getByRole('button', { name: 'Main parity conversation', exact: true }).click()
    await expect(page.getByRole('tab', { name: 'background.html', exact: true })).toBeVisible()
    await page.getByRole('tab', { name: 'background.html', exact: true }).click()
    await expect(page.frameLocator('.artifact-preview__frame--html').getByRole('heading', { name: 'Canonical child preview', exact: true })).toBeVisible()
    expect(gateway.requests.filter(request => request.method === 'sessions.sidechat.close')).toHaveLength(0)
    expect(gateway.errors).toEqual([])
  })

  test('navigates session references through the main route without replacing the side child identity', async ({ page }) => {
    const gateway = await installGateway(page)
    gateway.enableOtherSession()
    const { main, side } = await openHosts(page)
    for (const host of [main, side]) await send(host, 'Find the other conversation')
    await expect.poll(() => gateway.requests.filter(request => request.method === 'chat.send').length).toBe(2)
    const reference = { version: 1, kind: 'session', id: OTHER, label: 'Other parity conversation',
      scope: { sessionKey: OTHER }, state: { available: true, runStatus: null }, capabilities: { open: true, copy: true } }
    const tool = { type: 'tool_use', tool_use_id: 'find-session', name: 'session_search', input: { query: 'other' },
      result: JSON.stringify({ results: [{ reference }] }), execution_status: { status: 'success' } }
    for (const key of [MAIN, SIDE]) gateway.finish(key, 'This is the related conversation.', 'succeeded', [tool])
    await composer(side).fill('Side draft survives reference navigation')
    for (const host of [main, side]) {
      const card = host.getByTestId('session-reference-card')
      await expect(card).toBeEnabled()
      await card.click()
      await expect.poll(() => new URL(page.url()).searchParams.get('session')).toBe(OTHER)
      await expect(page.locator('.topbar')).toContainText('Other parity conversation')
      await expect(side).not.toBeVisible()
      await page.getByRole('button', { name: 'Main parity conversation', exact: true }).click()
      await expect.poll(() => new URL(page.url()).searchParams.get('session')).toBe(MAIN)
      await expect(side.getByTestId('session-reference-card')).toBeVisible()
      await expect(composer(side)).toHaveText('Side draft survives reference navigation')
    }
    await side.getByRole('button', { name: 'Send', exact: true }).click()
    await expect.poll(() => gateway.requests.filter(request => request.method === 'chat.send').length).toBe(3)
    expect(gateway.requests.filter(request => request.method === 'chat.send').at(-1)?.params?.sessionKey).toBe(SIDE)
    expect(gateway.requests.filter(request => request.method === 'sessions.sidechat.create')).toHaveLength(1)
    expect(gateway.requests.filter(request => request.method === 'sessions.sidechat.close')).toHaveLength(0)
    expect(gateway.errors).toEqual([])
  })

  for (const kind of ['workspace-reference', 'historical-tool-file'] as const) test(`opens ${kind} from both transcripts with parent panel ownership and child read authority`, async ({ page }) => {
    const gateway = await installGateway(page)
    const { main, side } = await openHosts(page)
    for (const host of [main, side]) await send(host, 'Inspect a source file')
    await expect.poll(() => gateway.requests.filter(request => request.method === 'chat.send').length).toBe(2)
    for (const key of [MAIN, SIDE]) {
      const reference = { version: 1, kind: 'workspace_file', id: 'reference.ts', label: 'reference.ts:1',
        scope: { sessionKey: key, workspaceId: 'workspace' }, locator: { relativePath: 'reference.ts', startLine: 1, endLine: 1 },
        state: { available: true, revision: 'file_1234567890abcdef' }, capabilities: { open: true, copy: true } }
      const tool = kind === 'workspace-reference'
        ? { type: 'tool_use', tool_use_id: 'read-reference', name: 'read_source', input: { path: 'reference.ts' },
            result: JSON.stringify({ status: 'success', reference }), execution_status: { status: 'success' } }
        : { type: 'tool_use', tool_use_id: 'write-history', name: 'write_file',
            input: { path: 'history.ts', content: key === SIDE ? 'const sideHistory = 1;' : 'const mainHistory = 1;' },
            result: 'File written successfully.', execution_status: { status: 'success' } }
      const { result, execution_status, ...start } = tool
      const complete = { type: 'tool_result', tool_use_id: tool.tool_use_id, name: tool.name, result, execution_status,
        ...(kind === 'historical-tool-file' ? { file_snapshot: { kind: 'write', path: 'history.ts',
          content: key === SIDE ? 'const sideHistory = 1;' : 'const mainHistory = 1;' } } : {}) }
      gateway.emit(key, 'session.event.tool_use_start', { tool_use_id: tool.tool_use_id, tool_name: tool.name, input: tool.input })
      gateway.emit(key, 'session.event.tool_result', complete)
      gateway.finish(key, 'Source inspection complete.', 'succeeded', [start, complete])
    }
    for (const [host, key] of [[main, MAIN], [side, SIDE]] as const) {
      await page.getByRole('tab', { name: 'Assistant chat', exact: true }).click()
      if (kind === 'workspace-reference') {
        await host.getByTestId('workspace-reference-card').locator('.workspace-reference__open').click()
        await expect(page.getByTestId('workspace-file-panel')).toContainText(key === SIDE ? 'const sideReference = 1;' : 'const mainReference = 1;')
        expect(gateway.requests.filter(request => request.method === 'workspaces.references.read').at(-1)?.params?.sessionKey).toBe(key)
      } else {
        await host.getByRole('button', { name: /^Time spent/ }).click()
        await host.locator('.tool-row[aria-expanded="false"]').click()
        await host.getByTestId('activity-code-open').click()
        await expect(page.getByTestId('historical-tool-file-panel')).toContainText(key === SIDE ? 'const sideHistory = 1;' : 'const mainHistory = 1;')
      }
      expect(new URL(page.url()).searchParams.get('session')).toBe(MAIN)
      await expect(page.getByRole('tab', { name: 'Assistant chat', exact: true })).toBeVisible()
      await captureReview(page, `${kind}-${key === SIDE ? 'side' : 'main'}`)
      // A parent-owned file remains part of this workbench after returning to
      // the assistant tab; it must not disappear in a hidden child scope.
      const fileTab = page.getByRole('tab', { name: kind === 'workspace-reference' ? 'reference.ts' : 'history.ts', exact: true })
      await expect(fileTab).toHaveAttribute('aria-selected', 'true')
      await page.getByRole('tab', { name: 'Assistant chat', exact: true }).click()
      await expect(fileTab).toBeVisible()
      await fileTab.click()
      await expect(fileTab).toHaveAttribute('aria-selected', 'true')
      await page.getByRole('button', { name: `Close tab: ${kind === 'workspace-reference' ? 'reference.ts' : 'history.ts'}`, exact: true }).click()
    }
    expect(gateway.errors).toEqual([])
  })

  test('pastes Chinese text through the focused side editor without changing the main draft or duplicating delivery', async ({ page, context }) => {
    const gateway = await installGateway(page)
    const { main, side } = await openHosts(page)
    await context.grantPermissions(['clipboard-read', 'clipboard-write'])
    const prompt = [
      '请先检查主会话的上下文，再说明这份 HTML 产物的布局、交互和下一步修改建议。不要立即修改文件。',
      '这次需要验证主会话和辅助对话同时打开时的完整交互。请保留主会话已有的草稿和执行状态，辅助对话中的输入、澄清和审批应当只属于当前辅助会话。',
      '检查页面标题、代码引用、可预览文件和生成产物的操作是否一致；说明哪些结果已经由实际执行验证，哪些仍然需要后续测试。',
      '如果需要进一步信息，先提出一个明确的问题并等待回复。完成分析后给出简洁结论，不要重复这段输入，不要创建额外任务。',
    ].join('\n\n')
    const mainDraft = '主会话独立草稿：请保留此内容。'
    // Use the browser clipboard and native keyboard paste, not locator.fill or
    // a dispatched synthetic input: document capture and the inline editor's
    // actual paste/selection/execCommand handlers all participate.
    for (const expectedMain of ['', mainDraft]) {
      await composer(main).fill(expectedMain)
      await composer(side).fill('')
      await page.evaluate(text => navigator.clipboard.writeText(text), prompt)
      await composer(side).click()
      await page.keyboard.press('Control+V')
      await expect(composer(side)).toHaveText(prompt, { useInnerText: true })
      await expect(composer(main)).toHaveText(expectedMain)
      await expect(composer(side)).toBeFocused()
    }
    await side.getByRole('button', { name: 'Send', exact: true }).click()
    await expect.poll(() => gateway.requests.filter(request => request.method === 'chat.send').length).toBe(1)
    expect(gateway.requests.find(request => request.method === 'chat.send')?.params).toMatchObject({ sessionKey: SIDE, message: prompt })
    gateway.finish(SIDE, '已检查这份产物。')
    await expect(side).toContainText('已检查这份产物。')
    await expect(composer(side)).toHaveText('')
    await expect(composer(main)).toHaveText(mainDraft)
    expect(gateway.requests.filter(request => request.method === 'chat.send')).toHaveLength(1)
    expect(gateway.requests.filter(request => request.method === 'sessions.sidechat.create')).toHaveLength(1)
    expect(gateway.errors).toEqual([])
  })

  test('accepts events before send ACK and cancelling side leaves the main execution running', async ({ page }) => {
    const gateway = await installGateway(page, true)
    const { main, side } = await openHosts(page)
    await send(main, 'Main keeps running')
    await send(side, 'Side can stop')
    await expect.poll(() => gateway.requests.filter(request => request.method === 'chat.send').length).toBe(2)
    for (const key of [MAIN, SIDE]) gateway.emit(key, 'session.event.text_delta', { text: 'Working before acknowledgement.' })
    gateway.releaseAcks()
    for (const host of [main, side]) await expect(host.locator('.conversation-live-edge')).toContainText('Working before acknowledgement.')
    await side.locator('.chat-stop-btn').click()
    await expect.poll(() => gateway.requests.filter(request => request.method === 'chat.abort').length).toBe(1)
    expect(gateway.requests.find(request => request.method === 'chat.abort')?.params?.sessionKey).toBe(SIDE)
    await expect(side.locator('.chat-stop-btn')).toHaveCount(0)
    await expect(main.locator('.chat-stop-btn')).toBeVisible()
    await expect(main.locator('.conversation-live-edge')).toContainText('Working before acknowledgement.')
    expect(new URL(page.url()).searchParams.get('session')).toBe(MAIN)
    gateway.finish(MAIN)
    await expect(main.locator('.chat-stop-btn')).toHaveCount(0)
    expect(gateway.errors).toEqual([])
  })
})
