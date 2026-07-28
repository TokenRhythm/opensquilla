import { expect, test, type Page } from '@playwright/test'

const CONTROL_URL = '/control/'
const SESSION_KEY = 'agent:main:webchat:e2e-assistant-activity'
const LIFECYCLE_SESSION_KEY = 'agent:main:webchat:e2e-assistant-activity-lifecycle'
const LIFECYCLE_TASK_ID = 'task-e2e-assistant-activity-lifecycle'

interface ActivityFixture {
  failed?: boolean
}

function wsResponse(id: string | number | undefined, payload: unknown) {
  return JSON.stringify({ type: 'res', id, ok: true, payload })
}

function wsEvent(event: string, payload: unknown) {
  return JSON.stringify({ type: 'event', event, payload })
}

async function mockActivityHistory(page: Page, fixture: ActivityFixture = {}) {
  await page.routeWebSocket(/\/ws$/, ws => {
    ws.onMessage(message => {
      let frame: Record<string, unknown>
      try {
        frame = JSON.parse(String(message)) as Record<string, unknown>
      } catch {
        return
      }
      if (frame.type !== 'req' || frame.id === undefined) return
      if (frame.method === 'connect') {
        ws.send(JSON.stringify({ protocol: 3, policy: {} }))
        return
      }
      if (frame.method === 'chat.history') {
        ws.send(JSON.stringify({
          type: 'res',
          id: frame.id,
          ok: true,
          payload: {
            messages: [{
              role: 'assistant',
              text: 'The canonical answer is complete.',
              id: `assistant-activity-${fixture.failed ? 'failed' : 'success'}`,
              timestamp: Math.floor(Date.now() / 1000) - 30,
              reasoning_content: 'I compared the available evidence before answering.',
              tool_calls: [{
                tool_use_id: 'activity-search',
                name: 'web_search',
                groupId: 'activity-group',
                input: { query: 'OpenSquilla activity' },
                result: fixture.failed ? 'Search service unavailable' : 'One verified result',
                is_error: fixture.failed === true,
                execution_status: { status: fixture.failed ? 'error' : 'success' },
              }],
              timeline: [
                { type: 'text', raw: 'Non-canonical streamed prefix.' },
                { type: 'tool-group', groupId: 'activity-group' },
                { type: 'text', raw: 'Non-canonical streamed suffix.' },
              ],
            }],
            has_more: false,
          },
        }))
        return
      }
      ws.send(JSON.stringify({ type: 'res', id: frame.id, ok: true, payload: {} }))
    })
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
  })
}

async function mockControlledActivityLifecycle(page: Page) {
  let sendFrame: ((frame: string) => void) | null = null
  let streamSeq = 3
  let settled = false
  let acceptedUserMessageId = 'activity-lifecycle-user'

  const emit = (event: string, payload: Record<string, unknown>) => {
    if (!sendFrame) throw new Error('activity lifecycle websocket is not connected')
    sendFrame(wsEvent(event, {
      key: LIFECYCLE_SESSION_KEY,
      task_id: LIFECYCLE_TASK_ID,
      stream_seq: streamSeq++,
      ...payload,
    }))
  }

  await page.addInitScript(() => {
    window.localStorage.setItem('opensquilla-locale', 'en')
  })
  await page.route('**/api/approvals', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ pending: [] }),
  }))
  await page.routeWebSocket(/\/ws$/, ws => {
    sendFrame = frame => ws.send(frame)
    ws.send(wsEvent('connect.challenge', {}))
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
        ws.send(JSON.stringify({
          protocol: 3,
          policy: { tick_interval_ms: 30_000, webui_stream_idle_grace_ms: 1_260_000 },
        }))
        return
      }
      if (method === 'chat.send') {
        const params = frame.params && typeof frame.params === 'object'
          ? frame.params as Record<string, unknown>
          : {}
        const requestedClientMessageId = params.clientMessageId
        if (typeof requestedClientMessageId === 'string' && requestedClientMessageId) {
          acceptedUserMessageId = requestedClientMessageId
        }
        ws.send(wsResponse(frame.id as string | number | undefined, {
          accepted: true,
          session: LIFECYCLE_SESSION_KEY,
          sessionKey: LIFECYCLE_SESSION_KEY,
          task_id: LIFECYCLE_TASK_ID,
          stream_seq: 1,
          user_message_id: acceptedUserMessageId,
        }))
        ws.send(wsEvent('task.running', {
          key: LIFECYCLE_SESSION_KEY,
          task_id: LIFECYCLE_TASK_ID,
          stream_seq: 1,
        }))
        ws.send(wsEvent('session.event.state_change', {
          key: LIFECYCLE_SESSION_KEY,
          task_id: LIFECYCLE_TASK_ID,
          stream_seq: 2,
          to_state: 'thinking',
        }))
        return
      }
      const messages = settled
        ? [{
            role: 'user',
            text: 'Inspect, draft, verify, and answer.',
            id: acceptedUserMessageId,
            message_id: acceptedUserMessageId,
            timestamp: Math.floor(Date.now() / 1000) - 30,
          }, {
            role: 'assistant',
            text: 'Final verified answer.',
            id: 'activity-lifecycle-assistant',
            message_id: 'activity-lifecycle-assistant',
            timestamp: Math.floor(Date.now() / 1000),
            tool_calls: [{
              tool_use_id: 'activity-inspect',
              name: 'read_file',
              groupId: 'activity-inspect-group',
              input: { path: '/private/project/chat.ts' },
              result: 'read',
              execution_status: { status: 'success' },
            }, {
              tool_use_id: 'activity-verify',
              name: 'bash_exec',
              groupId: 'activity-verify-group',
              input: { command: 'npm test' },
              result: 'verified',
              execution_status: { status: 'success' },
            }],
            timeline: [
              { type: 'tool-group', groupId: 'activity-inspect-group' },
              { type: 'text', raw: 'Draft candidate.' },
              { type: 'tool-group', groupId: 'activity-verify-group' },
              { type: 'text', raw: 'Final verified answer.' },
            ],
          }]
        : []
      const payloads: Record<string, unknown> = {
        'agents.list': { agents: [] },
        'chat.history': { messages, has_more: false, canonical_complete: true },
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
      ws.send(wsResponse(
        frame.id as string | number | undefined,
        payloads[method] ?? {},
      ))
    })
  })

  return {
    emit,
    finish() {
      settled = true
      emit('session.event.done', {
        text: 'Final verified answer.',
        model: 'test/activity',
        input_tokens: 12,
        output_tokens: 4,
      })
    },
  }
}

test.describe('Completed assistant activity disclosure', () => {
  test('keeps the canonical answer visible and supports keyboard disclosure', async ({ page }) => {
    await mockActivityHistory(page)
    await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(SESSION_KEY))
    await page.waitForSelector('.conn-pill', { timeout: 10000 })

    const activity = page.getByTestId('assistant-activity')
    await expect(activity).toBeVisible()
    await expect(activity).toHaveAttribute('data-share-expanded', 'false')
    // Completed history without timing metadata stays compact and never falls
    // back to an arbitrary activity-item count.
    await expect(activity.locator('.assistant-activity__summary')).toContainText('Completed')
    await expect(activity.locator('.assistant-activity__summary')).not.toContainText('item')

    const answer = page.getByText('The canonical answer is complete.', { exact: true })
    await expect(answer).toBeVisible()
    await expect(answer).toHaveText('The canonical answer is complete.')
    const processPrefix = activity.getByText('Non-canonical streamed prefix.', {
      exact: true,
    })
    await expect(processPrefix).toBeHidden()
    await expect(page.getByText('Non-canonical streamed suffix.')).toHaveCount(0)

    const row = activity.locator('.tool-row[data-op="web.search"]')
    await expect(row).toBeHidden()

    const summary = activity.locator('.assistant-activity__summary')
    const summaryArrow = summary.locator('.assistant-activity__summary-arrow')
    await expect(summaryArrow).toHaveCount(1)
    const idleSummaryStyles = await summary.evaluate((element) => {
      const summaryStyle = getComputedStyle(element)
      const arrow = element.querySelector<HTMLElement>('.assistant-activity__summary-arrow')
      return {
        backgroundColor: summaryStyle.backgroundColor,
        borderTopWidth: summaryStyle.borderTopWidth,
        boxShadow: summaryStyle.boxShadow,
        color: summaryStyle.color,
        arrowOpacity: arrow ? getComputedStyle(arrow).opacity : '',
      }
    })
    expect(idleSummaryStyles).toMatchObject({
      backgroundColor: 'rgba(0, 0, 0, 0)',
      borderTopWidth: '0px',
      boxShadow: 'none',
      // The chevron rests faintly visible so the disclosure affordance is
      // discoverable without hover.
      arrowOpacity: '0.34',
    })
    await summary.hover()
    await expect(summaryArrow).toHaveCSS('opacity', '0.8')
    const hoverSummaryStyles = await summary.evaluate((element) => {
      const summaryStyle = getComputedStyle(element)
      const arrow = element.querySelector<HTMLElement>('.assistant-activity__summary-arrow')
      return {
        backgroundColor: summaryStyle.backgroundColor,
        boxShadow: summaryStyle.boxShadow,
        color: summaryStyle.color,
        arrowOpacity: arrow ? Number.parseFloat(getComputedStyle(arrow).opacity) : 0,
      }
    })
    expect(hoverSummaryStyles.backgroundColor).toBe('rgba(0, 0, 0, 0)')
    expect(hoverSummaryStyles.boxShadow).toBe('none')
    expect(hoverSummaryStyles.color).not.toBe(idleSummaryStyles.color)
    expect(hoverSummaryStyles.arrowOpacity).toBeGreaterThan(0)
    await summary.press('Enter')
    await expect(summary).toHaveAttribute('aria-expanded', 'true')
    await expect(activity).toHaveAttribute('data-share-expanded', 'true')
    await expect(row).toBeVisible()
    await expect(processPrefix).toBeVisible()
    const reasoningFold = activity.locator('details.thinking-fold')
    await expect(reasoningFold).not.toHaveAttribute('open', '')
    await expect(reasoningFold.locator('.thinking-fold__body')).toBeHidden()
    await reasoningFold.locator('summary').click()
    await expect(reasoningFold.locator('.thinking-fold__body')).toContainText(
      'I compared the available evidence before answering.',
    )

    await expect(row).toHaveAttribute('aria-expanded', 'false')
    await expect(activity.locator('.activity-tool-details')).toHaveCount(0)
    await expect(row.locator('.tool-row__activity-icon')).toHaveCount(1)
    await expect(row.locator('.tool-row__bullet')).toHaveCount(0)
    const rowArrow = row.locator('.tool-row__activity-arrow')
    await expect(rowArrow).toHaveCount(1)
    await expect(rowArrow).toHaveCSS('opacity', '0')
    await row.hover()
    await expect(rowArrow).toHaveCSS('opacity', '0.8')

    await row.click()
    await expect(row).toHaveAttribute('aria-expanded', 'true')
    const details = activity.locator('.activity-tool-details')
    await expect(details).toBeVisible()
    await expect(details).toContainText('OpenSquilla activity')
    await expect(details).not.toContainText('view details')
    await expect(details.locator('.activity-tool-details__summary')).toHaveCount(1)
    const detailTrigger = details.locator('.activity-tool-details__hit-target')
    await expect(detailTrigger).toHaveCount(1)
    await expect(detailTrigger).toHaveAttribute('data-share-control', '')
    await expect(detailTrigger).toHaveAttribute('aria-label', /view details/i)
    await expect(details).not.toContainText('INPUT')
    await expect(details).not.toContainText('RESULT')
    await expect(details).not.toContainText('/private/')
    await expect(activity.locator('.tool-row-section')).toHaveCount(0)
    const flatStyles = await activity.evaluate((element) => {
      const readAll = (selector: string) =>
        Array.from(element.querySelectorAll<HTMLElement>(selector)).map((target) => {
          const style = getComputedStyle(target)
          return {
            backgroundColor: style.backgroundColor,
            borderWidths: [
              style.borderTopWidth,
              style.borderRightWidth,
              style.borderBottomWidth,
              style.borderLeftWidth,
            ],
            borderRadius: style.borderRadius,
            boxShadow: style.boxShadow,
          }
        })
      return {
        cards: readAll('.step-card'),
        rows: readAll('.tool-row'),
        details: readAll('.activity-tool-details'),
      }
    })
    for (const style of [
      ...flatStyles.cards,
      ...flatStyles.rows,
      ...flatStyles.details,
    ]) {
      expect(style).toMatchObject({
        backgroundColor: 'rgba(0, 0, 0, 0)',
        borderWidths: ['0px', '0px', '0px', '0px'],
        borderRadius: '0px',
        boxShadow: 'none',
      })
    }

    await summary.press('Space')
    await expect(summary).toHaveAttribute('aria-expanded', 'false')
    await expect(activity).toHaveAttribute('data-share-expanded', 'false')
    expect(await summary.evaluate(element => document.activeElement === element)).toBe(true)
    await expect(answer).toBeVisible()
  })

  test('omits failed work from the activity disclosure', async ({ page }) => {
    await mockActivityHistory(page, { failed: true })
    await page.setViewportSize({ width: 320, height: 844 })
    await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(`${SESSION_KEY}-failed`))
    await page.waitForSelector('.conn-pill', { timeout: 10000 })

    const activity = page.getByTestId('assistant-activity')
    await expect(activity).toBeVisible()
    await expect(activity).toHaveAttribute('data-share-expanded', 'false')
    await expect(activity).not.toContainText('failure recovered')

    const errorRow = activity.locator('.tool-row--error')
    await expect(errorRow).toHaveCount(0)
    const summary = activity.locator('.assistant-activity__summary')
    await summary.press('Enter')
    await expect(activity).toHaveAttribute('data-share-expanded', 'true')
    await expect(errorRow).toHaveCount(0)
    await expect(activity).not.toContainText('Search service unavailable')
    await expect(activity.locator('.tool-row-section--error')).toHaveCount(0)
    await expect(
      page.getByText('The canonical answer is complete.', { exact: true }),
    ).toBeVisible()

    await activity.locator('.assistant-activity__label').evaluate((element) => {
      element.textContent = 'Sehr lange lokalisierte Aktivitätszusammenfassung'
    })
    const summaryOverflow = await summary.evaluate(element =>
      element.scrollWidth - element.clientWidth,
    )
    expect(summaryOverflow).toBeLessThanOrEqual(1)
    const pageOverflow = await page.evaluate(() =>
      document.documentElement.scrollWidth - document.documentElement.clientWidth,
    )
    expect(pageOverflow).toBeLessThanOrEqual(1)
  })
})

test.describe('Live assistant activity lifecycle', () => {
  test('moves draft text back into activity when a later tool starts, then settles', async ({ page }) => {
    await page.emulateMedia({ reducedMotion: 'no-preference' })
    const lifecycle = await mockControlledActivityLifecycle(page)
    await page.goto(
      CONTROL_URL + 'chat?session=' + encodeURIComponent(LIFECYCLE_SESSION_KEY),
    )
    await expect(page.locator('.conn-pill.connected')).toBeVisible({ timeout: 10000 })

    await page.locator('.chat-textarea').fill('Inspect, draft, verify, and answer.')
    await page.locator('.chat-send-btn[aria-label="Send"]').click()

    const liveActivity = page.locator('.assistant-activity--live')
    await expect(liveActivity).toBeVisible()
    await expect(page.locator('.work-card')).toHaveCount(0)
    const liveSummary = liveActivity.locator('.assistant-activity__live-head')
    await expect(liveSummary).toHaveAttribute('aria-expanded', 'false')
    await liveSummary.click()
    await expect(liveSummary).toHaveAttribute('aria-expanded', 'true')
    const liveStatus = liveActivity.locator('.assistant-activity__live-label')
    await expect(liveStatus).toHaveText('Working')
    await expect(liveStatus).toHaveAttribute('role', 'status')
    await expect(liveStatus).toHaveAttribute('aria-live', 'polite')
    await expect(liveStatus).toHaveAttribute('aria-atomic', 'true')
    await expect(liveActivity.getByText('Working', { exact: true })).toHaveCount(1)
    // The phase label is the only polite live region. Failed work is omitted
    // from the disclosure, so it must not mount a second announcement region.
    await expect(liveActivity.locator('[role="status"]')).toHaveCount(1)
    await expect(liveActivity.locator('.assistant-activity__live-failure')).toHaveCount(0)
    await expect(liveActivity.locator('.assistant-activity-status__row')).toHaveCount(0)
    const liveMotion = await liveActivity.evaluate((element) => ({
      dot: getComputedStyle(
        element.querySelector<HTMLElement>('.assistant-activity__live-dot')!,
      ).animationName,
      label: getComputedStyle(
        element.querySelector<HTMLElement>('.assistant-activity__live-label')!,
      ).animationName,
    }))
    // The pulsing dot is the single working signal; the label is deliberately
    // static (the shimmer gradient was removed as visual noise).
    expect(liveMotion.dot).not.toBe('none')
    expect(liveMotion.label).toBe('none')

    lifecycle.emit('session.event.tool_use_start', {
      tool_use_id: 'activity-inspect',
      name: 'read_file',
      input: { path: '/private/project/chat.ts' },
    })
    const inspectRow = liveActivity.locator('.tool-row[data-op="file.inspect"]')
    await expect(inspectRow).toBeVisible()
    await expect(inspectRow).toHaveAttribute('aria-expanded', 'false')
    await expect(inspectRow.locator('.tool-row__activity-arrow')).toHaveCount(1)
    await expect(inspectRow.locator('.tool-row__bullet')).toHaveCount(0)
    await expect(liveActivity).not.toContainText('/private/project/chat.ts')
    await expect(liveStatus).toHaveText('Working')
    // A cluster still in flight reads in the present tense; it settles into
    // the past tense once its result lands.
    await expect(liveActivity.getByText('Inspecting files', { exact: true })).toHaveCount(1)

    lifecycle.emit('session.event.tool_result', {
      tool_use_id: 'activity-inspect',
      name: 'read_file',
      input: { path: '/private/project/chat.ts' },
      result: 'read',
      execution_status: { status: 'success' },
    })
    await expect(liveActivity.getByText('Inspected files', { exact: true })).toHaveCount(1)
    lifecycle.emit('session.event.text_delta', { text: 'Draft candidate.' })

    const answerCandidate = page.locator('.live-answer-candidate')
    await expect(answerCandidate).toHaveText('Draft candidate.')
    await expect(liveActivity.getByText('Draft candidate.', { exact: true })).toHaveCount(0)
    await expect(liveStatus).toHaveText('Writing the answer')
    await expect(
      liveActivity.getByText('Writing the answer', { exact: true }),
    ).toHaveCount(1)
    await expect(liveActivity.locator('.assistant-activity-status__row')).toHaveCount(0)

    lifecycle.emit('session.event.tool_use_start', {
      tool_use_id: 'activity-verify',
      name: 'bash_exec',
      input: { command: 'npm test' },
    })
    await expect(answerCandidate).toHaveCount(0)
    await expect(liveActivity.getByText('Draft candidate.', { exact: true })).toBeVisible()
    await expect(liveActivity.locator('.tool-row[data-op="command.run"]')).toBeVisible()
    await expect(liveStatus).toHaveText('Working')
    await expect(liveActivity.getByText('Running commands', { exact: true })).toHaveCount(1)

    lifecycle.emit('session.event.tool_result', {
      tool_use_id: 'activity-verify',
      name: 'bash_exec',
      input: { command: 'npm test' },
      result: 'verified',
      execution_status: { status: 'success' },
    })
    lifecycle.emit('session.event.text_delta', { text: 'Final verified answer.' })
    await expect(answerCandidate).toHaveText('Final verified answer.')
    await expect(liveActivity).toBeVisible()

    lifecycle.finish()
    await expect(liveActivity).toHaveCount(0)
    const settled = page.locator('.msg-ai .assistant-activity')
    await expect(settled).toBeVisible()
    await expect(settled).toHaveAttribute('data-share-expanded', 'false')
    const finalAnswer = page.locator('.msg-ai-text').filter({
      hasText: 'Final verified answer.',
    })
    await expect(finalAnswer).toBeVisible()
    expect(await finalAnswer.evaluate(element =>
      element.closest('.assistant-activity') === null,
    )).toBe(true)
    await settled.locator('.assistant-activity__summary').click()
    await expect(settled.getByText('Draft candidate.', { exact: true })).toBeVisible()
    await expect(
      settled.getByText('Final verified answer.', { exact: true }),
    ).toHaveCount(0)
    await expect(page.getByText('Final verified answer.', { exact: true })).toHaveCount(1)
  })

  test('disables live activity motion when reduced motion is requested', async ({ page }) => {
    await page.emulateMedia({ reducedMotion: 'reduce' })
    await mockControlledActivityLifecycle(page)
    await page.goto(
      CONTROL_URL + 'chat?session=' + encodeURIComponent(LIFECYCLE_SESSION_KEY),
    )
    await expect(page.locator('.conn-pill.connected')).toBeVisible({ timeout: 10000 })

    await page.locator('.chat-textarea').fill('Inspect, draft, verify, and answer.')
    await page.locator('.chat-send-btn[aria-label="Send"]').click()

    const liveActivity = page.locator('.assistant-activity--live')
    await expect(liveActivity).toBeVisible()
    const liveMotion = await liveActivity.evaluate((element) => ({
      dot: getComputedStyle(
        element.querySelector<HTMLElement>('.assistant-activity__live-dot')!,
      ).animationName,
      label: getComputedStyle(
        element.querySelector<HTMLElement>('.assistant-activity__live-label')!,
      ).animationName,
    }))
    expect(liveMotion).toEqual({ dot: 'none', label: 'none' })
  })
})
