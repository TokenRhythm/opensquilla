import { expect, test, _electron as electron, type ElectronApplication, type Locator, type Page } from '@playwright/test'
import { mkdtemp, mkdir, rm, writeFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import { tmpdir } from 'node:os'
import { basename, dirname, resolve, join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { MAIN, SIDE, installGateway } from './support/real-conversation-gateway'

interface MenuObservation {
  count: number
  roles: string[]
  selection: string
  editable: boolean
  frameId: number | null
  focusedFrameId: number | null
  ownerMatches: boolean
}

declare global {
  var __textContextMenuTest: {
    snapshot(): MenuObservation
    copy(): void
    clipboard(): { text: string; html: string }
  }
}

// The dedicated config serializes this OS clipboard proof. It renders the
// real Vue application; only Gateway data and the Electron bootstrap are fixtures.
test.describe.configure({ mode: 'serial' })

const MAIN_TEXT = 'Main synthetic selected passage.'
const SIDE_TEXT = 'Side synthetic selected passage.'
const MARKDOWN_TEXT = 'Markdown synthetic selected passage.'
const MARKDOWN = `# Synthetic preview\n\n**${MARKDOWN_TEXT}**\n`

async function removeIsolatedShell(isolation: string): Promise<void> {
  if (dirname(resolve(isolation)) !== resolve(tmpdir()) || !basename(isolation).startsWith('opensquilla-text-menu-')) {
    throw new Error('Refusing to remove a fixture outside its temporary directory')
  }
  await rm(isolation, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 })
}

async function launchShell(outputDirectory: string): Promise<{ app: ElectronApplication; isolation: string }> {
  const repo = resolve(import.meta.dirname, '../..')
  const desktop = resolve(repo, 'desktop/electron')
  const requireDesktop = createRequire(resolve(desktop, 'package.json'))
  const isolation = await mkdtemp(join(tmpdir(), 'opensquilla-text-menu-'))
  await mkdir(outputDirectory, { recursive: true })
  const fixture = join(isolation, 'main.mjs')
  await writeFile(fixture, `
import { app, BrowserWindow, Menu, clipboard, webContents } from 'electron';
import { installTextContextMenu } from ${JSON.stringify(pathToFileURL(resolve(desktop, 'dist/text-context-menu.js')).href)};
app.setPath('userData', ${JSON.stringify(join(isolation, 'profile'))});
app.whenReady().then(async () => {
let latestMenu;
let latestParams;
let observation = { count: 0, roles: [], selection: '', editable: false,
  frameId: null, focusedFrameId: null, ownerMatches: false };
// Native popups are outside Playwright's DOM; capture the menu and invoke its Copy role.
Menu.prototype.popup = function(options) {
  latestMenu = this;
  const contents = window.webContents;
  observation = { count: observation.count + 1,
    roles: this.items.map(item => item.role || item.type),
    selection: latestParams.selectionText, editable: latestParams.isEditable,
    frameId: options.frame?.frameTreeNodeId ?? null,
    focusedFrameId: contents.focusedFrame?.frameTreeNodeId ?? null,
    ownerMatches: options.window === window && webContents.getFocusedWebContents() === contents };
};
globalThis.__textContextMenuTest = {
  snapshot: () => observation,
  copy: () => {
    const item = latestMenu?.items.find(item => item.role === 'copy');
    if (!item?.enabled) throw new Error('Native Copy action is absent or disabled');
    clipboard.writeText('Synthetic clipboard sentinel.');
    if (process.platform === 'darwin') Menu.sendActionToFirstResponder('copy:');
    else item.click({}, BrowserWindow.getFocusedWindow(), webContents.getFocusedWebContents());
  },
  clipboard: () => ({ text: clipboard.readText(), html: clipboard.readHTML() }),
};
const window = new BrowserWindow({ width: 1680, height: 1080, show: true,
  webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true } });
window.webContents.on('context-menu', (_event, params) => { latestParams = params; });
installTextContextMenu(window.webContents);
await window.loadURL('about:blank');
});
`)
  const env = Object.fromEntries(Object.entries(process.env)
    .filter(([name, value]) => value !== undefined && !/(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)/i.test(name))) as Record<string, string>
  try {
    const app = await electron.launch({ executablePath: requireDesktop('electron'), args: [
      ...(process.platform === 'darwin' ? ['--use-mock-keychain'] : []),
      `--user-data-dir=${join(isolation, 'profile')}`, fixture,
    ], env, timeout: 60_000 })
    return { app, isolation }
  } catch (error) {
    await removeIsolatedShell(isolation)
    throw error
  }
}

async function copySelectedText(app: ElectronApplication, page: Page, target: Locator, expected: string) {
  await expect(target).toBeVisible()
  await target.scrollIntoViewIfNeeded()
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0]!.focus())
  await page.bringToFront()
  await expect.poll(() => app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0]!.isFocused())).toBe(true)
  // Read geometry only. Selection itself comes from real pointer input.
  const points = await target.evaluate(element => {
    const range = document.createRange()
    range.selectNodeContents(element)
    const rects = Array.from(range.getClientRects()).filter(rect => rect.width && rect.height)
    const first = rects[0]!, last = rects.at(-1)!
    return { start: { x: first.left, y: first.top + first.height / 2 },
      end: { x: last.right + 1, y: last.top + last.height / 2 },
      menu: { x: first.left + Math.min(20, first.width / 2), y: first.top + first.height / 2 } }
  })
  await page.mouse.move(points.start.x, points.start.y)
  await page.mouse.down()
  await page.mouse.move(points.end.x, points.end.y, { steps: 12 })
  await page.mouse.up()
  await expect.poll(() => page.evaluate(() => window.getSelection()?.toString().trim())).toBe(expected.trim())
  const before = await app.evaluate(() => globalThis.__textContextMenuTest.snapshot().count)
  await page.mouse.click(points.menu.x, points.menu.y, { button: 'right' })
  await expect.poll(() => app.evaluate(() => globalThis.__textContextMenuTest.snapshot().count)).toBe(before + 1)
  const menu = await app.evaluate(() => globalThis.__textContextMenuTest.snapshot())
  expect(menu.roles).toEqual(['copy'])
  expect(menu.selection.replace(/\r\n/g, '\n').trim()).toBe(expected.trim())
  expect(menu.editable).toBe(false)
  expect(menu.ownerMatches).toBe(true)
  expect(menu.frameId).not.toBeNull()
  expect(menu.focusedFrameId).toBe(menu.frameId)
  await app.evaluate(() => globalThis.__textContextMenuTest.copy())
  await expect.poll(async () => (await app.evaluate(() => globalThis.__textContextMenuTest.clipboard()))
    .text.replace(/\r\n/g, '\n').trim()).toBe(expected.trim())
  const copied = await app.evaluate(() => globalThis.__textContextMenuTest.clipboard())
  expect(copied.html).not.toBe('')
  return menu
}

test('real main chat, assistant chat and Markdown/source preview copy through Electron text menus', async ({}, testInfo) => {
  test.setTimeout(120_000)
  const { app, isolation } = await launchShell(testInfo.outputDir)
  const evidence: Array<{ surface: string; menu: MenuObservation }> = []
  try {
    const page = await app.firstWindow()
    await page.setViewportSize({ width: 1660, height: 1000 })
    const gateway = await installGateway(page, false, false, false, false, { workspace: true })
    await page.route('**/control/static/dist/opensquilla-mark.png', route => route.fulfill({
      contentType: 'image/png', path: resolve(import.meta.dirname, '../public-assets/opensquilla-mark.png'),
    }))
    gateway.appendHistory(MAIN, { role: 'assistant', id: 'copy-main-answer',
      text: `**${MAIN_TEXT}**\n\n[Preview](report.md)` })
    await page.route('**/api/v1/workspace-files/content?**', route => route.fulfill({
      contentType: 'text/markdown', body: MARKDOWN,
    }))
    await page.addInitScript(() => {
      window.OPENSQUILLA_FEATURES = { ...(window.OPENSQUILLA_FEATURES || {}), artifactWorkbench: true }
    })
    const baseURL = process.env.OPENSQUILLA_WEBUI_BASE_URL || String(testInfo.project.use.baseURL || 'http://127.0.0.1:18791')
    await page.goto(new URL(`/control/chat?session=${encodeURIComponent(MAIN)}`, baseURL).href)
    const main = page.locator('#app-main .chat').first()
    await expect(main).toContainText(MAIN_TEXT)
    evidence.push({ surface: 'main-assistant', menu: await copySelectedText(app, page, main.getByText(MAIN_TEXT, { exact: true }), MAIN_TEXT) })
    evidence.push({ surface: 'main-user', menu: await copySelectedText(app, page, main.locator('.msg-user-bubble').filter({ hasText: 'Earlier main question' }), 'Earlier main question') })

    await page.getByTestId('topbar-workbench-toggle').click()
    await page.getByRole('button', { name: 'Assistant chat', exact: true }).click()
    const side = page.getByTestId('assistant-chat-panel')
    await side.getByRole('textbox', { name: 'Message to send', exact: true }).fill('Synthetic side question')
    await side.getByRole('button', { name: 'Send', exact: true }).click()
    await expect.poll(() => gateway.requests.filter(request => request.method === 'chat.send'
      && request.params?.sessionKey === SIDE).length).toBe(1)
    gateway.finish(SIDE, `**${SIDE_TEXT}**`)
    await expect(side).toContainText(SIDE_TEXT)
    evidence.push({ surface: 'side-assistant', menu: await copySelectedText(app, page, side.getByText(SIDE_TEXT, { exact: true }), SIDE_TEXT) })

    await main.getByRole('link', { name: 'Open report.md', exact: true }).click()
    const panel = page.getByTestId('workspace-file-panel')
    await expect(panel).toContainText(MARKDOWN_TEXT)
    evidence.push({ surface: 'markdown-preview', menu: await copySelectedText(app, page, panel.getByText(MARKDOWN_TEXT, { exact: true }), MARKDOWN_TEXT) })
    await panel.getByRole('button', { name: 'Source', exact: true }).click()
    const source = panel.locator('.workspace-file__source')
    await expect(source).toContainText(MARKDOWN_TEXT)
    evidence.push({ surface: 'markdown-source', menu: await copySelectedText(app, page,
      source.locator('.workspace-file__text').filter({ hasText: MARKDOWN_TEXT }), `**${MARKDOWN_TEXT}**`) })
    expect(gateway.errors).toEqual([])
    await page.screenshot({ path: testInfo.outputPath('real-webui-copy.png'), fullPage: true })
    await testInfo.attach('real-webui-copy', { path: testInfo.outputPath('real-webui-copy.png'), contentType: 'image/png' })
  } finally {
    const evidencePath = testInfo.outputPath('native-menu-evidence.json')
    await writeFile(evidencePath, JSON.stringify(evidence, null, 2))
    await testInfo.attach('native-menu-evidence', { path: evidencePath, contentType: 'application/json' })
    try { await app.close() } finally { await removeIsolatedShell(isolation) }
  }
})
