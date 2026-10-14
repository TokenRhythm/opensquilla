import { test, expect, type Page } from '@playwright/test'
import { helloOkResponse } from './support/gateway-fixture'
import {
  chatHistoryPayload,
  sessionMessagesHydratePayload,
  sessionMessagesSnapshotPayload,
  sessionMessagesSubscribePayload,
} from './support/session-read-fixtures'

const CONTROL_URL = '/control/'
const SESSION_KEY = 'agent:main:webchat:e2eartifactcard'

// 1x1 transparent PNG, used as both the full image and the thumbnail bytes.
const PNG_1x1 = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==',
  'base64',
)

// Synthetic 16x16 VP9 WebM with a 0.2-second black frame.
const WEBM_TINY = Buffer.from(
  'GkXfo59ChoEBQveBAULygQRC84EIQoKEd2VibUKHgQJChYECGFOAZwEAAAAAAAJeEU2bdLpNu4tTq4QVSalmU6yBoU27i1OrhBZUrmtTrIHYTbuMU6uEElTDZ1OsggElTbuMU6uEHFO7a1OsggJI7AEAAAAAAABZAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAVSalmsirXsYMPQkBNgI1MYXZmNjIuMTIuMTAyV0GNTGF2ZjYyLjEyLjEwMkSJiEBpAAAAAAAAFlSua8iuAQAAAAAAAD/XgQFzxYg+1LSSKAxeUpyBACK1nIN1bmSIgQCGhVZfVlA5g4EBI+ODhAJiWgDgkLCBELqBEJqBAlW5gQESVMNnQIBzc6BjwIBnyJpFo4dFTkNPREVSRIeNTGF2ZjYyLjEyLjEwMnNz2mPAi2PFiD7UtJIoDF5SZ8ilRaOHRU5DT0RFUkSHmExhdmM2Mi4yOC4xMDIgbGlidnB4LXZwOWfIoUWjiERVUkFUSU9ORIeTMDA6MDA6MDAuMjAwMDAwMDAwAB9DtnVAl+eBAKO+gQAAgIJJg0IAAPAA9gY4JBwYQgAAIEAAIpv//6UT+4KU3o8VSrdtJ/1U/RntlFLTcdJsyP6m92VMvCYMgACjk4EAKACGAECSnChJQAADcAAAQkCjk4EAUACGAECSnCxKwAADcAAAQkCjk4EAeACGAECSnCxJwAADcAAAQkCjk4EAoACGAECSnChIoAADcAAAQkAcU7trkbuPs4EAt4r3gQHxggGr8IED',
  'base64',
)

// Seed a finished assistant turn carrying one image, one PDF, and one CSV,
// rewriting chat.history in flight.
async function seedHistory(
  page: Page,
  options: { artifacts?: Array<Record<string, unknown>>; includeHtml?: boolean; workspacePreviewHtml?: boolean } = {},
) {
  const requests: Array<{ method: string; params?: Record<string, unknown> }> = []
  const documentId = 'doc_artifact_card_html'
  const revisionId = 'rev-artifact-card-current'
  const currentArtifactId = 'art-card-html-current'
  const document = { documentId, headRevisionId: revisionId, name: 'interactive.html',
    sessionKey: SESSION_KEY, kind: 'html', latestDownloadUrl: `/api/v1/artifact-documents/${documentId}`,
    capabilities: { download: true, preview: true, revisions: true, source: false } }
  const revision = { revisionId, documentId, artifactId: currentArtifactId, filename: 'interactive.html',
    mediaType: 'text/html', byteSize: 4096, artifactSha256: 'b'.repeat(64),
    downloadUrl: `/api/v1/artifact-documents/${documentId}?revisionId=${revisionId}` }
  const resource = { resource: { type: 'document', documentId }, name: 'interactive.html', mime: 'text/html',
    sha256: 'b'.repeat(64), size: 4096, downloadUrl: document.latestDownloadUrl,
    capabilities: { preview: true, download: true, edit: false },
    relations: { documentId, headRevisionId: revisionId, headArtifactId: currentArtifactId } }
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
      const params = frame.params as Record<string, unknown> | undefined
      requests.push({ method, params })
      if (method === 'connect') {
        const auth = params?.auth as Record<string, unknown> | undefined
        // Match the local owner handshake: only a supplied token is verified.
        ws.send(helloOkResponse({
          ...(options.workspacePreviewHtml ? { features: { methods: [
            'sessions.messages.subscribe', 'sessions.messages.snapshot', 'sessions.messages.hydrate',
            'artifacts.get', 'workbench.resources.list', 'workbench.resources.get', 'workbench.resources.open',
            'artifacts.documents.get', 'artifacts.revisions.list', 'artifacts.changes.list',
          ] } } : {}),
          auth: {
            principal: {
              isOwner: true,
              authenticated: typeof auth?.token === 'string' && auth.token.length > 0,
              authState: 'authenticated',
              role: 'operator',
              scopes: ['operator.read', 'operator.write'],
              capabilities: ['chat.read', 'chat.write'],
            },
          },
        }))
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
            ...(options.workspacePreviewHtml ? {
              kind: 'artifact_ref', session_id: 'artifact-card-session-id', source: 'workspace-preview',
            } : {}),
            download_url: '/api/v1/artifacts/art-card-html',
          })
        }
        ws.send(JSON.stringify({
          type: 'res',
          id: frame.id,
          ok: true,
          payload: chatHistoryPayload([
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
          ]),
        }))
        return
      }
      if (options.workspacePreviewHtml) {
        let payload: unknown
        if (method === 'artifacts.get') {
          expect(params?.sessionKey).toBe(SESSION_KEY)
          expect(params?.artifactId).toBe('art-card-html')
          payload = { artifact: { id: 'art-card-html', name: 'interactive.html', mime: 'text/html',
            source: 'workspace-preview', documentId } }
        } else if (method === 'workbench.resources.list') {
          payload = { resources: [resource], totalCount: 1, pageSize: 200, returnedCount: 1,
            hasMore: false, nextCursor: null }
        } else if (method === 'workbench.resources.get' || method === 'workbench.resources.open') {
          expect(params?.sessionKey).toBe(SESSION_KEY)
          expect(params?.resourceRef).toMatchObject({ type: 'document', documentId })
          payload = method === 'workbench.resources.get' ? { resource }
            : { disposition: 'document', resolution: { status: 'current' }, resource,
                document, revision, materialized: false }
        } else if (method === 'artifacts.documents.get') payload = { document }
        else if (method === 'artifacts.revisions.list') payload = { revisions: [revision] }
        else if (method === 'artifacts.changes.list') payload = { changeSets: [] }
        if (payload) {
          ws.send(JSON.stringify({ type: 'res', id: frame.id, ok: true, payload }))
          return
        }
      }
      const key = String(params?.key || params?.sessionKey || SESSION_KEY)
      const payloads: Record<string, unknown> = {
        'agents.list': { agents: [] },
        'commands.list_for_surface': { commands: [] },
        'config.get': {
          squilla_router: { enabled: false, rollout_phase: 'observe', tiers: {} },
          permissions: {},
          skills: {},
        },
        'onboarding.status': { audioConfigured: false },
        'sessions.list': { sessions: [], count: 0, ts: 1_800_000_000, has_more: false },
        'sessions.messages.subscribe': sessionMessagesSubscribePayload(key),
        'sessions.messages.snapshot': sessionMessagesSnapshotPayload(key),
        'sessions.messages.hydrate': sessionMessagesHydratePayload(key),
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
  return { requests }
}

async function openSeeded(page: Page) {
  await page.route('**/api/v1/artifacts/**', route => {
    const pathname = new URL(route.request().url()).pathname
    if (pathname.endsWith('/art-card-pdf')) {
      return route.fulfill({
        status: 200,
        contentType: 'application/pdf',
        body: '%PDF-1.4\n%%EOF',
      })
    }
    return route.fulfill({ status: 200, contentType: 'image/png', body: PNG_1x1 })
  })
  await seedHistory(page)
  await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(SESSION_KEY))
  await page.waitForSelector('.topbar[data-connection-state]', { timeout: 10000 })
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

async function openVideoSeeded(page: Page) {
  await seedHistory(page, {
    artifacts: [{
      id: 'art-card-video',
      name: 'sample.webm',
      mime: 'video/webm',
      size: WEBM_TINY.byteLength,
      download_url: '/api/v1/artifacts/art-card-video',
    }],
  })
  await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(SESSION_KEY))
  await page.waitForSelector('.chat-header', { timeout: 10000 })
  await expect(page.locator('.msg-video-card')).toBeVisible({ timeout: 10000 })
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

  test('previewable file card opens the Workbench without downloading', async ({ page }) => {
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

    let popupCount = 0
    page.on('popup', () => { popupCount += 1 })
    await openBtn.click()
    const workbench = page.getByTestId('workbench-host')
    await expect(workbench).toBeVisible()
    await expect(workbench).toHaveAttribute('role', 'complementary')
    await expect(workbench.getByRole('tab', { name: 'report-q2.pdf', exact: true })).toBeVisible()
    await expect(workbench.getByRole('tab', { name: 'report-q2.pdf', exact: true })).toHaveAttribute('aria-selected', 'true')
    await expect(workbench.locator('.artifact-preview__frame--pdf')).toBeVisible()
    expect(popupCount).toBe(0)
  })

  test('CSV file card exposes Office Open and a separate Download control', async ({ page }) => {
    await openSeeded(page)

    const csvCard = page.locator('.msg-artifact-chip', { hasText: 'pricing.csv' })
    await expect(csvCard).toBeVisible()
    await expect(csvCard.locator('.msg-artifact-kind')).toHaveText('CSV')

    await expect(csvCard.getByRole('button', { name: 'Open pricing.csv' })).toBeVisible()
    await expect(csvCard.getByRole('button', { name: 'Download pricing.csv' })).toBeVisible()
  })

  test('download-only file card has a Download control and no Open', async ({ page }) => {
    await seedHistory(page, {
      artifacts: [{ id: 'art-card-json', name: 'pricing.json', mime: 'application/json', size: 12288 }],
    })
    await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(SESSION_KEY))

    const dataCard = page.locator('.msg-artifact-chip', { hasText: 'pricing.json' })
    await expect(dataCard).toBeVisible()
    await expect(dataCard.locator('.msg-artifact-kind')).toHaveText('JSON')
    await expect(dataCard.getByRole('button', { name: 'Open pricing.json' })).toHaveCount(0)
    await expect(dataCard.getByRole('button', { name: 'Download pricing.json' })).toBeVisible()
  })

  for (const workspacePreview of [false, true]) {
  test(`${workspacePreview ? 'internal workspace-preview' : 'html file'} card opens the Workbench and downloads separately`, async ({ page }) => {
    const previewPath = '/api/v1/artifact-preview/art-card-html-token/interactive.html'
    const html = '<html><body>Offline HTML preview</body></html>'
    let leaseCount = 0
    let previewCount = 0
    let nativeOpenCount = 0
    let htmlContentRequestCount = 0
    let documentContentRequestCount = 0
    let browserDownloadCount = 0
    page.on('download', () => { browserDownloadCount += 1 })
    await page.addInitScript(() => {
      localStorage.setItem('opensquilla.workbench.preview.v1', JSON.stringify({
        version: 1, mode: 'offline', noticeShown: true,
      }))
    })
    await page.route(`**${previewPath}`, route => {
      previewCount += 1
      return route.fulfill({ status: 200, contentType: 'text/html', body: html })
    })
    await page.route('**/api/v1/artifact-preview-leases/art-card-html-lease', route =>
      route.fulfill({ status: 204 }))
    await page.route('**/api/v1/artifact-documents/doc_artifact_card_html*', route => {
      documentContentRequestCount += 1
      return route.fulfill({ status: 200, contentType: 'text/html', body: html })
    })
    await page.route('**/api/v1/artifacts/**', async route => {
      const request = route.request()
      const url = new URL(request.url())
      if (url.pathname === `/api/v1/artifacts/${workspacePreview ? 'art-card-html-current' : 'art-card-html'}/preview-leases`) {
        leaseCount += 1
        expect(request.method()).toBe('POST')
        expect(request.headers()['x-opensquilla-session-key']).toBe(SESSION_KEY)
        expect(request.postDataJSON()).toEqual({ version: 1, mode: 'offline', client: 'web' })
        await route.fulfill({
          status: 201,
          json: {
            version: 1,
            lease_id: 'art-card-html-lease',
            effective_mode: 'offline',
            launch_url: previewPath,
            entrypoint: 'interactive.html',
            expires_at: new Date(Date.now() + 60 * 60 * 1000).toISOString(),
            preview_origin: null,
            idle_timeout_seconds: 3600,
            source: {
              kind: 'single_file', collection_status: 'not_applicable', file_count: 1,
              total_bytes: Buffer.byteLength(html), warning_codes: [],
            },
          },
        })
        return
      }
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
        htmlContentRequestCount += 1
        await route.fulfill({
          status: 200,
          contentType: 'text/html',
          body: html,
        })
        return
      }
      await route.fulfill({ status: 200, contentType: 'image/png', body: PNG_1x1 })
    })
    const gateway = await seedHistory(page, { includeHtml: true, workspacePreviewHtml: workspacePreview })
    await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(SESSION_KEY))
    await page.waitForSelector('.chat-header', { timeout: 10000 })

    const htmlCard = page.locator('.msg-artifact-chip', { hasText: 'interactive.html' })
    await expect(htmlCard).toBeVisible()
    await expect(htmlCard.locator('.msg-artifact-kind')).toHaveText('HTML')
    await htmlCard.getByRole('button', { name: 'Open interactive.html' }).click()
    const workbench = page.getByTestId('workbench-host')
    await expect(workbench).toBeVisible()
    await expect(workbench.getByRole('tab', { name: 'interactive.html', exact: true })).toBeVisible()
    await expect(workbench.getByRole('tab', { name: 'interactive.html', exact: true })).toHaveAttribute('aria-selected', 'true')
    const preview = workbench.locator('.artifact-preview__frame--html')
    await expect(preview).toBeVisible()
    await expect(preview).toHaveAttribute('sandbox', 'allow-scripts')
    await expect(preview).toHaveAttribute('src', new URL(previewPath, page.url()).toString())
    await expect(preview.contentFrame().getByText('Offline HTML preview')).toBeVisible()
    // The initial preview resource can read the artifact before its lease is
    // installed. Reading those bytes must not trigger a browser download.
    expect(browserDownloadCount).toBe(0)

    const downloadPromise = page.waitForEvent('download')
    await htmlCard.getByRole('button', { name: 'Download interactive.html' }).click()
    const download = await downloadPromise

    expect(download.suggestedFilename()).toBe('interactive.html')
    expect(nativeOpenCount).toBe(0)
    expect(leaseCount).toBe(1)
    expect(previewCount).toBe(1)
    if (workspacePreview) {
      expect(htmlContentRequestCount).toBe(1) // Only explicit download reads the old artifact.
      expect(documentContentRequestCount).toBeGreaterThan(0)
      expect(gateway.requests).toEqual(expect.arrayContaining([
        { method: 'artifacts.get', params: { sessionKey: SESSION_KEY, artifactId: 'art-card-html' } },
        expect.objectContaining({ method: 'workbench.resources.open' }),
      ]))
    } else expect(htmlContentRequestCount).toBe(2)
    expect(browserDownloadCount).toBe(1)

    await workbench.locator('.workbench-host__chrome').getByRole('button', { name: 'Collapse workbench' }).click()
    await expect(workbench).not.toBeVisible()
    await htmlCard.locator('.msg-artifact-body').click()
    await expect(workbench).toBeVisible()
    await expect(preview.contentFrame().getByText('Offline HTML preview')).toBeVisible()
    expect(nativeOpenCount).toBe(0)
    expect(browserDownloadCount).toBe(1)
  })
  }

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

  test('video stays in the message, performs zero initial requests, and loads authenticated controls', async ({ page }) => {
    const requests: Array<{
      authorization?: string
      sessionKey?: string
    }> = []
    await page.addInitScript(() => {
      sessionStorage.setItem('opensquilla.wsToken', 'video-token-e2e')
    })
    await page.route('**/api/v1/artifacts/art-card-video*', route => {
      const request = route.request()
      requests.push({
        authorization: request.headers().authorization,
        sessionKey: request.headers()['x-opensquilla-session-key'],
      })
      return route.fulfill({
        status: 200,
        contentType: 'video/webm',
        body: WEBM_TINY,
      })
    })
    await openVideoSeeded(page)

    await expect(page.locator('.msg-artifact-chip')).toHaveCount(0)
    await page.waitForTimeout(200)
    expect(requests).toHaveLength(0)

    await page.getByRole('button', { name: 'Load video preview for sample.webm' }).click()
    const player = page.locator('.msg-video-card__player')
    await expect(player).toBeVisible({ timeout: 10000 })
    await expect(player).toHaveAttribute('controls', '')
    await expect(player).toHaveAttribute('playsinline', '')
    await expect(player).toHaveAttribute('preload', 'metadata')
    expect(requests).toEqual([{
      authorization: 'Bearer video-token-e2e',
      sessionKey: SESSION_KEY,
    }])
  })
})
