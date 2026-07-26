import { test, expect, type Page } from '@playwright/test'

const CONTROL_URL = '/control/'
const SESSION_KEY = 'agent:main:webchat:e2eartifactcard'

// 1x1 transparent PNG, used as both the full image and the thumbnail bytes.
const PNG_1x1 = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==',
  'base64',
)

// Seed a finished assistant turn carrying one image, one previewable document,
// and one download-only data file, rewriting chat.history in flight.
async function seedHistory(
  page: Page,
  options: { artifacts?: Array<Record<string, unknown>>; includeHtml?: boolean } = {},
) {
  await page.route('**/api/approvals', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ pending: [] }),
  }))
  await page.routeWebSocket(/\/ws$/, ws => {
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    ws.onMessage(message => {
      let frame: Record<string, unknown>
      try {
        frame = JSON.parse(String(message)) as Record<string, unknown>
      } catch {
        return
      }
      if (frame.type !== 'req') return
      const method = String(frame.method || '')
      if (method === 'connect') {
        ws.send(JSON.stringify({ protocol: 3, policy: { tick_interval_ms: 30000 } }))
        return
      }
      if (method === 'chat.history') {
        const artifacts: Array<Record<string, unknown>> = options.artifacts || [
          {
            id: 'art-card-img',
            name: 'generated-image.png',
            mime: 'image/png',
            size: 744448,
            download_url: '/api/v1/artifacts/art-card-img',
            thumbnail_url: '/api/v1/artifacts/art-card-img?variant=thumb',
          },
          { id: 'art-card-pdf', name: 'report-q2.pdf', mime: 'application/pdf', size: 188416 },
          { id: 'art-card-csv', name: 'pricing.csv', mime: 'text/csv', size: 12288 },
        ]
        if (!options.artifacts && options.includeHtml) {
          artifacts.push({
            id: 'art-card-html',
            name: 'interactive.html',
            mime: 'text/html',
            size: 4096,
            download_url: '/api/v1/artifacts/art-card-html',
          })
        }
        ws.send(JSON.stringify({
          type: 'res',
          id: frame.id,
          ok: true,
          payload: {
            messages: [
              {
                role: 'user',
                text: 'Produce a few deliverables.',
                id: 'msg-artcard-user',
                timestamp: Math.floor(Date.now() / 1000) - 120,
              },
              {
                role: 'assistant',
                text: 'Here you go.',
                id: 'msg-artcard-assistant',
                timestamp: Math.floor(Date.now() / 1000) - 60,
                artifacts,
              },
            ],
            has_more: false,
          },
        }))
        return
      }
      const payloads: Record<string, unknown> = {
        'agents.list': { agents: [] },
        'commands.list_for_surface': { commands: [] },
        'config.get': {
          squilla_router: { enabled: false, rollout_phase: 'observe', tiers: {} },
          permissions: {},
          skills: {},
        },
        'onboarding.status': { audioConfigured: false },
        'sessions.list': { sessions: [], has_more: false },
        'sessions.messages.subscribe': {
          subscribed: true,
          replay_complete: true,
          current_stream_seq: 0,
          run_status: 'idle',
        },
        'usage.status': { sessions: [] },
      }
      ws.send(JSON.stringify({
        type: 'res',
        id: frame.id,
        ok: true,
        payload: payloads[method] ?? {},
      }))
    })
  })
}

async function openSeeded(page: Page) {
  await page.route('**/api/v1/artifacts/**', route =>
    route.fulfill({ status: 200, contentType: 'image/png', body: PNG_1x1 }))
  await seedHistory(page)
  await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(SESSION_KEY))
  await page.waitForSelector('.conn-pill', { timeout: 10000 })
  await page.waitForSelector('.chat-header', { timeout: 10000 })
}

function silentWav(): Buffer {
  const sampleRate = 8000
  const sampleCount = 800
  const dataSize = sampleCount * 2
  const wav = Buffer.alloc(44 + dataSize)
  wav.write('RIFF', 0)
  wav.writeUInt32LE(36 + dataSize, 4)
  wav.write('WAVE', 8)
  wav.write('fmt ', 12)
  wav.writeUInt32LE(16, 16)
  wav.writeUInt16LE(1, 20)
  wav.writeUInt16LE(1, 22)
  wav.writeUInt32LE(sampleRate, 24)
  wav.writeUInt32LE(sampleRate * 2, 28)
  wav.writeUInt16LE(2, 32)
  wav.writeUInt16LE(16, 34)
  wav.write('data', 36)
  wav.writeUInt32LE(dataSize, 40)
  return wav
}

async function openAudioSeeded(page: Page) {
  await seedHistory(page, {
    artifacts: [{
      id: 'art-card-audio',
      name: 'sample.wav',
      mime: 'audio/wav',
      size: silentWav().byteLength,
      download_url: '/api/v1/artifacts/art-card-audio',
    }],
  })
  await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(SESSION_KEY))
  await page.waitForSelector('.chat-header', { timeout: 10000 })
  await expect(page.locator('.msg-audio-card')).toBeVisible({ timeout: 10000 })
}

test.describe('Artifact deliverable cards', () => {
  test('image renders one media card and no duplicate file chip', async ({ page }) => {
    await openSeeded(page)

    const media = page.locator('.msg-media-card')
    await expect(media).toHaveCount(1)
    await expect(media.locator('.msg-media-card__name')).toHaveText('generated-image.png')
    // Clean meta: TYPE · size, never "FILE" or a "Preview file" prefix.
    await expect(media.locator('.msg-media-card__meta')).toHaveText('PNG · 727 KB')

    // The image is NOT also rendered as a file chip.
    const chipNames = await page.locator('.msg-artifact-chip .msg-artifact-name').allTextContents()
    expect(chipNames).not.toContain('generated-image.png')

    // Non-image artifacts are the only file chips: pdf + csv.
    await expect(page.locator('.msg-artifact-chip')).toHaveCount(2)
  })

  test('the media card thumbnail uses the variant=thumb URL', async ({ page }) => {
    const requested: string[] = []
    await page.route('**/api/v1/artifacts/**', route => {
      requested.push(route.request().url())
      route.fulfill({ status: 200, contentType: 'image/png', body: PNG_1x1 })
    })
    await seedHistory(page)
    await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(SESSION_KEY))
    await page.waitForSelector('.msg-media-card__img img', { timeout: 10000 })

    expect(requested.some(url => url.includes('art-card-img') && url.includes('variant=thumb'))).toBe(true)
  })

  test('previewable file card splits Open from Download', async ({ page }) => {
    await openSeeded(page)

    const pdfCard = page.locator('.msg-artifact-chip', { hasText: 'report-q2.pdf' })
    await expect(pdfCard).toBeVisible()
    // Clean meta uses the type pill and size, no doubled category.
    await expect(pdfCard.locator('.msg-artifact-kind')).toHaveText('PDF')
    await expect(pdfCard.locator('.msg-artifact-size')).toHaveText('184 KB')

    // Open and Download are separate, separately labelled controls.
    const openBtn = pdfCard.getByRole('button', { name: 'Open report-q2.pdf' })
    const downloadBtn = pdfCard.getByRole('button', { name: 'Download report-q2.pdf' })
    await expect(openBtn).toBeVisible()
    await expect(downloadBtn).toBeVisible()

    // Open opens a new tab; it never downloads.
    const popupPromise = page.waitForEvent('popup')
    await openBtn.click()
    const popup = await popupPromise
    expect(popup).toBeTruthy()
    await popup.close()
  })

  test('download-only file card has a Download control and no Open', async ({ page }) => {
    await openSeeded(page)

    const csvCard = page.locator('.msg-artifact-chip', { hasText: 'pricing.csv' })
    await expect(csvCard).toBeVisible()
    await expect(csvCard.locator('.msg-artifact-kind')).toHaveText('CSV')

    // No Open affordance for non-previewable data.
    await expect(csvCard.getByRole('button', { name: 'Open pricing.csv' })).toHaveCount(0)
    await expect(csvCard.getByRole('button', { name: 'Download pricing.csv' })).toBeVisible()
  })

  test('html file card is download-only and never invokes the native-open endpoint', async ({ page }) => {
    let nativeOpenCount = 0
    let htmlDownloadCount = 0
    await page.route('**/api/v1/artifacts/**', async route => {
      const request = route.request()
      const url = new URL(request.url())
      if (url.pathname === '/api/v1/artifacts/art-card-html/open') {
        nativeOpenCount += 1
        expect(request.method()).toBe('POST')
        expect(request.headers()['x-opensquilla-session-key']).toBe(SESSION_KEY)
        await route.fulfill({
          status: 202,
          contentType: 'application/json',
          body: JSON.stringify({ ok: true, status: 'accepted' }),
        })
        return
      }
      if (url.pathname === '/api/v1/artifacts/art-card-html') {
        htmlDownloadCount += 1
        await route.fulfill({
          status: 200,
          contentType: 'text/html',
          body: '<html><body>download only</body></html>',
        })
        return
      }
      await route.fulfill({ status: 200, contentType: 'image/png', body: PNG_1x1 })
    })
    await seedHistory(page, { includeHtml: true })
    await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(SESSION_KEY))
    await page.waitForSelector('.chat-header', { timeout: 10000 })

    const htmlCard = page.locator('.msg-artifact-chip', { hasText: 'interactive.html' })
    await expect(htmlCard).toBeVisible()
    await expect(htmlCard.getByRole('button', { name: 'Open interactive.html' })).toHaveCount(0)
    const downloadPromise = page.waitForEvent('download')
    await htmlCard.getByRole('button', { name: 'Download interactive.html' }).click()
    const download = await downloadPromise

    expect(download.suggestedFilename()).toBe('interactive.html')
    expect(nativeOpenCount).toBe(0)
    expect(htmlDownloadCount).toBe(1)
  })

  test('audio performs zero initial requests and fetches authenticated bytes only after Play', async ({ page }) => {
    const requests: Array<{
      authorization?: string
      sessionKey?: string
    }> = []
    await page.addInitScript(() => {
      sessionStorage.setItem('opensquilla.wsToken', 'audio-token-e2e')
    })
    await page.route('**/api/v1/artifacts/art-card-audio*', route => {
      const request = route.request()
      requests.push({
        authorization: request.headers().authorization,
        sessionKey: request.headers()['x-opensquilla-session-key'],
      })
      return route.fulfill({
        status: 200,
        contentType: 'audio/wav',
        body: silentWav(),
      })
    })
    await openAudioSeeded(page)

    await page.waitForTimeout(200)
    expect(requests).toHaveLength(0)

    await page.getByRole('button', { name: 'Play audio sample.wav' }).click()
    const player = page.locator('.msg-audio-card__player')
    await expect(player).toBeVisible({ timeout: 10000 })
    await expect(player).toHaveAttribute('controls', '')
    expect(requests).toEqual([{
      authorization: 'Bearer audio-token-e2e',
      sessionKey: SESSION_KEY,
    }])
  })

  test('audio failure exposes Retry and Download, then recovers to native controls', async ({ page }) => {
    let shouldFail = true
    let requests = 0
    await page.route('**/api/v1/artifacts/art-card-audio*', route => {
      requests += 1
      if (shouldFail) return route.fulfill({ status: 500, body: 'audio failed' })
      return route.fulfill({
        status: 200,
        contentType: 'audio/wav',
        body: silentWav(),
      })
    })
    await openAudioSeeded(page)

    const card = page.locator('.msg-audio-card')
    await page.getByRole('button', { name: 'Play audio sample.wav' }).click()
    await expect(card).toHaveAttribute('data-state', 'error')
    await expect(card.getByText('Audio could not be loaded.')).toBeVisible()
    await expect(card.getByRole('button', { name: 'Retry sample.wav' })).toBeVisible()
    await expect(card.getByRole('button', { name: 'Download sample.wav' })).toBeVisible()

    shouldFail = false
    await card.getByRole('button', { name: 'Retry sample.wav' }).click()
    await expect(card.locator('.msg-audio-card__player')).toBeVisible({ timeout: 10000 })
    expect(requests).toBe(2)
  })
})
