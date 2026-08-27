import { lstat, readFile, realpath, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { basename, dirname, isAbsolute, relative, resolve } from 'node:path'

import { expect, test, type Page, type WebSocketRoute } from '@playwright/test'

const MODE = process.env.LONG_TASK_CHARACTERIZATION_MODE
const REPORT_PATH = process.env.LONG_TASK_CHARACTERIZATION_REPORT_PATH
const BASELINE_PATH = process.env.LONG_TASK_CHARACTERIZATION_BASELINE_PATH
const SESSION_KEY = 'agent:main:webchat:e2e-long-task-characterization'
const TASK_ID = 'task-e2e-long-task-characterization'

type RpcRequest = {
  id?: string | number
  method?: string
  type?: string
}

function isWithin(root: string, candidate: string): boolean {
  const child = relative(root, candidate)
  return child === '' || (!child.startsWith('..') && !isAbsolute(child))
}

async function validateTempArtifactPath(
  value: string | undefined,
  label: string,
  mustExist: boolean,
): Promise<string> {
  if (!value || !isAbsolute(value)) {
    throw new Error(`${label} must be an absolute system-temp path`)
  }
  // realpath() makes macOS's /var -> /private/var alias compare correctly.
  const tempRoot = await realpath(tmpdir())
  const parent = await realpath(dirname(value))
  const canonical = resolve(parent, basename(value))
  if (!isWithin(tempRoot, canonical)) {
    throw new Error(`${label} must stay within the system temp directory`)
  }

  let info: Awaited<ReturnType<typeof lstat>> | null = null
  try {
    info = await lstat(value)
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error
  }
  if (mustExist && !info) throw new Error(`${label} does not exist`)
  if (info && (info.isSymbolicLink() || !info.isFile())) {
    throw new Error(`${label} must be a non-symlink regular file`)
  }
  if (mustExist) {
    const resolvedFile = await realpath(value)
    if (!isWithin(tempRoot, resolvedFile)) {
      throw new Error(`${label} resolves outside the system temp directory`)
    }
    return resolvedFile
  }
  return canonical
}

function response(id: string | number | undefined, payload: unknown): string {
  return JSON.stringify({ type: 'res', id, ok: true, payload })
}

function event(eventName: string, payload: Record<string, unknown>): string {
  return JSON.stringify({ type: 'event', event: eventName, payload })
}

async function preparePage(page: Page) {
  await page.addInitScript(() => localStorage.setItem('opensquilla-locale', 'en'))
  await page.route('**/api/approvals', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ pending: [], mode: 'prompt', allowPatterns: [], denyPatterns: [] }),
  }))
}

test.describe('f7 long-task performance characterization', () => {
  test.skip(
    MODE !== 'baseline' && MODE !== 'candidate',
    'Run explicitly with LONG_TASK_CHARACTERIZATION_MODE=baseline|candidate.',
  )

  test('measures the shared production wire fixture and enforces the f7 comparison', async ({
    page,
  }, testInfo) => {
    // The f7 UI cannot consume this wire rate in real time and must drain its
    // browser queue before the baseline is valid. Candidate mode remains a
    // short release gate; baseline characterization gets a one-time 10-minute
    // ceiling instead of treating backlog as a completed measurement.
    test.setTimeout(MODE === 'baseline' ? 600_000 : 180_000)
    const reportPath = await validateTempArtifactPath(
      REPORT_PATH,
      'LONG_TASK_CHARACTERIZATION_REPORT_PATH',
      false,
    )
    const baselinePath = MODE === 'candidate'
      ? await validateTempArtifactPath(
          BASELINE_PATH,
          'LONG_TASK_CHARACTERIZATION_BASELINE_PATH',
          true,
        )
      : null

    await page.setViewportSize({ width: 1280, height: 900 })
    await preparePage(page)

    const cdp = await page.context().newCDPSession(page)
    await cdp.send('Performance.enable')
    await cdp.send('HeapProfiler.enable')
    await cdp.send('HeapProfiler.collectGarbage')
    const initialHeap = await cdp.send('Runtime.getHeapUsage')
    const initialPerformance = await cdp.send('Performance.getMetrics')
    const metricValue = (
      payload: { metrics: Array<{ name: string; value: number }> },
      name: string,
    ) => payload.metrics.find(metric => metric.name === name)?.value ?? 0

    const generation = 'generation-characterization'
    const now = Math.floor(Date.now() / 1_000)
    const history = Array.from({ length: 200 }, (_, index) => ({
      role: index % 2 === 0 ? 'user' : 'assistant',
      text: `Synthetic baseline history ${index + 1}. ${'Windowed content. '.repeat(12)}`,
      id: `characterization-history-${index + 1}`,
      message_id: `characterization-history-${index + 1}`,
      timestamp: now - (200 - index) * 5,
    }))
    let socket: WebSocketRoute | null = null
    let streamSeq = 0
    let subscriptionDelivered = false
    let historyDelivered = false

    await page.routeWebSocket(/\/ws$/, ws => {
      socket = ws
      ws.send(event('connect.challenge', {}))
      ws.onMessage(message => {
        let frame: RpcRequest
        try {
          frame = JSON.parse(String(message)) as RpcRequest
        } catch {
          return
        }
        if (frame.type !== 'req') return
        const method = String(frame.method || '')
        if (method === 'connect') {
          ws.send(JSON.stringify({
            protocol: 3,
            policy: { tick_interval_ms: 30_000, webui_stream_idle_grace_ms: 1_260_000 },
            features: {
              methods: [
                'sessions.messages.subscribe',
                'sessions.messages.snapshot',
                'sessions.messages.hydrate',
              ],
            },
          }))
          return
        }
        if (method === 'sessions.messages.subscribe') {
          subscriptionDelivered = true
          ws.send(response(frame.id, {
            subscribed: true,
            hydration_complete: true,
            replay_complete: true,
            current_stream_seq: streamSeq,
            stream_generation: generation,
            run_status: 'running',
            active_task: { task_id: TASK_ID, status: 'running' },
          }))
          return
        }
        if (method === 'sessions.messages.snapshot') {
          ws.send(response(frame.id, {
            key: SESSION_KEY,
            task_id: TASK_ID,
            events: [],
            current_stream_seq: streamSeq,
            stream_generation: generation,
            run_status: 'running',
            active_task: { task_id: TASK_ID, status: 'running' },
          }))
          return
        }
        if (method === 'sessions.messages.hydrate') {
          ws.send(response(frame.id, {
            hydration_complete: true,
            workspaceId: null,
            run_status: 'running',
            active_task: { task_id: TASK_ID, status: 'running' },
          }))
          return
        }
        if (method === 'chat.history') {
          historyDelivered = true
          ws.send(response(frame.id, { messages: history, has_more: false }))
          return
        }
        const defaults: Record<string, unknown> = {
          'agents.list': { agents: [] },
          'commands.list_for_surface': { commands: [] },
          'config.get': {
            squilla_router: { enabled: false, rollout_phase: 'observe', tiers: {} },
            permissions: {},
            skills: {},
          },
          'models.routing.get': { mode: 'direct' },
          'onboarding.status': { audioConfigured: false },
          'sessions.list': { sessions: [], has_more: false },
          'usage.status': { sessions: [] },
        }
        ws.send(response(frame.id, defaults[method] ?? {}))
      })
    })

    const heapSamples = [initialHeap.usedSize]
    let sampling = true
    const heapSampler = (async () => {
      while (sampling) {
        try {
          heapSamples.push((await cdp.send('Runtime.getHeapUsage')).usedSize)
        } catch (error) {
          if (page.isClosed()) return
          throw error
        }
        await new Promise<void>(resolve => setTimeout(resolve, 250))
      }
    })()

    await page.goto('/control/chat?session=' + encodeURIComponent(SESSION_KEY))
    await expect(page.locator('.conn-pill.connected')).toBeVisible({ timeout: 10_000 })
    await expect(page.locator('.chat-thread')).toBeVisible()
    await expect.poll(() => subscriptionDelivered).toBe(true)
    await expect.poll(() => historyDelivered).toBe(true)
    await expect(page.locator('.chat-thread')).toContainText('Synthetic baseline history 200.')

    const send = (eventName: string, payload: Record<string, unknown>) => {
      if (!socket) throw new Error('characterization websocket is not connected')
      socket.send(event(eventName, {
        key: SESSION_KEY,
        task_id: TASK_ID,
        stream_generation: generation,
        stream_seq: ++streamSeq,
        ...payload,
      }))
    }
    const pace = async (index: number) => {
      if (index % 10 === 9) {
        await new Promise<void>(resolve => setTimeout(resolve, 20))
      }
    }

    // f7 binds the live stream from the explicit lifecycle event; newer
    // clients can also recover it from the subscription snapshot. Emit both so
    // the exact same wire fixture is meaningful on either side of the diff.
    send('task.running', {})
    send('session.event.tool_use_start', {
      tool_use_id: 'characterization-tool',
      name: 'synthetic_idempotent_tool',
      input: {},
    })
    for (let index = 0; index < 20_000; index += 1) {
      send('session.event.thinking', { text: index % 100 === 0 ? 'r' : '.' })
      await pace(index)
    }
    for (let index = 0; index < 10_000; index += 1) {
      send('session.event.tool_use_delta', {
        tool_use_id: 'characterization-tool',
        name: 'synthetic_idempotent_tool',
        fragment: index % 100 === 0 ? '"k":' : '0',
      })
      await pace(index)
    }
    send('session.event.tool_result', {
      tool_use_id: 'characterization-tool',
      name: 'synthetic_idempotent_tool',
      result: 'synthetic result',
      execution_status: { status: 'success' },
    })

    const endMarker = 'CHARACTERIZATION_END'
    const prefix = '## Shared 128 KiB characterization\n\n'
    const markdownPayload = (prefix + 'x'.repeat(128 * 1_024) + endMarker)
      .slice(0, 128 * 1_024 - endMarker.length) + endMarker
    expect(markdownPayload).toHaveLength(128 * 1_024)
    for (let index = 0; index < 4_000; index += 1) {
      send('session.event.text_delta', {
        text: markdownPayload.slice(
          Math.floor(index * markdownPayload.length / 4_000),
          Math.floor((index + 1) * markdownPayload.length / 4_000),
        ),
      })
      await pace(index)
    }
    await expect(page.locator('.chat-thread')).toContainText(endMarker, {
      timeout: MODE === 'baseline' ? 300_000 : 30_000,
    })
    await page.waitForTimeout(750)

    sampling = false
    await heapSampler
    const finalPerformance = await cdp.send('Performance.getMetrics')
    const report = {
      schemaVersion: 1,
      mode: MODE,
      fixture: {
        historyMessages: 200,
        reasoningDeltas: 20_000,
        toolFragments: 10_000,
        textDeltas: 4_000,
        textBytes: 128 * 1_024,
      },
      peakHeapDeltaBytes: Math.max(...heapSamples) - initialHeap.usedSize,
      recalcStyleCount: metricValue(finalPerformance, 'RecalcStyleCount')
        - metricValue(initialPerformance, 'RecalcStyleCount'),
      layoutCount: metricValue(finalPerformance, 'LayoutCount')
        - metricValue(initialPerformance, 'LayoutCount'),
      domNodes: await page.locator('*').count(),
      mountedMessageRows: await page.locator('[data-testid="chat-message-row"]').count(),
    }
    await writeFile(reportPath, JSON.stringify(report, null, 2), {
      encoding: 'utf8',
      mode: 0o600,
    })
    await testInfo.attach('long-task-characterization.json', {
      body: JSON.stringify(report, null, 2),
      contentType: 'application/json',
    })

    expect(report.peakHeapDeltaBytes).toBeGreaterThan(0)
    expect(report.recalcStyleCount).toBeGreaterThan(0)
    if (MODE === 'candidate') {
      const baseline = JSON.parse(await readFile(baselinePath!, 'utf8')) as {
        schemaVersion: number
        fixture: typeof report.fixture
        peakHeapDeltaBytes: number
        recalcStyleCount: number
      }
      expect(baseline.schemaVersion).toBe(report.schemaVersion)
      expect(baseline.fixture).toEqual(report.fixture)
      expect(report.peakHeapDeltaBytes).toBeLessThanOrEqual(
        baseline.peakHeapDeltaBytes * 0.5,
      )
      expect(report.recalcStyleCount).toBeLessThanOrEqual(
        baseline.recalcStyleCount * 0.3,
      )
    }
    await cdp.detach()
  })
})
