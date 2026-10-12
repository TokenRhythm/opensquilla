import { test, expect, type Page, type Route } from '@playwright/test'

const CONTROL_URL = '/control/'
const SESSION_KEY = 'agent:main:webchat:e2eshareimage'

// A 32x32 SOLID MAGENTA (#FF00FF) PNG, served as the artifact thumbnail bytes.
// The chat renders this as a real <img> whose src is a blob: object URL
// (URL.createObjectURL) — that blob src is the whole point of this regression:
// html-to-image's cache-busting once appended a "?<ts>" query to it, which a
// blob URL cannot resolve, so the capture rejected and "Share export failed".
//
// The colour is deliberate and load-bearing: the test scans the exported PNG
// for magenta to prove the image was actually EMBEDDED, not merely that the
// export survived. The composable swallows per-image embed errors so the PNG
// still renders when one image fails — a transparent or absent image would let
// the export "succeed" with the picture silently missing.
const PNG_MAGENTA = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAIAAAD8GO2jAAAAJ0lEQVR42u3NMQkAAAwD'
  + 'sPo33anoMQjkT5pORSAQCAQCgUAgEHwJDmDu+Gr4yaoqAAAAAElFTkSuQmCC',
  'base64',
)

// Seed one finished assistant turn that carries a single image artifact with a
// thumbnail variant, rewriting chat.history in flight.
async function seedHistory(page: Page) {
  await page.routeWebSocket(/\/ws$/, ws => {
    const server = ws.connectToServer()
    const historyIds = new Set<string>()
    ws.onMessage(message => {
      try {
        const frame = JSON.parse(String(message))
        if (frame?.type === 'req' && frame.method === 'chat.history') {
          historyIds.add(String(frame.id))
        }
      } catch {}
      server.send(message)
    })
    server.onMessage(message => {
      try {
        const frame = JSON.parse(String(message))
        if (frame?.type === 'res' && frame.id !== undefined && historyIds.has(String(frame.id))) {
          historyIds.delete(String(frame.id))
          frame.ok = true
          delete frame.error
          frame.payload = {
            messages: [
              {
                role: 'user',
                text: '画一张蚂蚁搬砖',
                id: 'msg-share-img-user',
                timestamp: Math.floor(Date.now() / 1000) - 120,
              },
              {
                role: 'assistant',
                text: 'Here is the image.',
                id: 'msg-share-img-assistant',
                timestamp: Math.floor(Date.now() / 1000) - 60,
                artifacts: [
                  {
                    id: 'art-share-img',
                    name: 'render.png',
                    mime: 'image/png',
                    size: 744448,
                    download_url: '/api/v1/artifacts/art-share-img',
                    thumbnail_url: '/api/v1/artifacts/art-share-img?variant=thumb',
                  },
                ],
              },
            ],
            has_more: false,
          }
          ws.send(JSON.stringify(frame))
          return
        }
      } catch {}
      ws.send(message)
    })
  })
}

function fulfillPng(route: Route) {
  return route.fulfill({ status: 200, contentType: 'image/png', body: PNG_MAGENTA })
}

test.describe('Share export with an image artifact', () => {
  // Regression: a conversation whose assistant message contains a generated
  // image (rendered from a blob: object URL) must still export to a PNG. The
  // capture used to reject on the blob <img>, surfacing "Share export failed".
  test('exports a conversation that includes a blob-backed image artifact', async ({ page }) => {
    await page.route('**/api/v1/artifacts/**', route => fulfillPng(route))
    await seedHistory(page)
    await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(SESSION_KEY))
    await page.waitForSelector('.topbar[data-connection-state]', { timeout: 10000 })

    // The assistant thumbnail must be a real, loaded <img> (its src is the
    // blob: URL) before we capture — otherwise the clone has nothing to embed.
    await expect(page.locator('.msg-media-card__img img')).toBeVisible({ timeout: 10000 })
    const thumbSrc = await page.locator('.msg-media-card__img img').first().getAttribute('src')
    expect(thumbSrc).toMatch(/^blob:/)

    // Watch for the failure toast so a rejected capture fails loudly here.
    const toast = page.getByText('Share export failed')

    // Enter share mode and select both bubbles, including the one with the image.
    await page.getByTestId('chat-session-actions-trigger').focus()
    await page.keyboard.press('Enter')
    await page.getByRole('menuitem', { name: 'Share', exact: true }).focus()
    await page.keyboard.press('Enter')
    const pickers = page.getByRole('button', { name: 'Add to share image' })
    await expect(pickers.first()).toBeVisible()
    expect(await pickers.count()).toBeGreaterThanOrEqual(2)
    await pickers.first().focus()
    await page.keyboard.press('Enter')
    await pickers.first().focus()
    await page.keyboard.press('Enter')
    await expect(page.locator('.chat-share-banner__count')).toHaveText(/2 selected/)

    // Save renders the PNG and opens the preview modal. With the bug this throws
    // and shows the failure toast instead of a dialog.
    await page.getByRole('button', { name: /Save PNG/ }).focus()
    await page.keyboard.press('Enter')

    const dialog = page.getByRole('dialog', { name: 'Share preview' })
    await expect(dialog).toBeVisible({ timeout: 15000 })
    const previewImg = dialog.getByRole('img', { name: 'Share preview' })
    await expect(previewImg).toBeVisible()
    await expect.poll(async () => previewImg.evaluate((img: HTMLImageElement) => img.naturalWidth))
      .toBeGreaterThan(0)

    // The export must actually CONTAIN the selected image, not merely succeed.
    // The composable swallows per-image embed errors, so a regression that drops
    // the blob image still produces a valid (image-less) PNG. Decode the preview
    // (a same-origin blob, so the canvas is readable) and require the seeded
    // magenta to be present — proving the picture was embedded, not skipped.
    const magentaPixels = await previewImg.evaluate((img: HTMLImageElement) => {
      const canvas = document.createElement('canvas')
      canvas.width = img.naturalWidth
      canvas.height = img.naturalHeight
      const ctx = canvas.getContext('2d')
      if (!ctx) return -1
      ctx.drawImage(img, 0, 0)
      const { data } = ctx.getImageData(0, 0, canvas.width, canvas.height)
      let count = 0
      for (let i = 0; i < data.length; i += 4) {
        // Tolerant match around #FF00FF; interior pixels stay near-exact even
        // after the contain-scale + 2x raster, edges may blend toward the card.
        if (data[i] > 215 && data[i + 1] < 40 && data[i + 2] > 215 && data[i + 3] > 200) count++
      }
      return count
    })
    expect(magentaPixels, 'the selected image must be embedded in the exported PNG')
      .toBeGreaterThan(100)

    // The export must not have surfaced the failure toast.
    await expect(toast).toHaveCount(0)
  })
})
