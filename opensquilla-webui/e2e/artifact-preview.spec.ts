import { test, expect, type Page, type Route } from '@playwright/test'

const CONTROL_URL = '/control/'
const SESSION_KEY = 'agent:main:webchat:e2eartifactpreview'

// 1x1 transparent PNG, used as both the full image and the thumbnail bytes.
const PNG_1x1 = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==',
  'base64',
)

const DEFAULT_ARTIFACTS = [
  {
    id: 'art-preview-img',
    name: 'render.png',
    mime: 'image/png',
    size: 744448,
    download_url: '/api/v1/artifacts/art-preview-img',
    thumbnail_url: '/api/v1/artifacts/art-preview-img?variant=thumb',
  },
]

// Seed one finished assistant turn carrying image artifacts with thumbnail
// variants, rewriting chat.history in flight.
async function seedHistory(page: Page, artifacts: object[] = DEFAULT_ARTIFACTS) {
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
                text: 'Render an image.',
                id: 'msg-preview-user',
                timestamp: Math.floor(Date.now() / 1000) - 120,
              },
              {
                role: 'assistant',
                text: 'Here it is.',
                id: 'msg-preview-assistant',
                timestamp: Math.floor(Date.now() / 1000) - 60,
                artifacts,
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
  return route.fulfill({ status: 200, contentType: 'image/png', body: PNG_1x1 })
}

async function openSeeded(
  page: Page,
  artifacts?: object[],
  options: { workbenchEnabled?: boolean } = {},
) {
  await page.addInitScript(({ workbenchEnabled }) => {
    window.OPENSQUILLA_FEATURES = {
      ...(window.OPENSQUILLA_FEATURES || {}),
      artifactWorkbench: workbenchEnabled,
    }
  }, { workbenchEnabled: options.workbenchEnabled !== false })
  await seedHistory(page, artifacts)
  await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(SESSION_KEY))
  await page.waitForSelector('.conn-pill', { timeout: 10000 })
  await page.waitForSelector('.chat-header', { timeout: 10000 })
}

test.describe('Artifact preview performance and slow-network states', () => {
  test('thumbnail loads lazily and resolves to a media image', async ({ page }) => {
    const requested: string[] = []
    await page.route('**/api/v1/artifacts/**', route => {
      requested.push(route.request().url())
      return fulfillPng(route)
    })
    await openSeeded(page)

    // The state machine reaches loaded and renders the thumbnail image.
    await page.waitForSelector('.msg-media-card__img img', { timeout: 10000 })
    // Lazy fetch targets the thumbnail variant, not the full image.
    expect(requested.some(url => url.includes('art-preview-img') && url.includes('variant=thumb'))).toBe(true)
  })

  test('a slow link shows a loading state before the image', async ({ page }) => {
    await page.route('**/api/v1/artifacts/**', async route => {
      await new Promise(resolve => setTimeout(resolve, 1200))
      return fulfillPng(route)
    })
    await openSeeded(page)

    // While the fetch is in flight, the loading placeholder is visible and the
    // image is not yet rendered.
    await expect(page.locator('.msg-media-card__img--loading')).toBeVisible({ timeout: 10000 })
    await expect(page.locator('.msg-media-card__img img')).toHaveCount(0)

    // It eventually resolves to the loaded image, never a permanent loading box.
    await expect(page.locator('.msg-media-card__img img')).toBeVisible({ timeout: 10000 })
    await expect(page.locator('.msg-media-card__img--loading')).toHaveCount(0)
  })

  test('a failed preview shows Retry + Download and recovers on retry', async ({ page }) => {
    let failNext = true
    await page.route('**/api/v1/artifacts/**', route => {
      if (failNext) {
        failNext = false
        return route.abort('failed')
      }
      return fulfillPng(route)
    })
    await openSeeded(page)

    // The dead-end "loading forever" is replaced by an error card.
    const errorCard = page.locator('.msg-media-card__img--error')
    await expect(errorCard).toBeVisible({ timeout: 10000 })
    await expect(errorCard).toHaveAttribute('data-state', 'error')

    // Download is decoupled and present even while the preview failed.
    await expect(errorCard.getByRole('button', { name: /Download render\.png/ })).toBeVisible()

    // Retry re-fetches; the route now succeeds and the image resolves.
    await errorCard.getByRole('button', { name: /Retry preview for render\.png/ }).click()
    await expect(page.locator('.msg-media-card__img img')).toBeVisible({ timeout: 10000 })
    await expect(page.locator('.msg-media-card__img--error')).toHaveCount(0)
  })

  test('clicking the media image opens an in-app lightbox, not a download or new tab', async ({ page }) => {
    let popupOpened = false
    let downloadTriggered = false
    page.on('popup', () => { popupOpened = true })
    page.on('download', () => { downloadTriggered = true })

    await page.route('**/api/v1/artifacts/**', route => fulfillPng(route))
    await openSeeded(page)

    // The thumbnail resolves, then the image button is the in-app trigger.
    const imgButton = page.locator('.msg-media-card__img')
    await expect(page.locator('.msg-media-card__img img')).toBeVisible({ timeout: 10000 })
    await imgButton.click()

    // A real modal dialog opens in-app: role=dialog + aria-modal, with a Download
    // action in its footer. It is neither a popup nor a browser download.
    const dialog = page.locator('.deliv-preview[role="dialog"]')
    await expect(dialog).toBeVisible({ timeout: 10000 })
    await expect(dialog).toHaveAttribute('aria-modal', 'true')
    await expect(dialog.getByRole('button', { name: /Download/ })).toBeVisible()
    // The lightbox shows the full image, never navigating away.
    await expect(dialog.locator('.deliv-preview__image')).toBeVisible({ timeout: 10000 })
    await expect(page.getByTestId('workbench-host')).toHaveCount(0)

    // Focus enters the dialog and wraps in both directions.
    const close = dialog.getByRole('button', { name: /Close preview/ })
    const download = dialog.getByRole('button', { name: /Download/ })
    await expect(close).toBeFocused()
    await page.keyboard.press('Shift+Tab')
    await expect(download).toBeFocused()
    await page.keyboard.press('Tab')
    await expect(close).toBeFocused()

    // Escape closes the lightbox and returns focus to the invoking image.
    await page.keyboard.press('Escape')
    await expect(dialog).toHaveCount(0)
    await expect(imgButton).toBeFocused()

    expect(popupOpened).toBe(false)
    expect(downloadTriggered).toBe(false)
  })

  test('the lightbox frame stays the same size when navigating between images', async ({ page }) => {
    // Two SVGs whose declared intrinsic sizes differ wildly: a small explicit
    // pixel size vs. a viewBox with no width/height at all. Before the fixed
    // media frame, the shrink-wrapped panel resized to each image's intrinsic
    // box, so prev/next made the whole dialog jump.
    const svgSmall = Buffer.from(
      '<svg xmlns="http://www.w3.org/2000/svg" width="240" height="280" viewBox="0 0 240 280">' +
      '<rect width="240" height="280" fill="#c8d6f0"/></svg>',
    )
    const svgLarge = Buffer.from(
      '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 900">' +
      '<rect width="800" height="900" fill="#f0d6c8"/></svg>',
    )
    await page.route('**/api/v1/artifacts/**', route => {
      const body = route.request().url().includes('art-nav-small') ? svgSmall : svgLarge
      return route.fulfill({ status: 200, contentType: 'image/svg+xml', body })
    })
    await openSeeded(page, [
      {
        id: 'art-nav-small',
        name: 'small.svg',
        mime: 'image/svg+xml',
        size: 512,
        download_url: '/api/v1/artifacts/art-nav-small',
        thumbnail_url: '/api/v1/artifacts/art-nav-small?variant=thumb',
      },
      {
        id: 'art-nav-large',
        name: 'large.svg',
        mime: 'image/svg+xml',
        size: 512,
        download_url: '/api/v1/artifacts/art-nav-large',
        thumbnail_url: '/api/v1/artifacts/art-nav-large?variant=thumb',
      },
    ])

    await expect(page.locator('.msg-media-card__img img').first()).toBeVisible({ timeout: 10000 })
    await page.locator('.msg-media-card__img').first().click()

    const dialog = page.locator('.deliv-preview[role="dialog"]')
    const image = dialog.locator('.deliv-preview__image')
    await expect(image).toBeVisible({ timeout: 10000 })
    const panel = dialog.locator('.deliv-preview__panel')
    const before = await panel.boundingBox()
    expect(before).not.toBeNull()

    const srcBefore = await image.getAttribute('src')
    await page.keyboard.press('ArrowRight')
    await expect(image).toBeVisible({ timeout: 10000 })
    await expect(dialog.locator('.deliv-preview__title')).toHaveText('large.svg')
    // The full image is served as a fresh blob URL, so a changed src proves
    // the second image actually rendered before we measure.
    await expect.poll(() => image.getAttribute('src'), { timeout: 10000 }).not.toBe(srcBefore)

    const after = await panel.boundingBox()
    expect(after).not.toBeNull()
    expect(Math.round(after!.width)).toBe(Math.round(before!.width))
    expect(Math.round(after!.height)).toBe(Math.round(before!.height))

    await page.keyboard.press('ArrowLeft')
    await expect(dialog.locator('.deliv-preview__title')).toHaveText('small.svg')
  })

  test('the legacy Workbench kill switch still keeps image preview in the Lightbox', async ({ page }) => {
    await page.route('**/api/v1/artifacts/**', route => fulfillPng(route))
    await openSeeded(page, undefined, { workbenchEnabled: false })

    await expect(page.locator('.msg-media-card__img img')).toBeVisible({ timeout: 10000 })
    await page.locator('.msg-media-card__img').click()

    await expect(page.locator('.deliv-preview[role="dialog"]')).toBeVisible()
    await expect(page.getByTestId('workbench-host')).toHaveCount(0)
  })

  test('the caption Download control works regardless of preview state', async ({ page }) => {
    await page.route('**/api/v1/artifacts/**', route => route.abort('failed'))
    await openSeeded(page)

    // Preview failed, but the caption-bar Download button is always available.
    await expect(page.locator('.msg-media-card__img--error')).toBeVisible({ timeout: 10000 })
    const downloadBtn = page.locator('.msg-media-card__download')
    await expect(downloadBtn).toBeVisible()
    await expect(downloadBtn).toBeEnabled()
  })
})
