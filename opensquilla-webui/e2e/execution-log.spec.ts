import { expect, test, type Page } from '@playwright/test'
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
const MIDDLE = 'MIDDLE_ERROR: 故障🙂\n'
const LOG = 'x'.repeat(PREFIX_LENGTH) + MIDDLE + 'z'.repeat(14000)
const PREVIEW = 'Execution failed. Output preview only.'

async function mockExecutionLog(page: Page) {
  const offsets: number[] = []
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
          const prefix = 'x'.repeat(Math.min(12000, Math.max(0, PREFIX_LENGTH - start)))
          const suffixOffset = Math.max(0, start - PREFIX_LENGTH)
          const content = prefix + Array.from(LOG.slice(PREFIX_LENGTH))
            .slice(suffixOffset, suffixOffset + 12000 - prefix.length).join('')
          const end = start + Array.from(content).length
          const total = LOG.length - 1
          reply({
            storage_kind: 'execution_log', handle: HANDLE, offset: start, content,
            returned_chars: Array.from(content).length, chars: total,
            next_offset: end < total ? end : null, complete: true,
          })
          return
        }
        default: reply({})
      }
    })
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
  })
  return offsets
}

test('execution logs are fetched by page independently of the model result preview', async ({ page, context }) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write'])
  const offsets = await mockExecutionLog(page)
  await page.goto(`/control/chat?session=${encodeURIComponent(SESSION_KEY)}`)
  const activity = page.getByTestId('assistant-activity')
  await expect(activity).toBeVisible()
  await expect(activity.locator('.assistant-activity__summary')).toContainText('Completed')
  await activity.locator('.assistant-activity__summary').click()
  const row = activity.locator('.tool-row').first()
  await expect(row).toBeVisible()
  if (await row.getAttribute('aria-expanded') !== 'true') await row.click()
  await activity.locator('.activity-tool-details__view, .activity-tool-details__hit-target, .activity-tool-details__fallback').first().click()
  const modal = page.locator('.tool-sheet')
  await expect(modal).toBeVisible()
  await expect(modal.locator('.tool-sheet__pre')).toContainText(PREVIEW)
  expect(offsets).toHaveLength(0)
  await modal.getByRole('button', { name: 'View execution log', exact: true }).click()
  await expect(modal.locator('[role="status"]')).toContainText('Characters 1–12000')
  await modal.getByRole('spinbutton').fill(String(PREFIX_LENGTH))
  await modal.getByRole('button', { name: 'Go', exact: true }).click()
  const visible = modal.locator('.tool-sheet__pre')
  await expect(visible).toContainText(MIDDLE)
  expect(Array.from(await visible.textContent() || '').length).toBe(12000)
  await modal.getByRole('button', { name: 'Copy this page', exact: true }).click()
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(await visible.textContent())
  await modal.getByRole('button', { name: 'Next', exact: true }).click()
  await expect(visible).not.toContainText(MIDDLE)
  await expect(modal.getByRole('button', { name: 'Next', exact: true })).toBeDisabled()
  expect(offsets).toEqual([0, PREFIX_LENGTH, PREFIX_LENGTH + 12000])
  await modal.getByRole('button', { name: 'Tool result', exact: true }).click()
  await expect(visible).toContainText(PREVIEW)
  await expect(visible).not.toContainText(MIDDLE)
})
