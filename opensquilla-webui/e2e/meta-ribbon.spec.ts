import { test, expect, type Page } from '@playwright/test'

/**
 * Meta-skill chat UI (Vue port) — deterministic render verification.
 *
 * We proxy the real gateway WebSocket (so the connect handshake + session load
 * are real) and INJECT synthetic `session.event.meta_*` frames to the client.
 * The composable's gating is lenient — payloads without `key`/`epoch` are
 * accepted (isCurrentSessionPayload/isStaleEpoch) and frames without `seq`
 * skip gap detection — so no session-key capture or live LLM run is needed.
 */

const CONTROL_URL = '/control/'

type Frame = Record<string, unknown>

function evt(event: string, payload: Record<string, unknown>): Frame {
  return { type: 'event', event, payload }
}

const RUN = 'run-e2e-1'
const PERSISTED_RUN = 'run-persisted-paper-1'
const announce = (steps: Array<{ id: string; label: string }>) =>
  evt('session.event.meta_run_announced', {
    run_id: RUN,
    meta_skill_name: 'meta-web-research-to-report',
    language: 'en',
    steps: steps.map((s) => ({ id: s.id, label: s.label, kind: 'task', depends_on: [] })),
    total: steps.length,
  })
const step = (step_id: string, state: string, status_text?: string) =>
  evt('session.event.meta_step_state', { run_id: RUN, step_id, state, status_text })
const completed = (o: Record<string, unknown>) =>
  evt('session.event.meta_run_completed', { run_id: RUN, ...o })
const preflight = () =>
  evt('session.event.meta_preflight', {
    run_id: 'pre-1',
    meta_skill_name: 'meta-web-research-to-report',
    interpreted_request: 'Research the EV battery market and produce a cited report.',
    missing_fields: [],
    assumptions: ['Scope: global market', 'Horizon: 2026'],
    can_skip: true,
    requires_confirmation: true,
  })

function persistedPaperRecovery() {
  const skippedIds = new Set(['paper_clarify', 'final_manuscript_package'])
  const stepIds = Array.from({ length: 36 }, (_, index) => {
    if (index === 1) return 'paper_clarify'
    if (index === 26) return 'final_manuscript_package'
    if (index === 30) return 'publication_quality_gate'
    return `paper_step_${index + 1}`
  })
  const steps = stepIds.map((id, index) => ({
    id,
    label: id.replaceAll('_', ' '),
    kind: index === 30 ? 'skill_exec' : 'agent',
    depends_on: index === 0 ? [] : [stepIds[index - 1]],
  }))
  return {
    recovery: {
      run_id: PERSISTED_RUN,
      announced: {
        run_id: PERSISTED_RUN,
        meta_skill_name: 'meta-paper-write',
        language: 'en',
        steps,
        total: steps.length,
      },
      step_states: steps.map((item, index) => ({
        run_id: PERSISTED_RUN,
        step_id: item.id,
        state: skippedIds.has(item.id)
          ? 'skipped'
          : index < 30
            ? 'succeeded'
            : index === 30
              ? 'failed'
              : 'pending',
        error: index === 30
          ? 'This step failed in a previous run. Review its tool result before retrying.'
          : undefined,
        rescue: index === 30 ? {
          actions: [
            { id: 'retry-step', label: 'Retry failed step' },
            { id: 'retry-with-partial-context', label: 'Retry with partial context' },
          ],
        } : {},
      })),
      completed: {
        run_id: PERSISTED_RUN,
        outcome: 'failed',
        completed_steps: steps
          .slice(0, 30)
          .filter((item) => !skippedIds.has(item.id))
          .map((item) => item.id),
        failed_steps: ['publication_quality_gate'],
        recovered_steps: [],
        skipped_steps: [...skippedIds],
      },
    },
  }
}

async function setup(page: Page): Promise<(frames: Frame[]) => Promise<void>> {
  let send: ((s: string) => void) | null = null
  await page.routeWebSocket('**/ws', (ws) => {
    const server = ws.connectToServer()
    ws.onMessage((m) => server.send(m))
    server.onMessage((m) => ws.send(m))
    send = (s) => ws.send(s)
  })
  await page.goto(CONTROL_URL + 'chat/new')
  // Wait until the proxied socket is connected and the chat view has mounted
  // its composer + registered useMetaRuns.subscribe() (settle avoids a race
  // where frames inject before the rpc.on listeners attach).
  await expect(page.locator('.conn-pill')).toContainText(/connected/i, { timeout: 15000 })
  await page.waitForSelector('.chat-textarea', { timeout: 15000 })
  await expect.poll(() => (send ? 'ready' : 'pending'), { timeout: 15000 }).toBe('ready')
  await page.waitForTimeout(600)
  return async (frames: Frame[]) => {
    for (const f of frames) send!(JSON.stringify(f))
    await page.waitForTimeout(120)
  }
}

test.describe('Meta-skill ribbon (Vue port)', () => {
  test('preflight card renders with interpreted request + actions', async ({ page }) => {
    const inject = await setup(page)
    await inject([preflight()])
    const card = page.locator('.meta-preflight')
    await expect(card).toBeVisible()
    await expect(card).toContainText('EV battery market')
    await expect(card.locator('.meta-preflight-actions button').first()).toBeVisible()
    await page.screenshot({ path: 'test-results/meta-preflight.png' })
  })

  test('ribbon lifecycle: announce → step progress → completed renders state + progress', async ({ page }) => {
    const inject = await setup(page)
    await inject([announce([
      { id: 'gather', label: 'Gather sources' },
      { id: 'analyze', label: 'Analyze' },
      { id: 'write', label: 'Write report' },
    ])])

    const ribbon = page.locator(`.meta-ribbon[data-run-id="${RUN}"]`)
    await expect(ribbon).toBeVisible()
    await expect(ribbon.locator('ol.meta-ribbon-chips li.chip')).toHaveCount(3)
    const bar = ribbon.locator('.meta-ribbon-track[role="progressbar"]')
    await expect(bar).toBeVisible()

    // step 1 running → one chip.running, progress ~33
    await inject([step('gather', 'running', 'Searching the web…')])
    await expect(ribbon.locator('li.chip.running')).toHaveCount(1)
    const v1 = Number(await bar.getAttribute('aria-valuenow'))
    expect(v1).toBeGreaterThan(0)
    await page.screenshot({ path: 'test-results/meta-ribbon-running.png' })

    // step1 done, step2 running → progress advances
    await inject([step('gather', 'succeeded'), step('analyze', 'running')])
    await expect(ribbon.locator('li.chip.succeeded')).toHaveCount(1)
    const v2 = Number(await bar.getAttribute('aria-valuenow'))
    expect(v2).toBeGreaterThan(v1)

    // completed ok → all three succeeded, progress 100
    await inject([completed({
      outcome: 'ok',
      completed_steps: ['gather', 'analyze', 'write'],
      failed_steps: [], recovered_steps: [], skipped_steps: [],
    })])
    await expect(ribbon.locator('li.chip.succeeded')).toHaveCount(3)
    expect(Number(await bar.getAttribute('aria-valuenow'))).toBe(100)
    await page.screenshot({ path: 'test-results/meta-ribbon-completed.png' })
  })

  test('collapse toggle hides chips via data-collapsed', async ({ page }) => {
    const inject = await setup(page)
    await inject([announce([{ id: 'a', label: 'Step A' }, { id: 'b', label: 'Step B' }])])
    const ribbon = page.locator(`.meta-ribbon[data-run-id="${RUN}"]`)
    await expect(ribbon.locator('ol.meta-ribbon-chips')).toBeVisible()
    await ribbon.locator('.meta-ribbon-toggle').click()
    await expect(ribbon).toHaveAttribute('data-collapsed', 'true')
    await expect(ribbon.locator('ol.meta-ribbon-chips')).toBeHidden()
  })

  test('failed run shows fail summary + rescue actions', async ({ page }) => {
    const inject = await setup(page)
    await inject([announce([{ id: 'gather', label: 'Gather' }, { id: 'analyze', label: 'Analyze' }])])
    await inject([
      step('gather', 'succeeded'),
      step('analyze', 'failed', 'Timed out'),
      completed({
        outcome: 'failed',
        completed_steps: ['gather'], failed_steps: ['analyze'],
        recovered_steps: [], skipped_steps: [],
      }),
    ])
    const ribbon = page.locator(`.meta-ribbon[data-run-id="${RUN}"]`)
    await expect(ribbon.locator('li.chip.failed')).toHaveCount(1)
    const actions = ribbon.locator('.meta-ribbon-actions')
    await expect(actions).toBeVisible()
    await expect(ribbon.locator('.meta-ribbon-fail-summary')).toBeVisible()
    await expect(actions.locator('button').first()).toBeVisible()
    await page.screenshot({ path: 'test-results/meta-ribbon-failed.png' })
  })

  test('preflight confirm sends meta.runs.confirm_preflight carrying interpretedRequest', async ({ page }) => {
    // Capture client → server frames through the proxy to assert the action's RPC.
    const outgoing: Array<Record<string, unknown>> = []
    let send: ((s: string) => void) | null = null
    await page.routeWebSocket('**/ws', (ws) => {
      const server = ws.connectToServer()
      ws.onMessage((m) => {
        try { outgoing.push(JSON.parse(String(m))) } catch { /* non-JSON ping */ }
        server.send(m)
      })
      server.onMessage((m) => ws.send(m))
      send = (s) => ws.send(s)
    })
    await page.goto(CONTROL_URL + 'chat/new')
    await expect(page.locator('.conn-pill')).toContainText(/connected/i, { timeout: 15000 })
    await page.waitForSelector('.chat-textarea', { timeout: 15000 })
    await expect.poll(() => (send ? 'ready' : 'pending'), { timeout: 15000 }).toBe('ready')
    await page.waitForTimeout(600)

    send!(JSON.stringify(preflight()))
    const card = page.locator('.meta-preflight')
    await expect(card).toBeVisible()
    await card.getByRole('button', { name: /start/i }).click()

    // The confirm action must hit meta.runs.confirm_preflight WITH interpretedRequest
    // (regression guard for the bug where interpretedRequest was dropped).
    await expect
      .poll(
        () =>
          outgoing.some(
            (f) =>
              f?.method === 'meta.runs.confirm_preflight' &&
              typeof (f?.params as Record<string, unknown>)?.interpretedRequest === 'string' &&
              ((f.params as Record<string, unknown>).interpretedRequest as string).length > 0,
          ),
        { timeout: 8000 },
      )
      .toBe(true)
  })

  test('replayed meta_run_announced (stale stream_seq) does not reset ribbon progress', async ({ page }) => {
    const inject = await setup(page)
    const announceSeq = (seq: number) =>
      evt('session.event.meta_run_announced', {
        run_id: RUN,
        meta_skill_name: 'meta-web-research-to-report',
        language: 'en',
        stream_seq: seq,
        steps: [
          { id: 'gather', label: 'Gather', kind: 'task', depends_on: [] },
          { id: 'analyze', label: 'Analyze', kind: 'task', depends_on: [] },
          { id: 'write', label: 'Write', kind: 'task', depends_on: [] },
        ],
        total: 3,
      })

    await inject([announceSeq(5)])
    const ribbon = page.locator(`.meta-ribbon[data-run-id="${RUN}"]`)
    await expect(ribbon.locator('ol.meta-ribbon-chips li.chip')).toHaveCount(3)

    // Advance a step (newer seq) — handleRpcAny advances the shared cursor to 6.
    await inject([
      evt('session.event.meta_step_state', {
        run_id: RUN,
        step_id: 'gather',
        state: 'succeeded',
        stream_seq: 6,
      }),
    ])
    await expect(ribbon.locator('li.chip.succeeded')).toHaveCount(1)

    // A replayed announce with a STALE seq (5 <= cursor 6) must be dropped —
    // otherwise it would recreate the ribbon and reset gather to pending.
    await inject([announceSeq(5)])
    await page.waitForTimeout(250)
    await expect(ribbon.locator('li.chip.succeeded')).toHaveCount(1)
    await expect(ribbon.locator('li.chip.pending')).toHaveCount(2)
  })

  test('reload restores a persisted 30-of-36 paper run with two skipped steps', async ({ page }) => {
    let recoveryRequests = 0
    await page.routeWebSocket('**/ws', (ws) => {
      const server = ws.connectToServer()
      ws.onMessage((message) => {
        let frame: Record<string, unknown> | null = null
        try { frame = JSON.parse(String(message)) } catch { /* non-JSON ping */ }
        if (frame?.method === 'meta.runs.recovery') {
          recoveryRequests += 1
          ws.send(JSON.stringify({
            type: 'res',
            id: frame.id,
            ok: true,
            payload: persistedPaperRecovery(),
          }))
          return
        }
        server.send(message)
      })
      server.onMessage((message) => ws.send(message))
    })

    const assertRecovered = async () => {
      const ribbon = page.locator(`.meta-ribbon[data-run-id="${PERSISTED_RUN}"]`)
      await expect(ribbon).toBeVisible({ timeout: 15000 })
      await expect(ribbon.locator('li.chip')).toHaveCount(36)
      await expect(ribbon.locator('li.chip.succeeded')).toHaveCount(28)
      await expect(ribbon.locator('li.chip.skipped')).toHaveCount(2)
      await expect(ribbon.locator('li.chip.failed')).toHaveCount(1)
      await expect(ribbon.locator('li.chip.pending')).toHaveCount(5)
      await expect(ribbon.locator('.meta-ribbon-counter')).toHaveText('Step 30 of 36')
      await expect(
        ribbon.locator('button[data-action="retry-step"]'),
      ).toHaveText('Retry failed step')
    }

    await page.goto(CONTROL_URL + 'chat/new')
    await expect(page.locator('.conn-pill')).toContainText(/connected/i, { timeout: 15000 })
    await assertRecovered()
    const beforeReload = recoveryRequests

    // Do not inject any session.event.meta_* frames after reload. The card
    // must come exclusively from the durable recovery projection.
    await page.reload()
    await expect(page.locator('.conn-pill')).toContainText(/connected/i, { timeout: 15000 })
    await assertRecovered()
    expect(recoveryRequests).toBeGreaterThan(beforeReload)
  })

  test('rescue actions replay failed-step, suppress duplicate partial-context, and show guidance', async ({ page }) => {
    // Capture client → server frames through the proxy so we can assert the
    // rescue buttons actually CALL meta.runs.replay (not just render). Guards
    // the gap where the failed-run test only checked the buttons were visible.
    const outgoing: Array<Record<string, unknown>> = []
    const replayLaunchText = '/meta-replay 0123456789abcdef0123456789abcdef'
    let send: ((s: string) => void) | null = null
    await page.routeWebSocket('**/ws', (ws) => {
      const server = ws.connectToServer()
      ws.onMessage((m) => {
        let frame: Record<string, unknown> | null = null
        try {
          frame = JSON.parse(String(m))
          outgoing.push(frame!)
        } catch { /* non-JSON ping */ }
        if (frame?.method === 'meta.runs.replay') {
          const params = (frame.params || {}) as Record<string, unknown>
          const mode = String(params.mode || 'failed-step')
          if (!params.replayToken) {
            ws.send(JSON.stringify({
              type: 'res', id: frame.id, ok: true,
              payload: {
                replay: {
                  message: '/meta meta-web-research-to-report -- original request',
                  live_replay: { available: true, replay_token: `ticket-${mode}` },
                },
              },
            }))
          } else {
            ws.send(JSON.stringify({
              type: 'res', id: frame.id, ok: true,
              payload: {
                replay: {
                  launch_text: replayLaunchText,
                  display_text: mode === 'failed-step'
                    ? 'Retry failed step · meta-web-research-to-report'
                    : 'Retry with partial context · meta-web-research-to-report',
                  live_replay: { available: true, committed: true },
                },
              },
            }))
          }
          return
        }
        if (frame?.method === 'chat.send') {
          const params = (frame.params || {}) as Record<string, unknown>
          if (params.message === replayLaunchText) {
            ws.send(JSON.stringify({
              type: 'res', id: frame.id, ok: true,
              payload: {
                sessionKey: params.sessionKey,
                task_status: 'succeeded',
              },
            }))
            return
          }
        }
        server.send(m)
      })
      server.onMessage((m) => ws.send(m))
      send = (s) => ws.send(s)
    })
    await page.goto(CONTROL_URL + 'chat/new')
    await expect(page.locator('.conn-pill')).toContainText(/connected/i, { timeout: 15000 })
    await page.waitForSelector('.chat-textarea', { timeout: 15000 })
    await expect.poll(() => (send ? 'ready' : 'pending'), { timeout: 15000 }).toBe('ready')
    await page.waitForTimeout(600)
    const inject = (frames: Frame[]) => { for (const f of frames) send!(JSON.stringify(f)) }

    // Announce two steps, fail the second WITH server-provided rescue.actions,
    // then complete(failed) so the rescue UI renders.
    inject([announce([
      { id: 'gather', label: 'Gather' },
      { id: 'analyze', label: 'Analyze' },
    ])])
    inject([
      evt('session.event.meta_step_state', { run_id: RUN, step_id: 'gather', state: 'succeeded' }),
      evt('session.event.meta_step_state', {
        run_id: RUN,
        step_id: 'analyze',
        state: 'failed',
        status_text: 'Timed out',
        error: 'Timed out',
        rescue: {
          actions: [
            { id: 'retry-step', label: 'Retry failed step' },
            { id: 'retry-with-partial-context', label: 'Retry with partial context' },
            { id: 'install-dependency', label: 'Install dependency' },
          ],
        },
      }),
      completed({
        outcome: 'failed',
        completed_steps: ['gather'],
        failed_steps: ['analyze'],
        recovered_steps: [],
        skipped_steps: [],
      }),
    ])
    await page.waitForTimeout(200)

    const ribbon = page.locator(`.meta-ribbon[data-run-id="${RUN}"]`)
    const actions = ribbon.locator('.meta-ribbon-actions')
    await expect(actions).toBeVisible()
    await expect(ribbon.locator('.meta-ribbon-fail-summary')).toContainText('Timed out')
    // partial-context currently has the same runtime seed semantics as
    // retry-step, so the UI suppresses that duplicate choice while retaining
    // backend compatibility for older clients.
    await expect(actions.locator('button')).toHaveCount(3)
    await expect(actions.locator('button[data-action="retry-step"]')).toHaveText('Retry failed step')
    await expect(
      actions.locator('button[data-action="retry-with-partial-context"]'),
    ).toHaveCount(0)

    // retry-step → meta.runs.replay with mode 'failed-step'.
    await actions.locator('button[data-action="retry-step"]').click()
    await expect
      .poll(
        () => outgoing.some(
          (f) => f?.method === 'meta.runs.replay' &&
            (f.params as Record<string, unknown>)?.mode === 'failed-step',
        ),
        { timeout: 8000 },
      )
      .toBe(true)
    await expect
      .poll(
        () => outgoing.some(
          (f) => f?.method === 'chat.send' &&
            (f.params as Record<string, unknown>)?.message === replayLaunchText,
        ),
        { timeout: 8000 },
      )
      .toBe(true)
    const firstReplaySend = outgoing.find(
      (f) => f?.method === 'chat.send' &&
        (f.params as Record<string, unknown>)?.message === replayLaunchText,
    )!
    expect((firstReplaySend.params as Record<string, unknown>).displayText).toContain(
      'Retry failed step',
    )
    expect(JSON.stringify(firstReplaySend.params)).not.toContain('ticket-failed-step')

    // install-dependency → guidance toast, never a replay RPC.
    await actions.locator('button[data-action="install-dependency"]').click()
    await expect(page.getByText(/Install the missing dependency/i)).toBeVisible()
  })
})
