import { expect, test, type Page } from '@playwright/test'
import { createHash } from 'node:crypto'
import { helloOkResponse } from './support/gateway-fixture'
import {
  chatHistoryPayload,
  sessionMessagesHydratePayload,
  sessionMessagesSnapshotPayload,
  sessionMessagesSubscribePayload,
} from './support/session-read-fixtures'

const SESSION_KEY = 'agent:main:webchat:e2e-execution-log'
const HANDLE = `tr-${'a'.repeat(32)}`
const PREFIX_LENGTH = 9 * 1024 * 1024
const MIDDLE = '🙂\nMIDDLE_ERROR: 故障\n'
const PREVIEW = 'Execution failed. Output preview only.'

async function mockExecutionLog(page: Page, options: { large?: boolean; holdFirstPage?: boolean } = {}) {
  const offsets: number[] = []
  // Put an astral character exactly at the first page boundary. Newlines keep
  // the ordinary log within the inline rendering budget, independently of the
  // separate >8 MiB download case below.
  const prefix = options.large ? 'x'.repeat(PREFIX_LENGTH) : `${'x'.repeat(79)}\n`.repeat(149) + 'x'.repeat(79)
  const tail = MIDDLE + (options.large ? 'z'.repeat(14000) : `${'z'.repeat(79)}\n`.repeat(175))
  const log = prefix + tail
  const tailPoints = Array.from(tail)
  let firstReply: (() => void) | undefined
  await page.addInitScript(() => window.localStorage.setItem('opensquilla-locale', 'en'))
  await page.routeWebSocket(/\/ws$/, ws => {
    ws.onMessage(message => {
      const frame = JSON.parse(String(message))
      if (frame.type !== 'req') return
      const reply = (payload: unknown) => ws.send(JSON.stringify({ type: 'res', id: frame.id, ok: true, payload }))
      const key = String(frame.params?.key || SESSION_KEY)
      switch (frame.method) {
        case 'connect': ws.send(helloOkResponse()); return
        case 'chat.history':
          reply(chatHistoryPayload([{
            role: 'assistant', id: 'assistant-log', text: 'Recovered after inspecting the test output.',
            timestamp: 1000,
            tool_calls: [{
              tool_use_id: 'execution-call', name: 'exec', groupId: 'execution-group',
              input: { command: 'synthetic-test' }, result: PREVIEW,
              result_truncated: true, result_original_chars: prefix.length + tailPoints.length,
              execution_log_handle: HANDLE, is_error: true, execution_status: { status: 'error' },
            }],
            timeline: [{ type: 'tool-group', groupId: 'execution-group' }],
          }]))
          return
        case 'sessions.messages.subscribe': reply(sessionMessagesSubscribePayload(key)); return
        case 'sessions.messages.snapshot': reply(sessionMessagesSnapshotPayload(key)); return
        case 'sessions.messages.hydrate': reply(sessionMessagesHydratePayload(key)); return
        case 'sessions.executionLog.read': {
          expect(frame.params.sessionKey).toBe(SESSION_KEY)
          expect(frame.params.handle).toBe(HANDLE)
          expect(frame.params.limit).toBe(12000)
          const start = Number(frame.params.offset)
          offsets.push(start)
          // The backend indexes Unicode code points; an astral character must not
          // move the next page by two positions as a JavaScript string would.
          const head = prefix.slice(start, start + 12000)
          const suffixOffset = Math.max(0, start - prefix.length)
          const content = head + tailPoints.slice(suffixOffset, suffixOffset + 12000 - head.length).join('')
          const end = start + Array.from(content).length
          const total = prefix.length + tailPoints.length
          const sendPage = () => reply({
            storage_kind: 'execution_log', handle: HANDLE, offset: start, content,
            returned_chars: Array.from(content).length, chars: total,
            next_offset: end < total ? end : null, complete: true,
          })
          if (options.holdFirstPage && offsets.length === 1) firstReply = sendPage
          else sendPage()
          return
        }
        default: reply({})
      }
    })
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
  })
  return { offsets, log, releaseFirstPage: () => firstReply?.() }
}

async function expandExecution(page: Page) {
  const activity = page.getByTestId('assistant-activity')
  await expect(activity).toBeVisible()
  await activity.locator('.assistant-activity__summary').click()
  const row = activity.locator('.tool-row').first()
  await expect(row).toBeVisible()
  if (await row.getAttribute('aria-expanded') !== 'true') await row.click()
  return activity
}

test('execution logs page inline without losing Unicode or confusing the retained model preview', async ({ page, context }) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write'])
  const { offsets, log, releaseFirstPage } = await mockExecutionLog(page, { holdFirstPage: true })
  await page.goto(`/control/chat?session=${encodeURIComponent(SESSION_KEY)}`)
  expect(offsets).toHaveLength(0)
  const activity = await expandExecution(page)
  const visible = activity.locator('.activity-code-details__output')
  await expect.poll(() => offsets).toEqual([0])
  await expect(visible).toContainText(PREVIEW)
  releaseFirstPage()
  await expect(visible).toHaveText(Array.from(log).slice(0, 12000).join(''))
  await expect(visible).not.toContainText(PREVIEW)
  const viewport = activity.locator('.activity-code-details__viewport')
  await viewport.focus()
  await viewport.press('Control+End')
  await expect.poll(() => offsets).toEqual([0, 12000])
  await expect(visible).toContainText(MIDDLE)
  await activity.getByTestId('activity-tool-detail-copy').click()
  await expect.poll(() => offsets).toEqual([0, 12000, 24000])
  await expect(visible).toHaveText(log)
  await expect(activity.getByTestId('activity-tool-detail-copy')).toHaveAttribute('aria-label', 'Copied')
  const visibleText = `$ synthetic-test\n\n${log}`
  // Windows' native clipboard represents line breaks as CRLF. Keep the
  // returned bytes exact; only the expected platform representation changes.
  const windowsClipboard = await page.evaluate(() => navigator.platform.startsWith('Win'))
  const expectedClipboard = windowsClipboard ? visibleText.replace(/\r?\n/g, '\r\n') : visibleText
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(expectedClipboard)
  await viewport.focus()
  await viewport.press('Control+End')
  expect(offsets).toEqual([0, 12000, 24000])
  await expect(page.locator('.tool-sheet')).toHaveCount(0)
})

test('oversized execution logs remain bounded inline and download the complete retained bytes', async ({ page }) => {
  const { offsets, log } = await mockExecutionLog(page, { large: true })
  // File-picker interaction is native and cannot be driven by Playwright.
  // Capture only that boundary; real UI, RPC pagination and stream writer run.
  await page.addInitScript(() => {
    const capture = { name: '', digest: '', bytes: 0, closed: false, aborted: false }
    Object.assign(window, { executionDownload: capture, showSaveFilePicker: async ({ suggestedName }: { suggestedName: string }) => {
      capture.name = suggestedName
      const chunks: Uint8Array[] = []
      return { createWritable: async () => ({
        write: async (chunk: Uint8Array) => { chunks.push(chunk); capture.bytes += chunk.length },
        close: async () => {
          const bytes = await new Blob(chunks).arrayBuffer()
          capture.digest = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)))
            .map(value => value.toString(16).padStart(2, '0')).join('')
          capture.closed = true
        },
        abort: async () => { capture.aborted = true },
      }) }
    } })
  })
  await page.goto(`/control/chat?session=${encodeURIComponent(SESSION_KEY)}`)
  const activity = await expandExecution(page)
  await expect(activity.getByTestId('tool-content-truncated')).toBeVisible()
  expect((await activity.locator('.activity-code-details__output').textContent())?.length).toBeLessThanOrEqual(8192)
  await activity.getByTestId('activity-tool-detail-download').click()
  await expect.poll(() => page.evaluate(() => (window as unknown as { executionDownload: { closed: boolean } }).executionDownload.closed)).toBe(true)
  const saved = await page.evaluate(() => (window as unknown as { executionDownload: Record<string, unknown> }).executionDownload)
  expect(saved).toEqual({ name: 'command-output.txt', digest: createHash('sha256').update(log).digest('hex'), bytes: Buffer.byteLength(log), closed: true, aborted: false })
  expect(offsets).toEqual([0, ...Array.from({ length: Math.ceil((log.length - 1) / 12000) }, (_, index) => index * 12000)])
  expect((await activity.locator('.activity-code-details__output').textContent())?.length).toBeLessThanOrEqual(8192)
})

test('shows PTY fallback in the collapsed activity row', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  const pageErrors: string[] = []
  page.on('pageerror', error => pageErrors.push(error.message))
  const sessionKey = 'agent:main:webchat:e2e-execution-io'
  const fallbackResult = JSON.stringify({
    status: 'ok',
    execution_id: 'fallback-e2e',
    io_mode_requested: 'pty',
    io_mode_used: 'pipe',
    fallback_reason: 'PTY backend unavailable',
    output: 'pipe output',
  })

  await page.addInitScript(() => window.localStorage.setItem('opensquilla-locale', 'en'))
  await page.routeWebSocket(/\/ws$/, ws => {
    ws.onMessage(message => {
      const frame = JSON.parse(String(message))
      if (frame.type !== 'req') return
      const reply = (payload: unknown) => ws.send(JSON.stringify({ type: 'res', id: frame.id, ok: true, payload }))
      switch (frame.method) {
        case 'connect': ws.send(helloOkResponse()); return
        case 'chat.history':
          reply(chatHistoryPayload([{
            role: 'assistant', id: 'assistant-execution-io', text: 'The command used a safe fallback.', timestamp: 1000,
            tool_calls: [{
              tool_use_id: 'fallback-call', name: 'exec_command', groupId: 'execution-io-group',
              input: { command: 'printf pipe output', io_mode: 'pty' }, result: fallbackResult,
              is_error: false, execution_status: { status: 'success' },
            }],
            timeline: [{ type: 'tool-group', groupId: 'execution-io-group' }],
          }]))
          return
        case 'sessions.messages.subscribe': reply(sessionMessagesSubscribePayload(sessionKey)); return
        case 'sessions.messages.snapshot': reply(sessionMessagesSnapshotPayload(sessionKey)); return
        case 'sessions.messages.hydrate': reply(sessionMessagesHydratePayload(sessionKey)); return
        default: reply({})
      }
    })
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
  })

  await page.goto(`/control/chat?session=${encodeURIComponent(sessionKey)}`)
  const activity = page.getByTestId('assistant-activity')
  await expect(activity).toBeVisible()
  await activity.locator('.assistant-activity__summary').click()
  const row = activity.locator('.tool-row').first()
  await expect(row).toBeVisible()
  await expect(row).toContainText('TTY unavailable; using a regular pipe')
  await expect(row).not.toContainText('Real terminal (TTY)')
  await expect(row).toHaveAttribute('aria-expanded', 'false')
  await row.screenshot({ path: 'output/playwright/execution-io-fallback-row.png' })
  await row.click()
  await expect(activity.locator('.activity-tool-details__execution-io'))
    .toContainText('TTY unavailable; using a regular pipe')
  await expect(activity.locator('.activity-tool-details__execution-io-reason'))
    .toContainText('PTY backend unavailable')
  await expect(activity.locator('.activity-code-details__output')).toHaveText('pipe output')
  await expect(page.locator('.tool-sheet')).toHaveCount(0)
  await page.screenshot({ path: 'output/playwright/execution-io-fallback.png', fullPage: true })
  expect(pageErrors).toEqual([])
})
