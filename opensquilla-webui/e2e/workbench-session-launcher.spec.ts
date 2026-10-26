import { expect, test, type Page } from '@playwright/test'
import { writeFile } from 'node:fs/promises'
import { helloOkResponse } from './support/gateway-fixture'
import { chatHistoryPayload, sessionMessagesHydratePayload, sessionMessagesSnapshotPayload,
  sessionMessagesSubscribePayload } from './support/session-read-fixtures'
import { expectComposerValue } from './utils'
import { docxFixture, pdfFixture, pptxFixture, xlsxFixture } from './support/office-fixtures'

test.use({ video: process.env.OPENSQUILLA_RETENTION_RECORD ? 'on' : 'retain-on-failure' })

const A = 'agent:main:webchat:launcher-a'
const B = 'agent:main:webchat:launcher-b'
type Wire = Record<string, unknown>

type ReaderResourceSample = {
  workers: { active: number; created: number; terminated: number }
  resizeObservers: number
  readerDom: number
}

async function installReaderResourceProbe(page: Page) {
  await page.addInitScript(() => {
    const workers = { active: 0, created: 0, terminated: 0 }
    let resizeObservers = 0
    const WorkerClass = window.Worker
    window.Worker = class extends WorkerClass {
      private retired = false
      constructor(url: string | URL, options?: WorkerOptions) {
        super(url, options)
        workers.active++; workers.created++
      }
      terminate() {
        if (!this.retired) { this.retired = true; workers.active--; workers.terminated++ }
        super.terminate()
      }
    }
    const ObserverClass = window.ResizeObserver
    window.ResizeObserver = class extends ObserverClass {
      private targets = new Set<Element>()
      observe(target: Element, options?: ResizeObserverOptions) {
        if (!this.targets.size) resizeObservers++
        this.targets.add(target)
        super.observe(target, options)
      }
      unobserve(target: Element) {
        if (this.targets.delete(target) && !this.targets.size) resizeObservers--
        super.unobserve(target)
      }
      disconnect() {
        if (this.targets.size) resizeObservers--
        this.targets.clear()
        super.disconnect()
      }
    }
    Object.defineProperty(window, '__retentionReaderResources', { get: () => ({
      workers: { ...workers }, resizeObservers,
      readerDom: document.querySelectorAll('.pdf-preview, .office-preview').length,
    }) })
  })
}

function readerResourceSample(page: Page): Promise<ReaderResourceSample> {
  return page.evaluate(() => (window as unknown as {
    __retentionReaderResources: ReaderResourceSample
  }).__retentionReaderResources)
}

async function installLauncherGateway(page: Page, projectA: boolean, projectB = projectA, artifacts: Wire[] = [], modernRecovery = false, initialSessionEpoch = 0) {
  const requests: Array<{ method: string; key: string; params?: Wire }> = []
  const materializedProjects = new Set<string>()
  let holdMetadata = true
  let holdHistory = true
  const metadataReplies: Array<() => void> = []
  const historyReplies: Array<() => void> = []
  let emitTerminalOutput: (data: string) => void = () => { throw new Error('Terminal is not connected') }
  let terminalTarget: unknown
  let terminalSeq = 0
  let holdCurrentSnapshot = false
  let holdCurrentMetadata = false
  let deliveryId = 0
  const deliveryEpoch = 'launcher-delivery'
  const currentSnapshotReplies: Array<() => void> = []
  const currentMetadataReplies: Array<() => void> = []
  let emitRecovery = () => { throw new Error('Gateway is not connected') }
  const runningTask = { task_id: 'launcher-running-task', turn_id: 'launcher-running-task',
    status: 'running', started_at: 1_800_000_000 }
  const initialStreamSequence = initialSessionEpoch === 0 ? 1 : 0
  const workspace = { id: 'launcher-project', name: 'Launcher project', path: 'C:/fixtures/launcher',
    available: true, removed: false, taskCount: 2, pinned: false }
  const project = (key: string) => materializedProjects.has(key) || (key === A ? projectA : key === B && projectB)
  const metadata = (key: string) => ({ workspaceId: project(key) ? workspace.id : null,
    projectWorkspace: project(key) ? workspace : null,
    ...(modernRecovery && key === A ? { epoch: initialSessionEpoch, current_stream_seq: initialStreamSequence, run_status: 'running',
      active_task: runningTask, tasks: [runningTask] } : {}) })
  const snapshot = (key: string) => sessionMessagesSnapshotPayload(key, modernRecovery && key === A ? {
    epoch: initialSessionEpoch, task_id: runningTask.task_id, current_stream_seq: initialStreamSequence,
    // The nonzero cold-open variant must install its epoch from metadata, not
    // incidentally from a replayed stream event that arrives before metadata.
    events: initialSessionEpoch === 0 ? [{ event: 'session.event.text_delta', payload: {
      key, session_key: key, task_id: runningTask.task_id, turn_id: runningTask.turn_id, epoch: initialSessionEpoch,
      stream_generation: 'e2e-stream-generation', stream_seq: 1, text: 'The current answer is still generating.',
    } }] : [],
  } : {})
  const history = (key: string) => chatHistoryPayload([A, B].includes(key) ? [
    { id: `${key}-user`, role: 'user', text: `Question ${key === A ? 'A' : 'B'}`, timestamp: 1_800_000_000 },
    { id: `${key}-answer`, role: 'assistant', text: `Answer ${key === A ? 'A' : 'B'} is ready.`, timestamp: 1_800_000_001, artifacts: key === A ? artifacts : [] },
  ] : [])
  await page.addInitScript(() => {
    localStorage.setItem('opensquilla-locale', 'en')
    localStorage.setItem('opensquilla-theme', 'dark')
    window.OPENSQUILLA_FEATURES = { ...(window.OPENSQUILLA_FEATURES || {}), artifactWorkbench: true }
  })
  await page.route('**/api/**', route => route.fulfill({ json: {} }))
  await page.route('**/api/approvals', route => route.fulfill({ json: { pending: [], mode: 'prompt', allowPatterns: [], denyPatterns: [] } }))
  await page.route('**/api/v1/workspace-files/list?**', route => route.fulfill({ json: {
    workspaceBinding: 'launcher-project', relativePath: '', nextCursor: null, hasMore: false, truncated: false,
    entries: [{ name: 'welcome.md', path: 'welcome.md', type: 'file', kind: 'text', mime: 'text/markdown', size: 10, mtimeNs: '123' }],
  } }))
  await page.route('**/api/v1/workspace-files/root', route => route.fulfill({ json: {
    kind: 'workspace-root', name: workspace.name, path: workspace.path, workspaceBinding: 'a'.repeat(64),
  } }))
  await page.routeWebSocket(/\/ws$/, ws => {
    emitRecovery = () => {
      holdCurrentSnapshot = true
      holdCurrentMetadata = true
      ws.send(JSON.stringify({ type: 'event', event: 'transport.flow.dirty', payload: {
        delivery_epoch: deliveryEpoch, dirty_keys: [A], global_dirty: false,
      } }))
    }
    emitTerminalOutput = data => ws.send(JSON.stringify({ type: 'event', event: 'terminal.output', payload: {
      terminalId: 'launcher-pty', target: terminalTarget, seq: ++terminalSeq, data, encoding: 'utf8',
    } }))
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    ws.onMessage(raw => {
      const frame = JSON.parse(String(raw)) as { type: string; id: string; method: string; params?: Wire; nonce?: string }
      if (frame.type === 'ping') { ws.send(JSON.stringify({ type: 'pong', nonce: frame.nonce })); return }
      if (frame.type !== 'req') return
      const key = String(frame.params?.key || frame.params?.sessionKey || frame.params?.session_key || A)
      requests.push({ method: frame.method, key, params: frame.params })
      const respond = (payload: unknown) => ws.send(JSON.stringify({ type: 'res', id: frame.id, ok: true, payload }))
      if (frame.method === 'connect') {
        ws.send(helloOkResponse({ features: { methods: ['sessions.messages.subscribe', 'sessions.messages.hydrate',
          'sessions.messages.snapshot', 'sessions.messages.unsubscribe', 'sessions.sidechat.create',
          'sessions.sidechat.close', 'sessions.create', 'sessions.routing.get', 'workspaces.list', 'workspaces.open',
           'terminal.create', 'terminal.resize', 'terminal.close', 'terminal.consume', 'terminal.write',
           ...(modernRecovery ? ['sessions.messages.snapshot.read', 'sessions.messages.resume',
             'sessions.messages.snapshot.release', 'transport.flow.update'] : [])] },
        ...(modernRecovery ? { policy: { concurrent_history_reads: true,
          transport_flow: { delivery_epoch: deliveryEpoch, window_frames: 128, window_bytes: 4 * 1024 * 1024 } } } : {}),
        auth: { principal: { capabilities: ['chat.read', 'chat.write', 'host.execute'] },
          runModePolicy: { allowedRunModes: ['safe', 'full'], defaultRunMode: 'full' } } }))
        return
      }
      if (frame.method === 'transport.flow.update') {
        respond({ delivery_epoch: deliveryEpoch, ack_delivery_id: Number(frame.params?.ack_delivery_id || 0),
          dirty_keys: [], global_dirty: false })
        return
      }
      if (frame.method === 'sessions.messages.snapshot.read') {
        const reply = () => {
          const value = snapshot(key)
          const bytes = Buffer.from(JSON.stringify(value))
          respond({ key, sync_revision: frame.params?.sync_revision, snapshot_id: `launcher-${frame.params?.sync_revision}`,
            segment_index: 0, segment_count: 1, byte_length: bytes.length, encoding: 'base64-json-utf8',
            data: bytes.toString('base64'), stream_generation: value.stream_generation,
            current_stream_seq: value.current_stream_seq, task_id: value.task_id, session_id: null, session_epoch: initialSessionEpoch,
            delivery: { delivery_epoch: deliveryEpoch, delivery_id: ++deliveryId } })
        }
        if (key === A && holdCurrentSnapshot) currentSnapshotReplies.push(reply)
        else reply()
        return
      }
      if (frame.method === 'sessions.messages.resume' || frame.method === 'sessions.messages.snapshot.release') {
        respond({ ...frame.params, ...(frame.method === 'sessions.messages.resume'
          ? { session_id: null, session_epoch: initialSessionEpoch, replay_to_seq: frame.params?.stream_seq } : { retired: true }) })
        return
      }
      if (frame.method === 'sessions.create') {
        if (frame.params?.workspaceId === workspace.id) materializedProjects.add(key)
        respond({ key, sessionId: `fixture-${key}` })
        return
      }
      if (frame.method === 'terminal.create') {
        terminalTarget = frame.params?.target ?? { kind: 'session', sessionKey: key }
        respond({ terminalId: 'launcher-pty', target: terminalTarget, cols: 100, rows: 30, shell: 'fixture', outputFlowControl: 'consumed-v1' })
        return
      }
      if (frame.method === 'terminal.consume') { respond({ terminalId: 'launcher-pty', consumedSeq: frame.params?.seq }); return }
      if (frame.method === 'terminal.resize') { respond({ terminalId: 'launcher-pty', cols: frame.params?.cols, rows: frame.params?.rows }); return }
      if (frame.method === 'terminal.close') { respond({ terminalId: 'launcher-pty', closed: true }); return }
      if (frame.method === 'terminal.write') { respond({ terminalId: 'launcher-pty', accepted: true, bytes: String(frame.params?.data || '').length }); return }
      const payloads: Record<string, unknown> = {
        'agents.list': { agents: [] }, 'commands.list_for_surface': { commands: [] },
        'config.get': { squilla_router: { enabled: false, tiers: {} }, permissions: {}, skills: {} },
        'onboarding.status': { audioConfigured: false }, 'usage.status': { sessions: [] },
        'workspaces.list': { workspaces: projectA || projectB ? [workspace] : [] },
        'sessions.list': { sessions: [A, B].map((sessionKey, index) => ({ key: sessionKey,
          title: `Launcher task ${index === 0 ? 'A' : 'B'}`, sessionKind: 'chat', surface: 'webchat',
          conversationKind: 'direct', effectiveAgentId: 'main', updatedAt: 1_800_000_000 - index,
          messageCount: 2, status: 'ok', runStatus: 'idle', workspaceId: project(sessionKey) ? workspace.id : null,
        })), count: 2, ts: 1_800_000_002, has_more: false },
        'chat.history': history(key),
        'sessions.messages.subscribe': sessionMessagesSubscribePayload(key, metadata(key)),
        'sessions.messages.hydrate': sessionMessagesHydratePayload(key, metadata(key)),
        'sessions.messages.snapshot': snapshot(key),
        'sessions.routing.get': { key, mode: 'direct', revision: 0, source: 'default', initialized: true, appliesTo: 'next_accepted_turn' },
        'sandbox.run_mode.preference.get': { runMode: 'full', source: 'config' },
      }
      const reply = () => respond(payloads[frame.method] ?? {})
      if (key === A && holdCurrentMetadata && ['sessions.messages.subscribe', 'sessions.messages.hydrate'].includes(frame.method)) {
        currentMetadataReplies.push(reply); return
      }
      if (key === B && frame.method === 'chat.history' && holdHistory) { historyReplies.push(reply); return }
      if (key === B && ['sessions.messages.subscribe', 'sessions.messages.hydrate'].includes(frame.method) && holdMetadata) {
        metadataReplies.push(reply); return
      }
      reply()
    })
  })
  return { requests,
    recoverCurrentSession: () => emitRecovery(),
    currentSnapshotPending: () => currentSnapshotReplies.length,
    currentMetadataPending: () => currentMetadataReplies.length,
    releaseCurrentSnapshot: () => { holdCurrentSnapshot = false; currentSnapshotReplies.splice(0).forEach(reply => reply()) },
    releaseCurrentMetadata: () => { holdCurrentMetadata = false; currentMetadataReplies.splice(0).forEach(reply => reply()) },
    terminalOutput: (data: string) => emitTerminalOutput(data),
    releaseMetadata: () => { holdMetadata = false; metadataReplies.splice(0).forEach(reply => reply()) },
    releaseHistory: () => { holdHistory = false; historyReplies.splice(0).forEach(reply => reply()) },
  }
}

test('terminal preserves alternate screen and consumes hidden output across session switches', async ({ page }, info) => {
  const gateway = await installLauncherGateway(page, false)
  gateway.releaseMetadata(); gateway.releaseHistory()
  await openLauncher(page, 3)
  await page.getByTestId('workbench-start-panel').locator('[data-panel-kind="terminal"]').click()
  const terminal = page.getByTestId('terminal-panel')
  await expect(terminal).toContainText('Connected')
  const rows = terminal.locator('.xterm-rows')
  gateway.terminalOutput('NORMAL-BASE\r\n\x1b[?1049h\x1b[2J\x1b[HALT-BEFORE')
  await expect(rows).toContainText('ALT-BEFORE')
  const original = await terminal.elementHandle()
  await page.getByRole('button', { name: 'Launcher task B', exact: true }).click()
  await expect(page.locator('#app-main .chat')).toContainText('Answer B is ready.')
  await expect(terminal).toBeHidden()
  const hiddenResizeCount = gateway.requests.filter(request => request.method === 'terminal.resize').length
  // This updates the real xterm parser while its renderer is hidden. It must
  // consume the frame, retain alternate-screen mode, and leave B interactive.
  gateway.terminalOutput('\x1b[HALT-HIDDEN')
  await expect.poll(() => gateway.requests.filter(request => request.method === 'terminal.consume').at(-1)?.params?.seq).toBe(2)
  expect(gateway.requests.filter(request => request.method === 'terminal.resize')).toHaveLength(hiddenResizeCount)
  const composer = page.locator('#app-main .chat-textarea')
  await composer.fill('Unsent B draft')
  await page.getByRole('button', { name: 'Launcher task A', exact: true }).click()
  await expect(terminal).toBeVisible()
  await expect(rows).toContainText('ALT-HIDDEN')
  expect(await original!.evaluate(node => node === document.querySelector('[data-testid="terminal-panel"]'))).toBe(true)
  expect(gateway.requests.filter(request => request.method === 'terminal.create')).toHaveLength(1)
  expect(gateway.requests.filter(request => request.method === 'terminal.close')).toHaveLength(0)
  await page.screenshot({ path: info.outputPath('terminal-restored-alternate-screen.png') })
  gateway.terminalOutput('\x1b[?1049l')
  await expect(rows).toContainText('NORMAL-BASE')
  await expect(rows).not.toContainText('ALT-HIDDEN')
  await page.getByRole('button', { name: 'Launcher task B', exact: true }).click()
  await expectComposerValue(composer, 'Unsent B draft')
  expect(gateway.requests.filter(request => request.method === 'terminal.close')).toHaveLength(0)
  await original!.dispose()
})

test('a committed editor input and immediate navigation preserve both session drafts', async ({ page }, info) => {
  const gateway = await installLauncherGateway(page, false)
  gateway.releaseMetadata(); gateway.releaseHistory()
  await page.goto(`/control/chat?session=${A}`)
  await expect(page.locator('#app-main .chat')).toContainText('Answer A is ready.')
  await page.getByRole('button', { name: 'Launcher task B', exact: true }).click()
  await expect(page.locator('#app-main .chat')).toContainText('Answer B is ready.')
  const composer = page.locator('#app-main .chat-textarea')
  await composer.fill('Existing B draft')
  await expectComposerValue(composer, 'Existing B draft')
  await page.getByRole('button', { name: 'Launcher task A', exact: true }).click()
  await expect(page.locator('#app-main .chat')).toContainText('Answer A is ready.')
  // Same-task input/navigation reproduces a final editor commit racing route
  // ownership, without writing app state or storage from the fixture.
  await page.getByRole('button', { name: 'Launcher task B', exact: true }).evaluate(button => {
    const editor = document.querySelector<HTMLElement>('#app-main .chat-textarea')!
    editor.textContent = 'Latest A edit'
    editor.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: 'Latest A edit' }))
    button.click()
  })
  await expect(page.locator('#app-main .chat')).toContainText('Answer B is ready.')
  await expectComposerValue(composer, 'Existing B draft')
  await page.getByRole('button', { name: 'Launcher task A', exact: true }).click()
  await expectComposerValue(composer, 'Latest A edit')
  await page.screenshot({ path: info.outputPath('main-draft-owner-restored.png') })
})

async function openLauncher(page: Page, count: number) {
  await page.setViewportSize({ width: 1440, height: 960 })
  await page.goto(`/control/chat?session=${A}`)
  await expect(page.locator('#app-main .chat')).toContainText('Answer A is ready.')
  await page.getByTestId('topbar-workbench-toggle').click()
  const host = page.getByTestId('workbench-host')
  const cards = page.getByTestId('workbench-start-panel').locator('[data-panel-kind]')
  await expect(cards).toHaveCount(count)
  await expect(cards.first()).toBeVisible()
  await expect(page.getByTestId('workbench-start-panel').locator('[data-panel-kind="assistant-chat"]')).toBeEnabled()
  await expect.poll(() => host.evaluate(element => element.getAnimations().every(animation =>
    animation.playState === 'finished' || animation.playState === 'idle'))).toBe(true)
  const boxes = await cards.evaluateAll(elements => elements.map(element => {
    const rect = element.getBoundingClientRect()
    return { left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom, width: rect.width, height: rect.height }
  }))
  expect(boxes[0]!.width).toBeLessThanOrEqual(380)
  for (const [index, box] of boxes.entries()) {
    expect(box.height).toBeGreaterThanOrEqual(68)
    expect(Math.abs(box.left - boxes[0]!.left)).toBeLessThan(1)
    expect(Math.abs(box.width - boxes[0]!.width)).toBeLessThan(1)
    if (index > 0) expect(box.top).toBeGreaterThanOrEqual(boxes[index - 1]!.bottom)
  }
  return { host, cards }
}

for (const initialSessionEpoch of [0, 7]) {
test(`same-session delivery recovery at epoch ${initialSessionEpoch} keeps launcher cards stable and defers terminal creation until metadata settles`, async ({ page }, info) => {
  const gateway = await installLauncherGateway(page, false, false, [], true, initialSessionEpoch)
  gateway.releaseMetadata(); gateway.releaseHistory()
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await openLauncher(page, 3)
  await expect(page.locator('.chat-stop-btn')).toBeVisible()
  expect(await page.evaluate(() => Boolean(window.opensquillaDesktop))).toBe(false)
  const launcher = page.getByTestId('workbench-start-panel')
  const terminal = launcher.locator('[data-panel-kind="terminal"]')
  await page.evaluate(() => {
    const original = document.querySelector('[data-testid="workbench-start-panel"]')
    const capture = () => {
      const panel = document.querySelector('[data-testid="workbench-start-panel"]')
      return { sameNode: panel === original, cards: Array.from(panel?.querySelectorAll<HTMLButtonElement>('[data-panel-kind]') || []).map(card => {
        const label = card.querySelector('.app-workbench__new-panel-option-copy > span')!
        const rect = label.getBoundingClientRect()
        return { kind: card.dataset.panelKind, disabled: card.disabled, text: card.innerText,
          labelX: Math.round(rect.x * 10) / 10, labelY: Math.round(rect.y * 10) / 10 }
      }) }
    }
    const probe = { running: true, baseline: capture(), frames: [] as ReturnType<typeof capture>[] }
    ;(window as unknown as { __launcherRecoveryProbe: typeof probe }).__launcherRecoveryProbe = probe
    const sample = () => {
      if (!probe.running) return
      probe.frames.push(capture())
      requestAnimationFrame(sample)
    }
    requestAnimationFrame(sample)
  })
  const renderedFrames = () => page.evaluate(async () => {
    for (let index = 0; index < 8; index++) await new Promise<void>(resolve => requestAnimationFrame(() => resolve()))
  })
  for (let cycle = 0; cycle < 2; cycle++) {
    gateway.recoverCurrentSession()
    await expect.poll(gateway.currentSnapshotPending).toBeGreaterThan(0)
    await renderedFrames()
    gateway.releaseCurrentSnapshot()
    await expect.poll(gateway.currentMetadataPending).toBeGreaterThan(0)
    await renderedFrames()
    if (cycle === 0) {
      gateway.releaseCurrentMetadata()
      await expect(terminal).toBeEnabled()
      await renderedFrames()
    }
  }
  const evidence = await page.evaluate(() => {
    const probe = (window as unknown as { __launcherRecoveryProbe: {
      running: boolean; baseline: unknown; frames: unknown[]
    } }).__launcherRecoveryProbe
    probe.running = false
    return { baseline: probe.baseline, frames: probe.frames }
  })
  await writeFile(info.outputPath('launcher-recovery-frames.json'), JSON.stringify(evidence, null, 2))
  expect(evidence.frames.length).toBeGreaterThanOrEqual(32)
  for (const frame of evidence.frames) expect(frame).toEqual(evidence.baseline)
  // A visible, previously available action may be clicked while authority is
  // refreshing, but must not execute against the old workspace binding.
  await terminal.click()
  await renderedFrames()
  expect(gateway.requests.filter(request => request.method === 'terminal.create')).toHaveLength(0)
  await expect(page.getByTestId('terminal-panel')).toHaveCount(0)
  gateway.releaseCurrentMetadata()
  await expect(page.getByTestId('terminal-panel')).toContainText('Connected')
  const creates = gateway.requests.filter(request => request.method === 'terminal.create')
  expect(creates).toHaveLength(1)
  expect(creates[0]!.params?.target).toEqual({ kind: 'default-agent', agentId: 'main' })
  expect(errors).toEqual([])
})
}

for (const withProject of [false, true]) {
  test(`explicitly opened launcher keeps ${withProject ? 4 : 3} vertical cards while sidebar session history and workspace resolve`, async ({ page }, info) => {
    const gateway = await installLauncherGateway(page, withProject)
    const { host, cards } = await openLauncher(page, withProject ? 4 : 3)
    await page.getByRole('button', { name: 'Launcher task B', exact: true }).click()
    await expect.poll(() => gateway.requests.some(request => request.key === B && request.method === 'chat.history')).toBe(true)
    await expect.poll(() => gateway.requests.some(request => request.key === B && request.method === 'sessions.messages.subscribe')).toBe(true)
    // A's open launcher belongs to A. B starts closed, even before its
    // metadata/history arrive, and opens only through the user's own action.
    await expect(host).toBeHidden()
    await page.getByTestId('topbar-workbench-toggle').click()
    await expect(host).toBeVisible()
    await expect(cards).toHaveCount(withProject ? 4 : 3)
    await expect.poll(() => host.evaluate(element => element.getAnimations().every(animation =>
      animation.playState === 'finished' || animation.playState === 'idle'))).toBe(true)
    // Observe the real App across every rendered frame; no store injection,
    // readiness delay, or synthetic event supplies the navigation state.
    const probe = await host.evaluateHandle(element => {
      const original = element
      const snapshots: Array<{ sameHost: boolean; visible: boolean; background: string; box: number[]; cards: number[][] }> = []
      let running = true
      const sample = () => {
        const current = document.querySelector('[data-testid="workbench-host"]')
        const style = getComputedStyle(original)
        const rect = original.getBoundingClientRect()
        snapshots.push({ sameHost: current === original && original.isConnected,
          visible: style.display !== 'none' && style.visibility !== 'hidden' && Number(style.opacity) === 1,
          background: style.backgroundColor, box: [rect.x, rect.y, rect.width, rect.height],
          cards: Array.from(original.querySelectorAll('[data-testid="workbench-start-panel"] [data-panel-kind]')).map(card => {
            const box = card.getBoundingClientRect(); return [box.x, box.y, box.width, box.height]
          }) })
      }
      const frame = () => { if (running) { sample(); requestAnimationFrame(frame) } }
      sample(); requestAnimationFrame(frame)
      return { stop: () => { sample(); running = false; return snapshots } }
    })
    for (const card of await cards.all()) await expect(card).toBeDisabled()
    gateway.releaseMetadata()
    await expect(page.locator('#app-main .chat-thread')).toHaveAttribute('data-session-key', B)
    for (const card of await cards.all()) await expect(card).toBeDisabled()
    await expect(page.getByTestId('workbench-start-panel')).toHaveAttribute('aria-busy', 'true')
    gateway.releaseHistory()
    await expect(page.locator('#app-main .chat')).toContainText('Answer B is ready.')
    await expect(cards).toHaveCount(withProject ? 4 : 3)
    for (const kind of ['terminal', 'assistant-chat', ...(withProject ? ['workspace-files'] : [])]) {
      await expect(host.locator(`[data-testid="workbench-start-panel"] [data-panel-kind="${kind}"]`)).toBeEnabled()
    }
    const snapshots = await probe.evaluate(value => value.stop())
    await probe.dispose()
    await info.attach('launcher-pending-frames', { body: JSON.stringify(snapshots, null, 2), contentType: 'application/json' })
    expect(snapshots.length).toBeGreaterThan(1)
    for (const snapshot of snapshots) {
      expect(snapshot.sameHost).toBe(true)
      expect(snapshot.visible).toBe(true)
      expect(snapshot.background).toBe(snapshots[0]!.background)
      expect(snapshot.cards).toHaveLength(withProject ? 4 : 3)
      for (const [index, value] of snapshot.box.entries()) expect(Math.abs(value - snapshots[0]!.box[index]!)).toBeLessThan(1)
      for (const [index, box] of snapshot.cards.entries()) {
        for (const [coordinate, value] of box.entries()) expect(Math.abs(value - snapshots[0]!.cards[index]![coordinate]!)).toBeLessThan(1)
      }
    }
    // Pure Web deliberately does not acquire the native browser capability.
    await expect(host.locator('[data-testid="workbench-start-panel"] [data-panel-kind="browser"]')).toBeDisabled()
    expect(gateway.requests.filter(request => /sessions.sidechat.(create|close)/.test(request.method))).toHaveLength(0)
  })
}

test('resolved no-project session removes only its workspace entry and side keeps the launcher canvas', async ({ page }) => {
  const gateway = await installLauncherGateway(page, true, false)
  const { host, cards } = await openLauncher(page, 4)
  const background = await host.evaluate(element => getComputedStyle(element).backgroundColor)
  await page.getByRole('button', { name: 'Launcher task B', exact: true }).click()
  await expect.poll(() => gateway.requests.some(request => request.key === B && request.method === 'chat.history')).toBe(true)
  await expect(host).toBeHidden()
  await page.getByTestId('topbar-workbench-toggle').click()
  await expect(host).toBeVisible()
  await expect(cards).toHaveCount(4)
  gateway.releaseMetadata()
  gateway.releaseHistory()
  await expect(page.locator('#app-main .chat')).toContainText('Answer B is ready.')
  await expect(cards).toHaveCount(3)
  await expect(host.locator('[data-testid="workbench-start-panel"] [data-panel-kind="workspace-files"]')).toHaveCount(0)
  await host.locator('[data-testid="workbench-start-panel"] [data-panel-kind="assistant-chat"]').click()
  await expect(page.getByTestId('assistant-chat-panel')).toBeVisible()
  expect(await host.evaluate(element => getComputedStyle(element).backgroundColor)).toBe(background)
  expect(gateway.requests.filter(request => /sessions.sidechat.(create|close)/.test(request.method))).toHaveLength(0)
})

for (const withProject of [false, true]) {
  test(`${withProject ? 'project' : 'global'} new task starts with its panel closed and returns to A's open launcher`, async ({ page }, info) => {
    const gateway = await installLauncherGateway(page, withProject)
    gateway.releaseMetadata(); gateway.releaseHistory()
    const { host, cards } = await openLauncher(page, withProject ? 4 : 3)
    if (withProject) {
      const project = page.locator('.sidebar-history-row--workspace').first()
      await project.hover()
      await project.getByTestId('project-workspace-new-task').click()
    } else {
      await page.getByRole('button', { name: 'New task', exact: true }).click()
    }
    await expect(page).toHaveURL(withProject
      ? /\/chat\/new\?agent=main&project=launcher-project$/
      : /\/chat\/new(?:\?|$)/)
    await expect(page.locator('.chat--new-landing')).toBeVisible()
    await expect(host).toBeHidden()
    await expect(page.getByTestId('topbar-workbench-toggle')).toHaveAttribute('aria-expanded', 'false')
    await page.screenshot({ path: info.outputPath(`${withProject ? 'project' : 'global'}-new-task-panel-closed.png`) })

    await page.getByRole('button', { name: 'Launcher task A', exact: true }).click()
    await expect(page.locator('#app-main .chat')).toContainText('Answer A is ready.')
    await expect(host).toBeVisible()
    await expect(cards).toHaveCount(withProject ? 4 : 3)
    await expect(page.getByTestId('topbar-workbench-toggle')).toHaveAttribute('aria-expanded', 'true')
    expect(gateway.requests.filter(request => ['sessions.create', 'sessions.sidechat.create',
      'sessions.sidechat.close', 'chat.send', 'chat.abort', 'chat.cancel'].includes(request.method))).toHaveLength(0)
  })
}

test('project draft materialization through Files does not leave the reopened panel menu preparing', async ({ page }) => {
  const gateway = await installLauncherGateway(page, true)
  await page.setViewportSize({ width: 1440, height: 960 })
  await page.goto('/control/chat/new?agent=main&project=launcher-project')
  await expect(page.locator('.topbar')).toHaveAttribute('data-connection-state', 'connected')
  const composer = page.locator('#app-main .chat-textarea')
  await composer.fill('Keep the project draft while opening files')
  const draftKey = await page.locator('#app-main .chat-thread').getAttribute('data-session-key')
  expect(draftKey).toBeTruthy()
  await page.getByTestId('topbar-workbench-toggle').click()
  const launcher = page.getByTestId('workbench-start-panel')
  await expect(launcher.locator('[data-panel-kind="assistant-chat"]')).toHaveCount(0)
  await launcher.locator('[data-panel-kind="workspace-files"]').click()
  await expect(page.getByTestId('workspace-files-panel')).toContainText('welcome.md')
  expect(new URL(page.url()).searchParams.get('session')).toBe(draftKey)
  await expectComposerValue(composer, 'Keep the project draft while opening files')
  expect(gateway.requests.filter(request => request.method === 'sessions.create')).toHaveLength(1)
  expect(gateway.requests.find(request => request.method === 'sessions.create')?.params).toMatchObject({
    session_key: draftKey, workspaceId: 'launcher-project',
  })
  await page.getByTestId('workbench-new-panel-trigger').click()
  const menu = page.getByTestId('workbench-new-panel-menu-list')
  await expect(menu).toBeVisible()
  await expect(menu.locator('[data-panel-kind="workspace-files"]')).toBeEnabled()
  await expect(menu.locator('[data-panel-kind="terminal"]')).toBeEnabled()
  await expect(menu.locator('[data-panel-kind="assistant-chat"]')).toHaveCount(0)
  expect(gateway.requests.filter(request => request.key === draftKey && request.method === 'chat.history')).toHaveLength(0)
  expect(gateway.requests.filter(request => ['chat.send', 'sessions.sidechat.create'].includes(request.method))).toHaveLength(0)
})

test.describe('session workbench retention', () => {
  for (const format of ['pdf', 'docx', 'pptx', 'xlsx'] as const) {
    test(`restores artifact ${format} reader through the real session entry`, async ({ page }, info) => {
      await installReaderResourceProbe(page)
      const cycles = Math.max(1, Math.min(20, Number.parseInt(process.env.OPENSQUILLA_RETENTION_CYCLES || '1', 10) || 1))
      const bytes = { pdf: pdfFixture, docx: docxFixture, pptx: pptxFixture, xlsx: xlsxFixture }[format]()
      const mime = { pdf: 'application/pdf', docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        pptx: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
        xlsx: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' }[format]
      const name = `artifact-retained.${format}`
      const gateway = await installLauncherGateway(page, true, true, [{ id: 'retained-document', name, mime,
        size: bytes.length, download_url: '/api/v1/artifacts/retained-document' }])
      gateway.releaseMetadata(); gateway.releaseHistory()
      let reads = 0
      const binaryRequests: Array<{ method: string; path: string }> = []
      await page.route('**/api/v1/artifacts/retained-document**', route => {
        const url = new URL(route.request().url())
        binaryRequests.push({ method: route.request().method(), path: url.pathname + url.search })
        reads++; return route.fulfill({ contentType: mime, body: bytes })
      })
      await page.route('**/opensquilla-mark.png', route => route.fulfill({ contentType: 'image/png', body: Buffer.from(
        'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==', 'base64',
      ) }))
      const errors: string[] = []
      page.on('pageerror', error => errors.push(error.message))
      await page.setViewportSize({ width: 1440, height: 960 })
      await page.goto('/control/chat?session=' + encodeURIComponent(A))
      const initialResources = await readerResourceSample(page)
      const openStarted = Date.now()
      await page.locator('.msg-artifact-body').filter({ hasText: name }).click()
      const reader = page.locator(format === 'pdf' ? '.pdf-preview' : '.office-preview')
      await expect(reader.getByRole('button', { name: 'Zoom in', exact: true })).toBeEnabled()
      const firstOpenMs = Date.now() - openStarted
      if (format !== 'pdf') {
        await reader.getByRole('button', { name: 'Next', exact: true }).click()
        await expect(reader.locator('.office-preview__position')).toHaveText('2 / 2')
      }
      await reader.getByRole('button', { name: 'Zoom in', exact: true }).click()
      const zoom = reader.locator(format === 'pdf' ? '.pdf-preview__fit' : '.office-preview__zoom')
      if (format !== 'xlsx') await expect(zoom).toHaveValue('manual')
      const activeBaseline = await readerResourceSample(page)
      const initialReads = reads
      expect(initialReads).toBe(1)
      let inactiveBaseline: ReaderResourceSample | undefined
      const samples: Array<{ cycle: number; awayMs: number; returnMs: number; reads: number; readDelta: number;
        inactive: ReaderResourceSample; active: ReaderResourceSample }> = []
      for (let cycle = 1; cycle <= cycles; cycle++) {
        const awayStarted = Date.now()
        await page.getByRole('button', { name: 'Launcher task B', exact: true }).click()
        await expect(page.locator('#app-main .chat:visible')).toContainText('Answer B is ready.')
        await expect(reader).toHaveCount(0)
        await expect.poll(async () => (await readerResourceSample(page)).workers.active).toBe(initialResources.workers.active)
        const inactive = await readerResourceSample(page)
        const awayMs = Date.now() - awayStarted
        inactiveBaseline ??= inactive
        expect(inactive.resizeObservers).toBe(inactiveBaseline.resizeObservers)
        const readsBeforeReturn = reads
        const returnStarted = Date.now()
        await page.getByRole('button', { name: 'Launcher task A', exact: true }).click()
        await expect(reader.getByRole('button', { name: 'Zoom in', exact: true })).toBeEnabled()
        if (format !== 'pdf') await expect(reader.locator('.office-preview__position')).toHaveText('2 / 2')
        if (format !== 'xlsx') await expect(zoom).toHaveValue('manual')
        const active = await readerResourceSample(page)
        samples.push({ cycle, awayMs, returnMs: Date.now() - returnStarted, reads, readDelta: reads - readsBeforeReturn, inactive, active })
        expect(reads - readsBeforeReturn).toBe(1)
        expect(active.readerDom).toBe(1)
        expect(active.workers.active).toBe(activeBaseline.workers.active)
        expect(active.resizeObservers).toBe(activeBaseline.resizeObservers)
      }
      expect(reads).toBe(initialReads * (cycles + 1))
      expect(errors).toEqual([])
      await page.mouse.move(10, 10)
      await page.screenshot({ path: info.outputPath(`artifact-${format}-reader-restored.png`) })
      // This is click-to-assertion latency in the controlled browser fixture,
      // not a performance gate or a heap measurement. Every return rebuilds
      // a disposed reader; JS modules and HTTP browser caches are already warm.
      await writeFile(info.outputPath(`artifact-${format}-retention-metrics.json`), JSON.stringify({
        format, cycles, firstOpenMs, initialResources, activeBaseline, initialReads, reads, binaryRequests, errors, samples,
      }, null, 2))
      expect(gateway.requests.filter(request => /sessions.sidechat.(create|close)|chat.abort|chat.cancel/.test(request.method))).toHaveLength(0)
    })
  }
  for (const format of ['pdf', 'docx', 'pptx', 'xlsx'] as const) {
    test(`restores ${format} reading position after disposing the old session reader`, async ({ page }, info) => {
      const gateway = await installLauncherGateway(page, true)
      gateway.releaseMetadata(); gateway.releaseHistory()
      const bytes = { pdf: pdfFixture, docx: docxFixture, pptx: pptxFixture, xlsx: xlsxFixture }[format]()
      const mime = { pdf: 'application/pdf', docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        pptx: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
        xlsx: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' }[format]
      const name = `retained.${format}`
      await page.route('**/api/v1/workspace-files/list?**', route => route.fulfill({ json: {
        workspaceBinding: 'launcher-project', relativePath: '', nextCursor: null, hasMore: false, truncated: false,
        entries: [{ name, path: name, type: 'file', kind: 'download', mime, size: bytes.length, mtimeNs: '123' }],
      } }))
      await page.route('**/api/v1/workspace-files/resolve', route => route.fulfill({ json: {
        workspaceBinding: 'launcher-project', files: [{ requestedPath: name, path: name, name,
          mime, size: bytes.length, kind: 'download', revision: 'reading-v1' }],
      } }))
      let reads = 0
      await page.route('**/api/v1/workspace-files/content?**', route => {
        reads++
        return route.fulfill({ contentType: mime, body: bytes })
      })
      const errors: string[] = []
      page.on('pageerror', error => errors.push(error.message))
      const { host } = await openLauncher(page, 4)
      await host.locator('[data-panel-kind="workspace-files"]').click()
      const files = page.getByTestId('workspace-files-panel')
      await files.getByRole('treeitem', { name, exact: true }).click()
      const reader = files.locator(format === 'pdf' ? '.pdf-preview' : '.office-preview')
      await expect(reader.getByRole('button', { name: 'Zoom in', exact: true })).toBeEnabled()
      if (format !== 'pdf') {
        await reader.getByRole('button', { name: 'Next', exact: true }).click()
        await expect(reader.locator('.office-preview__position')).toHaveText('2 / 2')
      }
      await reader.getByRole('button', { name: 'Zoom in', exact: true }).click()
      const zoom = format === 'pdf' ? reader.locator('.pdf-preview__fit') : reader.locator('.office-preview__zoom')
      const expectedZoom = format === 'xlsx' ? null : await zoom.inputValue()
      if (expectedZoom) expect(expectedZoom).toBe('manual')
      await page.getByRole('button', { name: 'Launcher task B', exact: true }).click()
      await expect(page.locator('#app-main .chat:visible')).toContainText('Answer B is ready.')
      await expect(reader).toHaveCount(0)
      await page.getByRole('button', { name: 'Launcher task A', exact: true }).click()
      await expect(reader.getByRole('button', { name: 'Zoom in', exact: true })).toBeEnabled()
      if (format !== 'pdf') await expect(reader.locator('.office-preview__position')).toHaveText('2 / 2')
      if (expectedZoom) await expect(zoom).toHaveValue(expectedZoom)
      expect(reads).toBeGreaterThan(1)
      expect(errors).toEqual([])
      await page.screenshot({ path: info.outputPath(`${format}-reader-restored.png`) })
      expect(gateway.requests.filter(request => /sessions.sidechat.(create|close)|chat.abort|chat.cancel/.test(request.method))).toHaveLength(0)
    })
  }
  test('restores files, reading state and side draft through the real A B A entry without retaining file DOM', async ({ page }, info) => {
    const gateway = await installLauncherGateway(page, true)
    gateway.releaseMetadata()
    gateway.releaseHistory()
    const content = '# Retained document\n\n' + Array.from({ length: 100 }, (_, index) =>
      `Reading line ${index + 1}: keep this workspace document and its reading position.`).join('\n\n')
    let contentReads = 0
    await page.route('**/api/v1/workspace-files/resolve', route => route.fulfill({ json: {
      workspaceBinding: 'launcher-project', files: [{ requestedPath: 'welcome.md', path: 'welcome.md',
        name: 'welcome.md', mime: 'text/markdown', size: content.length, kind: 'text', revision: 'retention-v1' }],
    } }))
    await page.route('**/api/v1/workspace-files/content?**', route => {
      contentReads += 1
      return route.fulfill({ contentType: 'text/markdown', body: content })
    })
    const { host } = await openLauncher(page, 4)
    await host.locator('[data-panel-kind="workspace-files"]').click()
    const files = page.getByTestId('workspace-files-panel')
    await files.getByRole('treeitem', { name: 'welcome.md' }).click()
    const document = files.getByTestId('workspace-file-panel')
    await expect(document).toContainText('Retained document')
    await document.getByRole('button', { name: 'Source', exact: true }).click()
    await document.getByRole('button', { name: 'Search source', exact: true }).click()
    await document.locator('input[type="search"]').fill('Reading line')
    await files.locator('.workspace-files__filter-input').fill('welcome')
    const source = document.locator('.workspace-file__source')
    await source.hover()
    await page.mouse.wheel(0, 480)
    await expect.poll(() => source.evaluate(element => element.scrollTop)).toBeGreaterThan(100)
    const readingTop = await source.evaluate(element => element.scrollTop)

    await page.getByTestId('workbench-new-panel-trigger').click()
    await page.getByTestId('workbench-new-panel-menu-list').locator('[data-panel-kind="assistant-chat"]').click()
    const side = page.getByTestId('assistant-chat-panel')
    await side.locator('.chat-textarea').fill('Unsent side draft stays with A')
    const tabs = host.getByRole('tablist', { name: 'Open workbench items', exact: true })
    await tabs.getByRole('tab', { name: 'Workspace files', exact: true }).click()
    await expect(files).toBeVisible()
    await expect(document.getByRole('button', { name: 'Source', exact: true })).toHaveAttribute('aria-pressed', 'true')
    await expect(document.locator('input[type="search"]')).toHaveValue('Reading line')

    await page.getByRole('button', { name: 'Launcher task B', exact: true }).click()
    await expect(page.locator('#app-main .chat:visible')).toContainText('Answer B is ready.')
    await expect(files).toHaveCount(0)
    await expect(side).toBeHidden()
    await page.locator('#app-main .chat-textarea:visible').fill('Independent main draft in B')
    await expectComposerValue(page.locator('#app-main .chat-textarea:visible'), 'Independent main draft in B')

    await page.getByRole('button', { name: 'Launcher task A', exact: true }).click()
    await expect(page.locator('#app-main .chat:visible')).toContainText('Answer A is ready.')
    await expect(files).toBeVisible()
    await expect(tabs.getByRole('tab')).toHaveCount(2)
    await expect(files.locator('.workspace-files__filter-input')).toHaveValue('welcome')
    await expect(files.getByRole('treeitem', { name: 'welcome.md' })).toHaveAttribute('aria-selected', 'true')
    await expect(document.getByRole('button', { name: 'Source', exact: true })).toHaveAttribute('aria-pressed', 'true')
    await expect(document.locator('input[type="search"]')).toHaveValue('Reading line')
    await expect.poll(() => source.evaluate(element => element.scrollTop)).toBeCloseTo(readingTop, 0)
    expect(contentReads).toBeGreaterThan(1)
    await page.screenshot({ path: info.outputPath('files-restored.png') })
    await tabs.getByRole('tab', { name: 'Assistant chat', exact: true }).click()
    await expectComposerValue(side.locator('.chat-textarea'), 'Unsent side draft stays with A')
    expect(gateway.requests.filter(request => /sessions.sidechat.(create|close)|chat.abort|chat.cancel/.test(request.method))).toHaveLength(0)
    await page.screenshot({ path: info.outputPath('side-draft-restored.png') })
  })
})
