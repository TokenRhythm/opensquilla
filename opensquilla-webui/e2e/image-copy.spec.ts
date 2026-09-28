import { test, expect, type Locator, type Page } from '@playwright/test'
import { helloOkResponse } from './support/gateway-fixture'
import {
  chatHistoryPayload,
  sessionMessagesHydratePayload,
  sessionMessagesSnapshotPayload,
  sessionMessagesSubscribePayload,
} from './support/session-read-fixtures'

const SESSION_KEY = 'agent:main:webchat:e2eimagecopy'
// Synthetic RGBA fixture: a 12×8 image with an opaque red left half and a
// transparent right half. Its 1×1 green thumbnail deliberately differs.
const ORIGINAL_PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAwAAAAICAYAAADN5B7xAAAAFklEQVR4nGP4z8DwHxtmwAVGNdBCAwC4C1+hkVaFpQAAAABJRU5ErkJggg==',
  'base64',
)
const THUMBNAIL_PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNg+M/wHwAEAQH/cetH5QAAAABJRU5ErkJggg==',
  'base64',
)
const SVG_SOURCE = '<?xml version="1.0" encoding="UTF-8"?>\n'
  + '<!-- Synthetic source; preserve whitespace and Unicode: 图形 -->\n'
  + '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="8" viewBox="0 0 12 8">\n'
  + '  <rect width="6" height="8" fill="#ff0000"/>\n</svg>\n'
// Same synthetic image encoded as JPEG (opaque black right half) and lossless WebP.
const ORIGINAL_JPEG = Buffer.from(
  '/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQH/2wBDAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQH/wAARCAAIAAwDAREAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAACQr/xAAaEAABBQEAAAAAAAAAAAAAAAAABgcIOIa2/8QAFQEBAQAAAAAAAAAAAAAAAAAACQr/xAAeEQABAQkAAAAAAAAAAAAAAAAABwUGCTc4hYe2t//aAAwDAQACEQMRAD8AnHmHXJxcj3SYD9RGaDsXrXmsV9RRqFFyxn2FPgNxAiQU/9k=',
  'base64',
)
const ORIGINAL_WEBP = Buffer.from('UklGRjYAAABXRUJQVlA4WAoAAAAQAAAACwAABwAAVlA4TBgAAAAvC8ABEA8Q8x/zHwyBQJLZ/mQrRPQ/MgA=', 'base64')
const ATTACHMENT_HASH = 'c'.repeat(64)

interface FixtureOptions {
  body?: string | Buffer
  mime?: string
  name?: string
  attachment?: 'inline' | 'staged'
}

interface ClipboardPaste {
  text: string
  files: Array<{ type: string; width: number; height: number; left: number[]; right: number[] }>
  error?: string
}

declare global {
  interface Window {
    __imageCopyPaste?: ClipboardPaste
    __imageCopyScriptExecuted?: boolean
  }
}

async function openFixture(page: Page, options: FixtureOptions = {}) {
  const body = options.body ?? ORIGINAL_PNG
  const mime = options.mime ?? 'image/png'
  const name = options.name ?? 'original.png'
  const originals: string[] = []
  const thumbnails: string[] = []
  await page.route('**/opensquilla-mark.png', route => route.fulfill({ contentType: 'image/png', body: THUMBNAIL_PNG }))
  await page.route('**/api/system/update', route => route.fulfill({ json: {} }))
  await page.route('**/api/elevated-mode', route => route.fulfill({ json: { enabled: false } }))
  await page.route('**/api/approvals', route => route.fulfill({
    json: { pending: [], mode: 'prompt', allowPatterns: [], denyPatterns: [] },
  }))
  await page.route('**/api/v1/artifacts/**', route => {
    const url = new URL(route.request().url())
    if (url.searchParams.get('variant') === 'thumb') {
      thumbnails.push(url.toString())
      return route.fulfill({ contentType: 'image/png', body: THUMBNAIL_PNG })
    }
    originals.push(url.toString())
    return route.fulfill({ contentType: mime, body })
  })
  await page.route('**/api/v1/attachments/**', route => {
    originals.push(route.request().url())
    expect(route.request().headers()['x-opensquilla-session-key']).toBe(SESSION_KEY)
    return route.fulfill({ contentType: mime, body })
  })
  await page.routeWebSocket(/\/ws$/, ws => {
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    ws.onMessage(message => {
      const frame = JSON.parse(String(message))
      if (frame.type !== 'req') return
      if (frame.method === 'connect') {
        ws.send(helloOkResponse({ auth: { principal: {
          isOwner: true, authenticated: true, authState: 'authenticated',
          role: 'operator', scopes: ['operator.read', 'operator.write'],
          capabilities: ['chat.read', 'chat.write'],
        } } }))
        return
      }
      const key = String(frame.params?.key || frame.params?.sessionKey || SESSION_KEY)
      const attachment = options.attachment === 'staged'
        ? { name, mime, size: Buffer.byteLength(body), sha256_ref: ATTACHMENT_HASH,
          download_url: `/api/v1/attachments/${ATTACHMENT_HASH}` }
        : { name, type: mime, data: Buffer.from(body).toString('base64') }
      const artifact = {
        id: 'image-copy-original', name, mime, size: Buffer.byteLength(body),
        download_url: '/api/v1/artifacts/image-copy-original',
        thumbnail_url: '/api/v1/artifacts/image-copy-original?variant=thumb',
      }
      const payloads: Record<string, unknown> = {
        'chat.history': chatHistoryPayload([
          { role: 'user', id: 'copy-user', text: 'Synthetic image copy fixture.', timestamp: 1_800_000_000,
            ...(options.attachment ? { attachments: [attachment] } : {}) },
          { role: 'assistant', id: 'copy-assistant', text: 'Synthetic image is ready.', timestamp: 1_800_000_001,
            ...(!options.attachment ? { artifacts: [artifact] } : {}) },
        ]),
        'agents.list': { agents: [] },
        'commands.list_for_surface': { commands: [] },
        'config.get': { squilla_router: { enabled: false, rollout_phase: 'observe', tiers: {} }, permissions: {}, skills: {} },
        'onboarding.status': { audioConfigured: false },
        'sandbox.run_mode.preference.get': { runMode: 'full', source: 'config' },
        'sessions.list': { sessions: [], count: 0, ts: 1_800_000_000, has_more: false },
        'sessions.messages.subscribe': sessionMessagesSubscribePayload(key),
        'sessions.messages.snapshot': sessionMessagesSnapshotPayload(key),
        'sessions.messages.hydrate': sessionMessagesHydratePayload(key),
        'usage.status': { sessions: [] },
      }
      ws.send(JSON.stringify({ type: 'res', id: frame.id, ok: true, payload: payloads[frame.method] ?? {} }))
    })
  })
  if (process.env.OPENSQUILLA_PLAYWRIGHT_MANAGE_WEBUI === 'preview') {
    await page.route('**/control/chat?*', async route => {
      if (!route.request().isNavigationRequest()) return route.fallback()
      const response = await route.fetch()
      const body = (await response.text()).replace(/(src|href)="\.\//g, '$1="/control/')
      await route.fulfill({ response, body })
    })
  }
  await page.goto(`/control/chat?session=${encodeURIComponent(SESSION_KEY)}`)
  await expect(page.locator('.chat-header')).toBeVisible()
  const resource = page.locator(options.attachment ? '.msg-file-resource'
    : mime.startsWith('image/') ? '.msg-media-card' : '.msg-artifact-chip')
  await expect(resource).toBeVisible()
  return { resource, originals, thumbnails }
}

// Native paste checks the output of the actual Clipboard API write, including
// WebKit's user activation rules. Call only after our synthetic copy succeeded;
// this helper must never inspect pre-existing user clipboard content.
async function pasteSyntheticClipboard(page: Page): Promise<ClipboardPaste> {
  await page.evaluate(() => {
    delete window.__imageCopyPaste
    document.querySelector('[data-testid="clipboard-paste-probe"]')?.remove()
    const target = document.createElement('div')
    target.contentEditable = 'true'
    target.dataset.testid = 'clipboard-paste-probe'
    target.setAttribute('aria-label', 'Synthetic clipboard paste probe')
    target.style.cssText = 'position:fixed;inset:0 auto auto 0;width:80px;height:40px;z-index:2147483647'
    target.addEventListener('paste', event => {
      event.preventDefault()
      event.stopImmediatePropagation()
      const text = event.clipboardData?.getData('text/plain') ?? ''
      const files = Array.from(event.clipboardData?.files ?? [])
      void Promise.all(files.map(async file => {
        const image = new Image()
        const url = URL.createObjectURL(file)
        try {
          image.src = url
          await image.decode()
          const canvas = document.createElement('canvas')
          canvas.width = image.naturalWidth
          canvas.height = image.naturalHeight
          const context = canvas.getContext('2d')!
          context.drawImage(image, 0, 0)
          return { type: file.type, width: canvas.width, height: canvas.height,
            left: Array.from(context.getImageData(1, 1, 1, 1).data),
            right: Array.from(context.getImageData(canvas.width - 1, 1, 1, 1).data) }
        } finally {
          URL.revokeObjectURL(url)
        }
      })).then(files => { window.__imageCopyPaste = { text, files } })
        .catch(error => { window.__imageCopyPaste = { text, files: [], error: String(error) } })
    })
    document.body.append(target)
    target.focus()
  })
  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+V' : 'Control+V')
  await expect.poll(() => page.evaluate(() => window.__imageCopyPaste)).toBeTruthy()
  const result = await page.evaluate(() => window.__imageCopyPaste!)
  await page.getByTestId('clipboard-paste-probe').evaluate(element => element.remove())
  expect(result.error).toBeUndefined()
  return result
}

async function copyImage(page: Page, resource: Locator) {
  await resource.getByTestId('copy-image').click()
  await expect(resource.locator('.image-copy-actions')).toHaveAttribute('data-state', 'ok')
  const pasted = await pasteSyntheticClipboard(page)
  expect(pasted.files).toEqual([{ type: 'image/png', width: 12, height: 8,
    left: [255, 0, 0, 255], right: [0, 0, 0, 0] }])
}

test.describe('Image copy through the system clipboard', () => {
  // The dedicated config uses one worker for the shared OS clipboard.

  test('generated card copies original PNG pixels instead of its thumbnail', async ({ page }) => {
    const fixture = await openFixture(page)
    await expect(fixture.resource.locator('.msg-media-card__img img')).toBeVisible()
    expect(fixture.thumbnails).toHaveLength(1)
    expect(fixture.originals).toHaveLength(0)
    await copyImage(page, fixture.resource)
    expect(fixture.originals).toHaveLength(1)
  })

  for (const format of ['jpeg', 'webp'] as const) {
    test(`${format.toUpperCase()} original is converted to a PNG at its original dimensions`, async ({ page }) => {
      const { resource } = await openFixture(page, { body: format === 'jpeg' ? ORIGINAL_JPEG : ORIGINAL_WEBP,
        name: `original.${format}`, mime: `image/${format}` })
      await resource.getByTestId('copy-image').click()
      await expect(resource.locator('.image-copy-actions')).toHaveAttribute('data-state', 'ok')
      const pasted = await pasteSyntheticClipboard(page)
      expect(pasted.files).toHaveLength(1)
      expect(pasted.files[0]).toMatchObject({ type: 'image/png', width: 12, height: 8 })
      // JPEG is lossy and has no alpha channel; do not assert exact RGB rounding.
      expect(pasted.files[0].left[0]).toBeGreaterThan(250)
      expect(pasted.files[0].left[1]).toBeLessThan(5)
      expect(pasted.files[0].left[2]).toBeLessThan(5)
      expect(pasted.files[0].left[3]).toBe(255)
      expect(pasted.files[0].right).toEqual(format === 'jpeg' ? [0, 0, 0, 255] : [0, 0, 0, 0])
    })
  }

  test('shared large preview copies original PNG with transparency', async ({ page }) => {
    const { resource } = await openFixture(page)
    await resource.locator('.msg-media-card__img').click()
    const lightbox = page.locator('.deliv-preview')
    await expect(lightbox).toBeVisible()
    await copyImage(page, lightbox)
    await page.keyboard.press('Escape')
    await expect(lightbox).toHaveCount(0)
    await expect(resource.locator('.msg-media-card__img')).toBeFocused()
  })

  test('generated SVG copies a transparent PNG and exact UTF-8 source', async ({ page }, testInfo) => {
    const { resource } = await openFixture(page, { body: SVG_SOURCE, name: 'drawing.svg', mime: 'image/svg+xml' })
    await copyImage(page, resource)
    await resource.getByTestId('image-copy-more').click()
    await page.screenshot({ path: testInfo.outputPath('svg-copy-menu.png') })
    await page.getByTestId('copy-svg-source').click()
    await expect(resource.locator('.image-copy-actions')).toHaveAttribute('data-state', 'ok')
    const pasted = await pasteSyntheticClipboard(page)
    expect(pasted.text).toBe(SVG_SOURCE)
    expect(pasted.files).toEqual([])
  })

  test('generated SVG with text/plain MIME copies from its file card', async ({ page }) => {
    const { resource } = await openFixture(page, { body: SVG_SOURCE, name: 'drawing.svg', mime: 'text/plain' })
    await expect(resource).toHaveClass(/msg-artifact-chip/)
    await copyImage(page, resource)
  })

  test('viewBox-only SVG receives bounded intrinsic PNG dimensions', async ({ page }) => {
    const { resource } = await openFixture(page, { body: SVG_SOURCE.replace(' width="12" height="8"', ''),
      name: 'viewbox.svg', mime: 'image/svg+xml' })
    await copyImage(page, resource)
  })

  for (const attachment of ['inline', 'staged'] as const) {
    test(`uploaded text/plain SVG stays a file and copies its ${attachment} original`, async ({ page }) => {
      const { resource, originals } = await openFixture(page, {
        body: SVG_SOURCE, name: 'drawing.svg', mime: 'text/plain', attachment,
      })
      await expect(resource).toHaveClass(/msg-file-resource--file/)
      await expect(resource.locator('img')).toHaveCount(0)
      await copyImage(page, resource)
      expect(originals).toHaveLength(attachment === 'staged' ? 1 : 0)
      await expect(resource.locator('img')).toHaveCount(0)
    })
  }

  test('uploaded raster thumbnail exposes copy and shared preview', async ({ page }) => {
    const { resource } = await openFixture(page, { attachment: 'inline' })
    await expect(resource.locator('.msg-thumb')).toBeVisible()
    await copyImage(page, resource)
    await resource.locator('.msg-thumb-button').click()
    await copyImage(page, page.locator('.deliv-preview'))
    await page.keyboard.press('Escape')
    await expect(page.locator('.deliv-preview')).toHaveCount(0)
    await expect(resource.locator('.msg-thumb-button')).toBeFocused()
  })

  test('SVG scripts, event attributes, and external resources stay inert', async ({ page }) => {
    const external: string[] = []
    await page.route('https://svg-fixture.invalid/**', route => {
      external.push(route.request().url())
      return route.abort()
    })
    const unsafe = SVG_SOURCE.replace('<svg ', '<svg onload="window.__imageCopyScriptExecuted = true" ')
      .replace('</svg>', '<script>window.__imageCopyScriptExecuted = true; fetch("https://svg-fixture.invalid/script")</script>'
        + '<image href="https://svg-fixture.invalid/image.png" width="1" height="1"/>'
        + '<style>@import url("https://svg-fixture.invalid/style.css");</style></svg>')
    const { resource } = await openFixture(page, { body: unsafe, mime: 'image/svg+xml', name: 'inert.svg' })
    await copyImage(page, resource)
    expect(external).toEqual([])
    expect(await page.evaluate(() => window.__imageCopyScriptExecuted)).toBeUndefined()
    await expect(page.locator('svg[onload]')).toHaveCount(0)
  })

  test('permission rejection reports failure and keeps Download available', async ({ page }) => {
    await page.addInitScript(() => {
      Object.defineProperty(navigator.clipboard, 'write', {
        configurable: true,
        value: () => Promise.reject(new DOMException('Synthetic clipboard rejection', 'NotAllowedError')),
      })
    })
    const { resource } = await openFixture(page)
    let downloads = 0
    page.on('download', () => { downloads += 1 })
    await resource.getByTestId('copy-image').click()
    await expect(resource.locator('.image-copy-actions')).toHaveAttribute('data-state', 'error')
    await expect(resource.getByTestId('copy-image')).toBeEnabled()
    await expect(resource.getByRole('button', { name: 'Download original.png' })).toBeVisible()
    expect(downloads).toBe(0)
  })

  for (const invalid of [
    { body: 'not an image', name: 'broken.png', mime: 'image/png' },
    { body: '<svg xmlns=\"http://www.w3.org/2000/svg\"><broken>', name: 'broken.svg', mime: 'image/svg+xml' },
  ]) test(`malformed ${invalid.name} reports failure without copying or downloading`, async ({ page }) => {
    const { resource } = await openFixture(page, invalid)
    let downloads = 0
    page.on('download', () => { downloads += 1 })
    await resource.getByTestId('copy-image').click()
    await expect(resource.locator('.image-copy-actions')).toHaveAttribute('data-state', 'error')
    await expect(resource.getByTestId('copy-image')).toBeEnabled()
    expect(downloads).toBe(0)
  })
})
