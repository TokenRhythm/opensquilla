import { expect, test, type Download, type Page } from '@playwright/test'

const CONTROL_URL = '/control/chat/new'
const HISTORY_IMAGE_DATA = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII='

type CapturedSend = {
  message?: string
  sessionKey?: string
  displayText?: string
  attachments?: Array<Record<string, unknown>>
}

type HistoryAttachmentFixture = 'send' | 'html' | 'image' | 'staged'

type MockRpcOptions = {
  replayHistoryAfterSend?: boolean
  historyAttachmentFixture?: HistoryAttachmentFixture
  historyRequests?: Array<Record<string, unknown>>
}

function wsResponse(id: string, payload: unknown) {
  return JSON.stringify({ type: 'res', id, ok: true, payload })
}

function wsEvent(event: string, payload: Record<string, unknown>) {
  return JSON.stringify({ type: 'event', event, payload })
}

async function mockApprovals(page: Page) {
  await page.route('**/api/approvals', route =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ pending: [] }),
    }))
}

async function mockRpc(page: Page, capturedSends: CapturedSend[], options: MockRpcOptions = {}) {
  const historyMessages: Array<Record<string, unknown>> = []
  await page.routeWebSocket(/\/ws$/, ws => {
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    ws.onMessage(message => {
      try {
        const frame = JSON.parse(String(message))
        if (frame?.type !== 'req') return
        const method = String(frame.method || '')
        if (method === 'connect') {
          ws.send(JSON.stringify({ protocol: 3, policy: { tick_interval_ms: 30000 } }))
          return
        }
        if (method === 'chat.send') {
          const params = (frame.params || {}) as CapturedSend
          capturedSends.push(params)
          if (options.replayHistoryAfterSend) {
            const messageIndex = capturedSends.length
            historyMessages.splice(0, historyMessages.length, {
              role: 'user',
              text: params.displayText || '',
              timestamp: new Date().toISOString(),
              id: `history-user-${messageIndex}`,
              message_id: `history-user-${messageIndex}`,
              attachments: historyAttachmentsFromSend(params, options.historyAttachmentFixture || 'send'),
            })
          }
          ws.send(wsResponse(String(frame.id), {
            sessionKey: params.sessionKey,
            status: 'accepted',
          }))
          if (options.replayHistoryAfterSend) {
            ws.send(wsEvent('session.event.done', {
              key: params.sessionKey || '',
              sessionKey: params.sessionKey || '',
              status: 'succeeded',
              stream_seq: capturedSends.length,
            }))
          }
          return
        }
        if (method === 'chat.history') {
          options.historyRequests?.push(frame.params || {})
          ws.send(wsResponse(String(frame.id), { messages: historyMessages, has_more: false }))
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
          'sessions.list': { sessions: [], has_more: false },
          'sessions.messages.subscribe': {
            subscribed: true,
            replay_complete: true,
            current_stream_seq: 0,
            run_status: 'idle',
          },
          'usage.status': { sessions: [] },
        }

        ws.send(wsResponse(String(frame.id), payloads[method] ?? {}))
      } catch (err) {
        if (!(err instanceof SyntaxError)) throw err
      }
    })
  })
}

function historyAttachmentsFromSend(params: CapturedSend, fixture: HistoryAttachmentFixture): Array<Record<string, unknown>> {
  const first = params.attachments?.[0] || {}
  if (fixture === 'html') {
    return [{
      type: 'text/html',
      name: 'preview.html',
      data: 'PGh0bWw+',
    }]
  }
  if (fixture === 'image') {
    return [{
      type: 'image/png',
      name: 'photo.png',
      data: HISTORY_IMAGE_DATA,
    }]
  }
  if (fixture === 'staged') {
    return [{
      sha256_ref: 'b'.repeat(64),
      name: String(first.name || 'quarterly-report.pdf'),
      mime: String(first.mime || 'application/pdf'),
      size: 2_000_001,
      download_url: `/api/v1/attachments/${'b'.repeat(64)}?token=legacy&sessionKey=legacy&variant=download`,
    }]
  }
  return (params.attachments || []).map(att => ({
    type: att.type,
    name: att.name,
    data: att.data,
  }))
}

async function readDownloadBytes(download: Download): Promise<Buffer> {
  const stream = await download.createReadStream()
  if (!stream) throw new Error('download stream unavailable')
  const chunks: Buffer[] = []
  for await (const chunk of stream) chunks.push(Buffer.from(chunk))
  return Buffer.concat(chunks)
}

async function openMockedChat(page: Page, capturedSends: CapturedSend[], options: MockRpcOptions = {}) {
  await mockApprovals(page)
  await mockRpc(page, capturedSends, options)
  await page.goto(CONTROL_URL)
  await expect(page.locator('.chat-textarea')).toBeVisible()
  await expect(page.locator('.conn-pill.connected')).toBeVisible()
}

async function dropFiles(page: Page, files: Array<{ name: string; type: string; text?: string; size?: number }>) {
  await page.evaluate((fileSpecs) => {
    const dataTransfer = new DataTransfer()
    for (const spec of fileSpecs) {
      const parts = spec.size
        ? [new Uint8Array(spec.size)]
        : [spec.text || 'drag upload']
      dataTransfer.items.add(new File(parts, spec.name, { type: spec.type }))
    }
    const chat = document.querySelector('.chat')
    if (!chat) throw new Error('chat root not found')
    for (const type of ['dragenter', 'dragover', 'drop']) {
      chat.dispatchEvent(new DragEvent(type, {
        bubbles: true,
        cancelable: true,
        dataTransfer,
      }))
    }
  }, files)
}

async function expectDropOverlayCoversChat(page: Page) {
  const layout = await page.evaluate(() => {
    const rect = (box: DOMRect) => ({
      bottom: box.bottom,
      left: box.left,
      right: box.right,
      top: box.top,
    })
    const overlayEl = document.querySelector('.chat-drop-overlay')
    const chat = document.querySelector('.chat')?.getBoundingClientRect()
    const body = document.querySelector('.chat-body')?.getBoundingClientRect()
    const overlay = overlayEl?.getBoundingClientRect()
    const beacon = document.querySelector('.chat-drop-overlay__beacon')?.getBoundingClientRect()
    const overlayStyle = overlayEl ? getComputedStyle(overlayEl) : null
    if (!chat || !body || !overlay || !beacon || !overlayStyle) {
      throw new Error('drop overlay layout target not found')
    }
    return {
      chat: rect(chat),
      body: rect(body),
      overlay: rect(overlay),
      beacon: rect(beacon),
      pointerEvents: overlayStyle.pointerEvents,
    }
  })

  expect(Math.abs(layout.overlay.left - layout.chat.left)).toBeLessThanOrEqual(2)
  expect(Math.abs(layout.overlay.top - layout.chat.top)).toBeLessThanOrEqual(2)
  expect(Math.abs(layout.overlay.right - layout.chat.right)).toBeLessThanOrEqual(2)
  expect(Math.abs(layout.overlay.bottom - layout.chat.bottom)).toBeLessThanOrEqual(2)
  expect(layout.overlay.top).toBeLessThan(layout.body.top)
  expect(layout.overlay.bottom).toBeGreaterThan(layout.body.bottom)
  expect(layout.beacon.left).toBeGreaterThanOrEqual(layout.overlay.left)
  expect(layout.beacon.right).toBeLessThanOrEqual(layout.overlay.right)
  expect(layout.pointerEvents).toBe('none')
}

test.describe('attachment drag upload', () => {
  test('ignores non-file drags and keeps file drag affordance stable across children', async ({ page }) => {
    const capturedSends: CapturedSend[] = []
    await openMockedChat(page, capturedSends)

    await page.evaluate(() => {
      const dataTransfer = new DataTransfer()
      dataTransfer.setData('text/plain', 'not a file')
      const chat = document.querySelector('.chat')
      if (!chat) throw new Error('chat root not found')
      chat.dispatchEvent(new DragEvent('dragenter', { bubbles: true, cancelable: true, dataTransfer }))
      chat.dispatchEvent(new DragEvent('dragover', { bubbles: true, cancelable: true, dataTransfer }))
    })
    await expect(page.locator('.chat-drop-overlay')).toHaveCount(0)

    const nonFileDropPrevented = await page.evaluate(() => {
      const dataTransfer = new DataTransfer()
      dataTransfer.setData('text/uri-list', 'https://example.test/drop-target')
      dataTransfer.setData('text/plain', 'https://example.test/drop-target')
      const chat = document.querySelector('.chat')
      if (!chat) throw new Error('chat root not found')
      const event = new DragEvent('drop', { bubbles: true, cancelable: true, dataTransfer })
      chat.dispatchEvent(event)
      return event.defaultPrevented
    })
    expect(nonFileDropPrevented).toBe(true)
    await expect(page.locator('.attachment-chip')).toHaveCount(0)

    await page.evaluate(() => {
      const dataTransfer = new DataTransfer()
      dataTransfer.items.add(new File(['stable'], 'stable.txt', { type: 'text/plain' }))
      const chat = document.querySelector('.chat')
      const textarea = document.querySelector('.chat-textarea')
      if (!chat || !textarea) throw new Error('drag targets not found')
      chat.dispatchEvent(new DragEvent('dragenter', { bubbles: true, cancelable: true, dataTransfer }))
      textarea.dispatchEvent(new DragEvent('dragenter', { bubbles: true, cancelable: true, dataTransfer }))
      textarea.dispatchEvent(new DragEvent('dragleave', { bubbles: true, cancelable: true, dataTransfer }))
    })
    await expect(page.locator('.chat-drop-overlay')).toBeVisible()
    await expect(page.locator('.chat-drop-overlay')).toContainText('Drop to attach')
    await expectDropOverlayCoversChat(page)

    await page.evaluate(() => {
      const dataTransfer = new DataTransfer()
      dataTransfer.items.add(new File(['stable'], 'stable.txt', { type: 'text/plain' }))
      const chat = document.querySelector('.chat')
      if (!chat) throw new Error('chat root not found')
      chat.dispatchEvent(new DragEvent('drop', { bubbles: true, cancelable: true, dataTransfer }))
    })
    await expect(page.locator('.chat-drop-overlay')).toHaveCount(0)
    await expect(page.locator('.attachment-chip')).toContainText('stable.txt')
    await expect(page.locator('.attachment-chip--busy')).toHaveCount(0)
  })

  test('keeps the drop affordance contained on mobile and honors reduced motion', async ({ page }) => {
    const capturedSends: CapturedSend[] = []
    await page.setViewportSize({ width: 390, height: 780 })
    await page.emulateMedia({ reducedMotion: 'reduce' })
    await openMockedChat(page, capturedSends)

    await page.evaluate(() => {
      const dataTransfer = new DataTransfer()
      dataTransfer.items.add(new File(['mobile'], 'mobile-check.txt', { type: 'text/plain' }))
      const chat = document.querySelector('.chat')
      if (!chat) throw new Error('chat root not found')
      chat.dispatchEvent(new DragEvent('dragenter', { bubbles: true, cancelable: true, dataTransfer }))
      chat.dispatchEvent(new DragEvent('dragover', { bubbles: true, cancelable: true, dataTransfer }))
    })

    await expect(page.locator('.chat-drop-overlay')).toBeVisible()
    await expectDropOverlayCoversChat(page)

    const layout = await page.evaluate(() => {
      const rect = (selector: string) => {
        const el = document.querySelector(selector)
        if (!el) throw new Error(`${selector} not found`)
        const box = el.getBoundingClientRect()
        return {
          bottom: box.bottom,
          left: box.left,
          right: box.right,
          top: box.top,
          width: box.width,
        }
      }
      const beacon = document.querySelector('.chat-drop-overlay__beacon')
      if (!beacon) throw new Error('beacon not found')
      return {
        overlay: rect('.chat-drop-overlay'),
        beacon: rect('.chat-drop-overlay__beacon'),
        copy: rect('.chat-drop-overlay__copy'),
        documentOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
        animationName: getComputedStyle(beacon).animationName,
      }
    })

    expect(layout.documentOverflow).toBeLessThanOrEqual(1)
    expect(layout.beacon.left).toBeGreaterThanOrEqual(layout.overlay.left)
    expect(layout.beacon.right).toBeLessThanOrEqual(layout.overlay.right)
    expect(layout.copy.left).toBeGreaterThanOrEqual(layout.overlay.left)
    expect(layout.copy.right).toBeLessThanOrEqual(layout.overlay.right)
    expect(layout.animationName).toBe('none')
  })

  test('drops and sends a small inline file without leaking local paths', async ({ page }) => {
    const capturedSends: CapturedSend[] = []
    await openMockedChat(page, capturedSends)

    await dropFiles(page, [
      { name: 'small.txt', type: 'text/plain', text: 'hello from inline drop' },
    ])
    await expect(page.locator('.attachment-chip')).toContainText('small.txt')
    await expect(page.locator('.attachment-chip--busy')).toHaveCount(0)

    await page.locator('.chat-send-btn[aria-label="Send"]').click()
    await expect.poll(() => capturedSends.length).toBe(1)
    await expect(page.locator('.msg-attachments .msg-file-chip')).toContainText('small.txt')

    const params = capturedSends[0]
    expect(params.message).toBe('Describe these attachments')
    expect(params.attachments).toHaveLength(1)
    const attachment = params.attachments?.[0] || {}
    expect(attachment).toMatchObject({
      mime: 'text/plain',
      name: 'small.txt',
      type: 'text/plain',
    })
    expect(String(attachment.data || '')).toBeTruthy()
    expect(attachment.file_uuid).toBeUndefined()
    expect(JSON.stringify(params)).not.toContain('/Users/')
    expect(JSON.stringify(params)).not.toContain('small.txt/')
  })

  test('keeps non-image history replay attachments as file chips', async ({ page }) => {
    const capturedSends: CapturedSend[] = []
    const historyRequests: Array<Record<string, unknown>> = []
    await openMockedChat(page, capturedSends, {
      replayHistoryAfterSend: true,
      historyAttachmentFixture: 'html',
      historyRequests,
    })

    await dropFiles(page, [
      { name: 'preview.html', type: 'text/html', text: '<html><body>preview</body></html>' },
    ])
    await expect(page.locator('.attachment-chip')).toContainText('preview.html')
    await expect(page.locator('.attachment-chip__thumb')).toHaveCount(0)

    const historyCallsBeforeSend = historyRequests.length
    await page.locator('.chat-send-btn[aria-label="Send"]').click()
    await expect.poll(() => capturedSends.length).toBe(1)
    await expect.poll(() => historyRequests.length).toBeGreaterThan(historyCallsBeforeSend)

    await expect(page.locator('.msg-attachments .msg-file-chip')).toContainText('preview.html')
    await expect(page.locator('.msg-attachments .msg-thumb')).toHaveCount(0)

    const downloadPromise = page.waitForEvent('download')
    await page.getByRole('button', { name: 'Download preview.html' }).click()
    const download = await downloadPromise
    expect(download.suggestedFilename()).toBe('preview.html')
    expect(await readDownloadBytes(download)).toEqual(Buffer.from('<html>'))
  })

  test('keeps image history replay attachments as thumbnails', async ({ page }) => {
    const capturedSends: CapturedSend[] = []
    const historyRequests: Array<Record<string, unknown>> = []
    await openMockedChat(page, capturedSends, {
      replayHistoryAfterSend: true,
      historyAttachmentFixture: 'image',
      historyRequests,
    })

    await dropFiles(page, [
      { name: 'photo.png', type: 'image/png', text: 'image placeholder' },
    ])
    await expect(page.locator('.attachment-chip')).toContainText('photo.png')

    const historyCallsBeforeSend = historyRequests.length
    await page.locator('.chat-send-btn[aria-label="Send"]').click()
    await expect.poll(() => capturedSends.length).toBe(1)
    await expect.poll(() => historyRequests.length).toBeGreaterThan(historyCallsBeforeSend)

    const thumb = page.locator('.msg-attachments .msg-thumb[alt="photo.png"]')
    await expect(thumb).toBeVisible()
    await expect(thumb).toHaveAttribute('src', `data:image/png;base64,${HISTORY_IMAGE_DATA}`)
    await expect(page.locator('.msg-attachments .msg-file-chip')).toHaveCount(0)
  })

  test('drops a large staged file through the authenticated upload path', async ({ page }) => {
    const capturedSends: CapturedSend[] = []
    const historyRequests: Array<Record<string, unknown>> = []
    const uploadRequests: Array<{ url: string; authorization?: string }> = []
    const downloadRequests: Array<{
      authorization?: string
      sessionKey?: string
      url: string
    }> = []
    await page.addInitScript(() => {
      sessionStorage.setItem('opensquilla.wsToken', 'token-e2e')
    })
    await page.route('**/api/v1/files/upload', route => {
      const request = route.request()
      uploadRequests.push({
        url: request.url(),
        authorization: request.headers().authorization,
      })
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          file_uuid: 'u-e2e-staged',
          filename: 'quarterly-report.pdf',
          mime: 'application/pdf',
          size: 2_000_001,
        }),
      })
    })
    await page.route('**/api/v1/attachments/**', route => {
      const request = route.request()
      downloadRequests.push({
        authorization: request.headers().authorization,
        sessionKey: request.headers()['x-opensquilla-session-key'],
        url: request.url(),
      })
      return route.fulfill({
        status: 200,
        contentType: 'application/pdf',
        headers: {
          'content-disposition': 'attachment; filename="server-quarterly-report.pdf"',
        },
        body: Buffer.from('staged attachment bytes'),
      })
    })
    await openMockedChat(page, capturedSends, {
      replayHistoryAfterSend: true,
      historyAttachmentFixture: 'staged',
      historyRequests,
    })

    const longName = 'quarterly-report-with-a-very-long-name-that-must-not-overflow-the-composer.pdf'
    await dropFiles(page, [
      { name: longName, type: 'application/pdf', size: 2_000_001 },
    ])
    await expect(page.locator('.attachment-chip')).toContainText(longName)
    await expect(page.locator('.attachment-chip--busy')).toHaveCount(0)
    await expect.poll(() => uploadRequests.length).toBe(1)

    expect(uploadRequests[0].authorization).toBe('Bearer token-e2e')
    expect(new URL(uploadRequests[0].url).search).not.toContain('token')

    const layout = await page.evaluate(() => {
      const chip = document.querySelector('.attachment-chip')?.getBoundingClientRect()
      const composer = document.querySelector('.chat-composer-inner')?.getBoundingClientRect()
      return {
        chipRight: chip?.right || 0,
        composerRight: composer?.right || 0,
        documentOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      }
    })
    expect(layout.chipRight).toBeLessThanOrEqual(layout.composerRight + 1)
    expect(layout.documentOverflow).toBeLessThanOrEqual(1)

    const historyCallsBeforeSend = historyRequests.length
    await page.locator('.chat-textarea').fill('summarize')
    await page.locator('.chat-send-btn[aria-label="Send"]').click()
    await expect.poll(() => capturedSends.length).toBe(1)
    await expect.poll(() => historyRequests.length).toBeGreaterThan(historyCallsBeforeSend)
    await expect(page.locator('.msg-attachments .msg-file-chip')).toContainText(longName)
    await expect(page.locator('.msg-attachments .msg-thumb')).toHaveCount(0)

    const attachment = capturedSends[0].attachments?.[0] || {}
    expect(attachment).toMatchObject({
      file_uuid: 'u-e2e-staged',
      mime: 'application/pdf',
      name: longName,
      type: 'application/pdf',
    })
    expect(attachment.data).toBeUndefined()
    expect(JSON.stringify(capturedSends[0])).not.toContain('/Users/')

    const downloadPromise = page.waitForEvent('download')
    await page.getByRole('button', { name: `Download ${longName}` }).click()
    const download = await downloadPromise
    expect(download.suggestedFilename()).toBe('server-quarterly-report.pdf')
    expect(await readDownloadBytes(download)).toEqual(Buffer.from('staged attachment bytes'))
    expect(downloadRequests).toHaveLength(1)
    expect(downloadRequests[0].authorization).toBe('Bearer token-e2e')
    expect(downloadRequests[0].sessionKey).toBe(capturedSends[0].sessionKey)
    const requested = new URL(downloadRequests[0].url)
    expect(requested.searchParams.get('variant')).toBe('download')
    expect(requested.searchParams.has('token')).toBe(false)
    expect(requested.searchParams.has('sessionKey')).toBe(false)
  })
})
