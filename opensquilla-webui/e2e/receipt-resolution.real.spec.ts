import type { Page, TestInfo } from '@playwright/test'
import { execFile } from 'node:child_process'
import { createHash } from 'node:crypto'
import { readFile, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { promisify } from 'node:util'
import { expect, test } from './real-gateway.fixture'
import { startRealGoalGateway, type RealGoalGateway } from './real-goal-gateway'
import { expectComposerValue } from './utils'

type Request = { type: string; id: string; method: string; params: Record<string, unknown> }
type Response = { type: string; id: string; ok: boolean; payload?: Record<string, unknown> }
type Send = { request: Request; forwarded: boolean; response?: Response }
type Receipt = { request: Request; response?: Response }

const SESSION = 'agent:main:webchat:e2e-receipt-resolution'
test.use({ isolatedRealGatewayInitialSessionKeys: [SESSION] })

async function originMainWalModule(testInfo: TestInfo) {
  const baseline = new URL('./fixtures/legacy-wal/', import.meta.url)
  const provenance = JSON.parse(await readFile(new URL('provenance.json', baseline), 'utf8'))
  const source = (await readFile(new URL('pendingInputWal.schema2.ts.txt', baseline), 'utf8')).replaceAll('\r\n', '\n')
  const sha256 = (value: string) => createHash('sha256').update(value).digest('hex')
  expect(sha256(source)).toBe(provenance.sha256)
  // The immutable historical reader works in a shallow, offline checkout.
  // Pin the old page-context normalizer; reuse only unchanged runtime dependencies.
  for (const [path, hash] of Object.entries(provenance.dependencies)) {
    const dependency = await readFile(path === 'types/pageContext.ts'
      ? new URL('pageContext.schema2.ts.txt', baseline)
      : new URL(`../src/${path}`, import.meta.url), 'utf8')
    expect(sha256(dependency.replaceAll('\r\n', '\n')), path).toBe(hash)
  }
  const modulePath = testInfo.outputPath('origin-main-pendingInputWal.ts')
  await writeFile(modulePath, source.replace(
    "from '@/types/pageContext'", "from './origin-main-pageContext'",
  ))
  await writeFile(testInfo.outputPath('origin-main-pageContext.ts'),
    await readFile(new URL('pageContext.schema2.ts.txt', baseline), 'utf8'))
  await writeFile(testInfo.outputPath('officeSelection.ts'),
    "export { normalizeOfficeSelection } from '@/types/officeSelection'\n")
  await writeFile(testInfo.outputPath('attachments.ts'), "export { snapshotAttachment } from '@/utils/chat/attachments'\n")
  await writeFile(testInfo.outputPath('origin-main-reader.json'), JSON.stringify(provenance, null, 2))
  return `/@fs/${modulePath.replaceAll('\\', '/')}`
}

function logicalRequestId(params: Record<string, unknown>): string {
  const prefix = `tr2:${params.acceptanceEpoch}:`
  expect(String(params.clientRequestId).startsWith(prefix)).toBe(true)
  return String(params.clientRequestId).slice(prefix.length)
}

/** Fault only the transport. Receipt resolution, SQLite and execution are real. */
async function faultFirstSend(page: Page, fault: 'lose-ack' | 'drop-request' | 'hold-request' | 'none', holdReceipts = false) {
  const sends: Send[] = []
  const receipts: Receipt[] = []
  const epochs: string[] = []
  const requests: Request[] = []
  let connections = 0
  let disconnectCurrent: (() => void) | undefined
  await page.routeWebSocket(/\/ws$/, browser => {
    connections += 1
    const server = browser.connectToServer()
    const pendingSends = new Map<string, Send>()
    const pendingReceipts = new Map<string, Receipt>()
    const disconnect = () => {
      browser.close({ code: 1012, reason: `Synthetic ${fault}` })
      server.close({ code: 1012, reason: `Synthetic ${fault}` })
    }
    disconnectCurrent = disconnect
    browser.onMessage(raw => {
      const request = JSON.parse(String(raw)) as Request
      if (request.type === 'req') requests.push(request)
      if (request.type === 'req' && /^chat\.send(?:\.v2)?$/.test(request.method)) {
        const send: Send = { request, forwarded: !(['drop-request', 'hold-request'].includes(fault) && sends.length === 0) }
        sends.push(send)
        if (!send.forwarded) {
          // No server.send occurs: the server has no acceptance to infer.
          if (fault !== 'hold-request') disconnect()
          return
        }
        pendingSends.set(request.id, send)
      }
      if (request.type === 'req' && request.method.startsWith('turns.receipt.')) {
        const receipt = { request }
        receipts.push(receipt)
        pendingReceipts.set(request.id, receipt)
        // Keep the original browser WAL unresolved until the real server has
        // restarted. No fake receipt is ever supplied to the application.
        if (holdReceipts) return
      }
      server.send(raw)
    })
    server.onMessage(raw => {
      const response = JSON.parse(String(raw)) as Response
      if (response.type === 'hello-ok') {
        const policy = (response as Response & { policy?: Record<string, unknown> }).policy
        if (typeof policy?.turn_receipt_epoch === 'string') epochs.push(policy.turn_receipt_epoch)
      }
      const receipt = response.type === 'res' ? pendingReceipts.get(response.id) : undefined
      if (receipt) {
        receipt.response = response
        pendingReceipts.delete(response.id)
      }
      const send = response.type === 'res' ? pendingSends.get(response.id) : undefined
      if (send) {
        send.response = response
        pendingSends.delete(response.id)
        if (send === sends[0] && fault === 'lose-ack') {
          // The real positive response proves acceptance before the socket breaks.
          disconnect()
          return
        }
      }
      browser.send(raw)
    })
  })
  return { sends, receipts, epochs, requests, connections: () => connections,
    disconnect() { disconnectCurrent?.() },
    releaseReceiptQueries() { holdReceipts = false } }
}

async function openChat(page: Page, controlUrl: string) {
  await page.addInitScript(() => localStorage.setItem('opensquilla-locale', 'en'))
  await page.goto(`${controlUrl}chat?session=${encodeURIComponent(SESSION)}`)
  await expect(page.locator('.topbar[data-connection-state="connected"]')).toBeVisible({ timeout: 30_000 })
  return {
    input: page.locator('.chat-textarea'),
    send: page.locator('.chat-send-btn[aria-label="Send"]'),
    recovery: page.getByTestId('chat-delivery-recovery'),
    check: page.getByTestId('chat-delivery-recovery-check'),
  }
}

async function reloadChat(page: Page) {
  await page.reload()
  await expect(page.locator('.topbar[data-connection-state="connected"]')).toBeVisible({ timeout: 30_000 })
  await expect(page.locator('.chat-textarea')).toBeVisible({ timeout: 30_000 })
}

async function evidence(page: Page, testInfo: TestInfo, network: Awaited<ReturnType<typeof faultFirstSend>>) {
  // This browser is an isolated fixture: retain its synthetic input ownership
  // on failure, so a missing draft is not diagnosed from a screenshot alone.
  const localRows = await page.evaluate(async () => {
    const database = await new Promise<IDBDatabase>((resolve, reject) => {
      const request = indexedDB.open('opensquilla-chat-pending-inputs')
      request.onsuccess = () => resolve(request.result)
      request.onerror = () => reject(request.error)
    })
    try {
      const stores = Array.from(database.objectStoreNames)
      const rows: Record<string, unknown> = {}
      await Promise.all(stores.map(async name => {
        rows[name] = await new Promise((resolve, reject) => {
          const request = database.transaction(name, 'readonly').objectStore(name).getAll()
          request.onsuccess = () => resolve(request.result)
          request.onerror = () => reject(request.error)
        })
      }))
      return rows
    } finally { database.close() }
  }).catch(error => ({ unavailable: String(error) }))
  const composerState = await page.evaluate(() => {
    type Component = {
      type?: { __file?: string; __name?: string; name?: string }
      setupState?: Record<string, unknown>
      parent?: Component
    }
    const input = document.querySelector('.chat-textarea') as (Element & { __vueParentComponent?: Component }) | null
    let component = input?.__vueParentComponent
    const parents: string[] = []
    while (component) {
      const name = component.type?.__file || component.type?.__name || component.type?.name || '?'
      parents.push(name)
      if (name.includes('ChatView')) {
        const state = component.setupState || {}
        const fields: Record<string, unknown> = {}
        for (const key of [
          'inputText', 'composerText', 'localPaths', 'localPathReferences', 'selectedSkills',
          'pendingAttachments', 'pendingSessionIntent', 'pendingForkBeforeMessageId',
          'pendingWorkspaceId', 'activePromptAnnotations', 'sessionKey', 'deliveryIdentity',
        ]) fields[key] = state[key] ?? null
        return JSON.parse(JSON.stringify({ parents, fields }))
      }
      component = component.parent
    }
    return { parents, unavailable: true }
  }).catch(error => ({ unavailable: String(error) }))
  const composerDraft = await page.evaluate(sessionKey => (
    localStorage.getItem(`opensquilla.chat.draft:${sessionKey}`)
  ), SESSION).catch(error => ({ unavailable: String(error) }))
  const path = testInfo.outputPath('receipt-resolution-network.json')
  await writeFile(path, JSON.stringify({
    sends: network.sends, receipts: network.receipts, epochs: network.epochs,
    requests: network.requests, connections: network.connections(),
    localRows, composerState, composerDraft,
  }, null, 2))
  await testInfo.attach('receipt-resolution-network', { path, contentType: 'application/json' })
}

test('lost positive acknowledgement reads the real receipt without resending', async ({
  page, isolatedRealGateway,
}, testInfo) => {
  test.setTimeout(90_000)
  const network = await faultFirstSend(page, 'lose-ack')
  const rendererErrors: string[] = []
  page.on('pageerror', error => rendererErrors.push(error.message))
  try {
    const { input, send, recovery } = await openChat(page, isolatedRealGateway.controlUrl)
    const prompt = 'Keep the accepted original turn when its acknowledgement is lost.'
    await input.fill(prompt)
    await expect(send).toBeEnabled()
    await send.click()
    await expect.poll(() => network.sends[0]?.response?.ok).toBe(true)
    expect(network.sends[0]?.request.method).toBe('chat.send.v2')
    await expect.poll(() => network.receipts.some(item => item.response?.payload?.status === 'found'), {
      timeout: 30_000,
    }).toBe(true)
    await expect.poll(async () => (await isolatedRealGateway.readProviderCalls()).length).toBe(1)
    await isolatedRealGateway.releaseFirstTask()
    await expect(page.locator('.msg-ai').filter({ hasText: 'Task one completed after the lifecycle checks.' })).toBeVisible({ timeout: 30_000 })
    await expect(recovery).toHaveCount(0)
    await expect(send).toBeEnabled()
    await reloadChat(page)
    await expect(page.locator('.msg-user').filter({ hasText: prompt })).toHaveCount(1)
    await expect(recovery).toHaveCount(0)
    expect(network.sends).toHaveLength(1)
    expect(network.receipts.every(item => item.request.method === 'turns.receipt.get')).toBe(true)
    expect((await isolatedRealGateway.readProviderCalls()).length).toBe(1)
    expect(rendererErrors).toEqual([])
    await page.screenshot({ path: testInfo.outputPath('accepted-once-after-reload.png') })
  } finally {
    await evidence(page, testInfo, network)
  }
})

test('explicit Check retires a request that never reached the server and restores a fresh send', async ({
  page, isolatedRealGateway,
}, testInfo) => {
  test.setTimeout(90_000)
  const network = await faultFirstSend(page, 'drop-request')
  const rendererErrors: string[] = []
  page.on('pageerror', error => rendererErrors.push(error.message))
  try {
    const { input, send, recovery, check } = await openChat(page, isolatedRealGateway.controlUrl)
    const prompt = 'Restore this original synthetic draft after confirmed non-acceptance.'
    await input.fill(prompt)
    await expect(send).toBeEnabled()
    await send.click()
    await expect.poll(() => network.sends.length).toBe(1)
    expect(network.sends[0]?.request.method).toBe('chat.send.v2')
    expect(network.sends[0]?.forwarded).toBe(false)
    await expect.poll(() => network.receipts.some(item => item.response?.payload?.status === 'not_found'), {
      timeout: 30_000,
    }).toBe(true)
    await expect(recovery).toBeVisible()
    await expect(check).toBeEnabled()
    expect((await isolatedRealGateway.readProviderCalls()).length).toBe(0)
    expect(network.receipts.every(item => item.request.method === 'turns.receipt.get')).toBe(true)
    await check.click()
    await expect.poll(() => network.receipts.find(item => item.request.method === 'turns.receipt.resolve')?.response?.payload?.status, {
      timeout: 30_000,
    }).toBe('retired')
    await expectComposerValue(input, prompt)
    await expect(recovery).toHaveCount(0)
    await expect(page.locator('.msg-ai')).toHaveCount(0)
    await expect(send).toBeEnabled()
    expect(network.sends).toHaveLength(1)
    expect((await isolatedRealGateway.readProviderCalls()).length).toBe(0)
    await page.screenshot({ path: testInfo.outputPath('retired-draft-restored.png') })
    await send.click()
    await expect.poll(() => network.sends[1]?.response?.ok).toBe(true)
    expect(network.sends[1]?.request.method).toBe('chat.send.v2')
    expect(network.sends[1]?.request.params.clientRequestId).not.toBe(network.sends[0]?.request.params.clientRequestId)
    expect(logicalRequestId(network.sends[1]!.request.params)).not.toBe(logicalRequestId(network.sends[0]!.request.params))
    await expect.poll(async () => (await isolatedRealGateway.readProviderCalls()).length).toBe(1)
    await isolatedRealGateway.releaseFirstTask()
    await expect(page.locator('.msg-ai').filter({ hasText: 'Task one completed after the lifecycle checks.' })).toBeVisible({ timeout: 30_000 })
    await reloadChat(page)
    await expect(page.locator('.msg-user').filter({ hasText: prompt })).toHaveCount(1)
    await expect(recovery).toHaveCount(0)
    expect(network.sends.filter(item => item.forwarded)).toHaveLength(1)
    expect((await isolatedRealGateway.readProviderCalls()).length).toBe(1)
    expect(rendererErrors).toEqual([])
  } finally {
    await evidence(page, testInfo, network)
  }
})

test('annotation-only draft survives retirement and reload before one fresh execution', async ({
  page, isolatedRealGateway,
}, testInfo) => {
  test.setTimeout(90_000)
  const network = await faultFirstSend(page, 'drop-request')
  const annotationBody = 'Clarify the selected synthetic heading.'
  const stateDir = testInfo.outputPath('isolated-real-gateway', 'state')
  const repoRoot = fileURLToPath(new URL('../..', import.meta.url))
  const python = process.env.OPENSQUILLA_WEBUI_E2E_PYTHON
    || join(repoRoot, '.venv', process.platform === 'win32' ? 'Scripts' : 'bin',
      process.platform === 'win32' ? 'python.exe' : 'python')
  const { stdout } = await promisify(execFile)(python, [
    '-I', fileURLToPath(new URL('./fixtures/receipt-annotation-document.py', import.meta.url)), stateDir, SESSION,
  ], {
    cwd: stateDir, windowsHide: true, timeout: 30_000,
    env: {
      PATH: process.env.PATH, SYSTEMROOT: process.env.SYSTEMROOT,
      HOME: stateDir, USERPROFILE: stateDir, APPDATA: join(stateDir, 'appdata'),
      LOCALAPPDATA: join(stateDir, 'local-appdata'), OPENSQUILLA_HOME: stateDir,
      OPENSQUILLA_STATE_DIR: stateDir,
    },
  })
  const document = JSON.parse(stdout) as {
    documentId: string; resourceId: string; revisionId: string; sha256: string
  }
  await testInfo.attach('annotation-document', { body: JSON.stringify(document), contentType: 'application/json' })
  // Seed a completed immutable draft for a real session-owned document/version.
  // Unversioned legacy drafts are intentionally stale; do not bypass that guard.
  // Every send, receipt, reload and execution below remains production.
  await page.addInitScript(({ sessionKey, body, document }) => {
    Object.assign(window, { OPENSQUILLA_FEATURES: { artifactPromptAnnotations: true } })
    if (localStorage.getItem('receipt-annotation-fixture-seeded')) return
    localStorage.setItem('receipt-annotation-fixture-seeded', '1')
    localStorage.setItem('opensquilla.page-annotation-drafts.v1', JSON.stringify([{
      annotationId: 'receipt-annotation-fixture', sessionKey,
      ...document, documentName: 'fixture.html',
      targetRef: 'synthetic-heading', body, status: 'draft', tagName: 'h1',
      quote: 'Synthetic heading', targetText: 'Synthetic heading', createdAt: 1, updatedAt: 1,
    }]))
  }, { sessionKey: SESSION, body: annotationBody, document })
  try {
    const { input, send, recovery, check } = await openChat(page, isolatedRealGateway.controlUrl)
    const chip = page.locator('.chat-prompt-annotation-chip').filter({ hasText: annotationBody })
    await expect(chip).toBeVisible()
    await expectComposerValue(input, '')
    await expect(send).toBeEnabled()
    await send.click()
    await expect.poll(() => network.receipts.some(item => item.response?.payload?.status === 'not_found'), {
      timeout: 30_000,
    }).toBe(true)
    expect(network.sends[0]?.forwarded).toBe(false)
    const original = network.sends[0]!.request.params
    expect(original.pageContext).toMatchObject({
      resourceId: document.resourceId, revisionId: document.revisionId, sha256: document.sha256,
      annotations: [{ text: annotationBody, selectionText: 'Synthetic heading' }],
    })
    await expect(check).toBeEnabled()
    await check.click()
    await expect.poll(() => network.receipts.find(item => item.request.method === 'turns.receipt.resolve')?.response?.payload?.status, {
      timeout: 30_000,
    }).toBe('retired')
    await expect(chip).toBeVisible()
    await expectComposerValue(input, '')
    await expect(recovery).toHaveCount(0)
    expect((await isolatedRealGateway.readProviderCalls()).length).toBe(0)
    await reloadChat(page)
    await expect(chip).toBeVisible()
    await expect(send).toBeEnabled()
    expect(network.sends).toHaveLength(1)
    await send.click()
    await expect.poll(() => network.sends[1]?.response?.ok).toBe(true)
    expect(network.sends[1]?.request.params.pageContext).toEqual(original.pageContext)
    expect(network.sends[1]?.request.params.clientRequestId).not.toBe(original.clientRequestId)
    expect(logicalRequestId(network.sends[1]!.request.params)).not.toBe(logicalRequestId(original))
    await expect.poll(async () => (await isolatedRealGateway.readProviderCalls()).length).toBe(1)
    await isolatedRealGateway.releaseFirstTask()
    await expect(page.locator('.msg-ai').filter({ hasText: 'Task one completed after the lifecycle checks.' })).toBeVisible({ timeout: 30_000 })
    await expect(chip).toHaveCount(0)
    await reloadChat(page)
    await expect(page.locator('.msg-user')).toHaveCount(1)
    await expect(chip).toHaveCount(0)
    await expect(recovery).toHaveCount(0)
    expect(network.sends.filter(item => item.forwarded)).toHaveLength(1)
    expect((await isolatedRealGateway.readProviderCalls()).length).toBe(1)
    await page.screenshot({ path: testInfo.outputPath('annotation-sent-once-after-reload.png') })
  } finally {
    await evidence(page, testInfo, network)
  }
})

type DatabaseEvidence = {
  counts: Record<string, number>
  taskStatuses: string[]
}

/** SQLite's backup API includes committed WAL pages; no live DB file copy. */
async function databaseEvidence(source: string, copyTo?: string): Promise<DatabaseEvidence> {
  const repoRoot = fileURLToPath(new URL('../..', import.meta.url))
  const python = process.env.OPENSQUILLA_WEBUI_E2E_PYTHON
    || join(repoRoot, '.venv', process.platform === 'win32' ? 'Scripts' : 'bin',
      process.platform === 'win32' ? 'python.exe' : 'python')
  const script = `
import json, sqlite3, sys
source = sqlite3.connect(sys.argv[1], timeout=5)
try:
    if len(sys.argv) > 2:
        target = sqlite3.connect(sys.argv[2], timeout=5)
        try:
            source.backup(target)
        finally:
            target.close()
    print(json.dumps({
        "counts": {name: source.execute("SELECT count(*) FROM " + name).fetchone()[0]
                   for name in ("turn_request_resolution", "turn_ingress_receipts", "agent_tasks")},
        "taskStatuses": [row[0] for row in source.execute("SELECT status FROM agent_tasks")]
    }))
finally:
    source.close()
`
  const { stdout } = await promisify(execFile)(python,
    ['-I', '-c', script, source, ...(copyTo ? [copyTo] : [])], { windowsHide: true, timeout: 15_000 })
  return JSON.parse(stdout) as DatabaseEvidence
}

for (const restoreBackup of [false, true]) {
  test(restoreBackup
    ? 'old database backup cannot turn an accepted unknown into a resendable draft'
    : 'explicit Check recovers accepted receipt after restart and automatic recovery budget expiry', async ({
    page, isolatedRealGateway,
  }, testInfo) => {
    test.setTimeout(150_000)
    const network = await faultFirstSend(page, 'lose-ack', true)
    const outputDir = testInfo.outputPath('isolated-real-gateway')
    const database = join(outputDir, 'state', 'sessions.db')
    const backup = testInfo.outputPath('before-request.sqlite')
    const states: Record<string, DatabaseEvidence> = {}
    let replacement: RealGoalGateway | undefined
    const rendererErrors: string[] = []
    page.on('pageerror', error => rendererErrors.push(error.message))
    try {
      const { input, send, recovery, check } = await openChat(page, isolatedRealGateway.controlUrl)
      states.before = await databaseEvidence(database, backup)
      const prompt = 'This accepted request must never be reissued after restoring an older database.'
      await input.fill(prompt)
      await expect(send).toBeEnabled()
      await send.click()
      await expect.poll(() => network.sends[0]?.response?.ok).toBe(true)
      const original = network.sends[0]!.request.params
      expect(original.acceptanceEpoch).toBe(network.epochs[0])
      await expect.poll(async () => (await isolatedRealGateway.readProviderCalls()).length).toBe(1)
      await isolatedRealGateway.releaseFirstTask()
      await expect.poll(async () => {
        const state = await databaseEvidence(database)
        return state.taskStatuses.length === 1
          && state.taskStatuses.every(status => ['completed', 'succeeded'].includes(status))
      }, { timeout: 30_000 }).toBe(true)
      states.accepted = await databaseEvidence(database)
      expect(states.accepted.counts.turn_request_resolution).toBe(1)
      expect(states.accepted.counts.turn_ingress_receipts).toBe(1)
      // Receipt replies were withheld, so the real browser still has unknown
      // acceptance even though the provider has finished its one execution.
      await expect.poll(() => network.receipts.length, { timeout: 30_000 }).toBeGreaterThan(0)
      await expect.poll(async () => {
        const row = (await deliveryRows(page)).find(row => row.ownerRequestId === logicalRequestId(original))
        return typeof row?.recovery?.expiresAt === 'number' && row.recovery.expiresAt <= Date.now()
      }, { timeout: 40_000 }).toBe(true)
      await expect(check).toBeEnabled()
      const exhausted = (await deliveryRows(page)).find(row => row.ownerRequestId === logicalRequestId(original))!
      const receiptCount = network.receipts.length
      expect(network.receipts.every(item => !item.response)).toBe(true)
      await isolatedRealGateway.stop()
      if (restoreBackup) await databaseEvidence(backup, database)
      states.beforeRestart = await databaseEvidence(database)
      replacement = await startRealGoalGateway({
        outputDir, webuiOrigin: isolatedRealGateway.webuiOrigin, scenario: 'lifecycle',
        initialSessionKeys: [SESSION], port: Number(new URL(isolatedRealGateway.wsUrl).port),
      })
      await reloadChat(page)
      await expect.poll(() => network.epochs.some(epoch => epoch !== original.acceptanceEpoch), {
        timeout: 30_000,
      }).toBe(true)
      expect(network.sends).toHaveLength(1)
      await expect(recovery).toBeVisible()
      await expect(check).toBeEnabled()
      const restarted = (await deliveryRows(page)).find(row => row.ownerRequestId === logicalRequestId(original))!
      expect(restarted).toMatchObject({
        phase: 'unknown', request: exhausted.request, recovery: exhausted.recovery,
      })
      expect(network.receipts).toHaveLength(receiptCount)
      expect((await replacement.readProviderCalls()).length).toBe(1)
      await writeFile(testInfo.outputPath('restart-recovery-budget.json'), JSON.stringify({
        before: exhausted.recovery, after: restarted.recovery,
        ownerRequestId: restarted.ownerRequestId, acceptanceEpoch: original.acceptanceEpoch,
        receiptCountBeforeCheck: receiptCount,
      }, null, 2))
      network.releaseReceiptQueries()
      await check.click()
      await expect.poll(() => network.receipts.slice(receiptCount).some(item =>
        item.request.method === 'turns.receipt.resolve'
        && item.response?.payload?.status === (restoreBackup ? 'not_found' : 'found')),
      { timeout: 30_000 }).toBe(true)
      for (const item of network.receipts) {
        expect(item.request.params.originalRequest).toMatchObject({
          acceptanceEpoch: original.acceptanceEpoch, clientRequestId: original.clientRequestId,
        })
      }

      if (restoreBackup) {
        expect(states.beforeRestart.counts.turn_request_resolution).toBe(0)
        await expect(check).toBeEnabled({ timeout: 40_000 })
        await expect(recovery).toBeVisible()
        await expectComposerValue(input, '')
        expect(network.receipts.every(item => item.response?.payload?.status !== 'retired')).toBe(true)
        states.checked = await databaseEvidence(database)
        expect(states.checked.counts.turn_request_resolution).toBe(0)
        expect(states.checked.counts.turn_ingress_receipts).toBe(0)
        expect((await replacement.readProviderCalls()).length).toBe(1)
        await page.screenshot({ path: testInfo.outputPath('rollback-request-remains-protected.png') })
        await reloadChat(page)
        await expect(recovery).toBeVisible()
        expect(network.sends).toHaveLength(1)

        // Keep every protection above, and continue in this original session.
        // Only this explicit click authorizes a new request after the restore.
        const continuation = 'An intentional new message in the original session after the restore.'
        expect(new URL(page.url()).searchParams.get('session')).toBe(SESSION)
        await input.fill(continuation)
        await expect(send).toBeEnabled({ timeout: 30_000 })
        expect(network.sends).toHaveLength(1)
        await send.click()
        await expect.poll(() => network.sends[1]?.response?.ok).toBe(true)
        expect(network.sends[1]!.request.params.sessionKey).toBe(SESSION)
        expect(network.sends[1]!.request.params.acceptanceEpoch).not.toBe(original.acceptanceEpoch)
        expect(network.sends[1]!.request.params.clientRequestId).not.toBe(original.clientRequestId)
        expect(logicalRequestId(network.sends[1]!.request.params)).not.toBe(logicalRequestId(original))
        await expect.poll(async () => (await replacement!.readProviderCalls()).length).toBe(2)
        await expect(page.locator('.msg-user').filter({ hasText: continuation })).toHaveCount(1)
        await expect(page.locator('.msg-ai').filter({ hasText: 'Task one completed after the lifecycle checks.' })).toBeVisible()
        await reloadChat(page)
        await expect(recovery).toBeVisible()
        await expect(page.locator('.msg-user').filter({ hasText: continuation })).toHaveCount(1)
        expect(network.sends).toHaveLength(2)
        expect(network.sends.filter(item => item.request.params.clientRequestId === original.clientRequestId)).toHaveLength(1)
        expect(network.requests.filter(item => item.method === 'chat.abort')).toEqual([])
      } else {
        await expect(recovery).toHaveCount(0)
        await expect(page.locator('.msg-user').filter({ hasText: prompt })).toHaveCount(1)
        await expect(page.locator('.msg-ai').filter({ hasText: 'Task one completed after the lifecycle checks.' })).toBeVisible()
        expect(network.sends).toHaveLength(1)
        expect((await replacement.readProviderCalls()).length).toBe(1)
        await page.screenshot({ path: testInfo.outputPath('restart-positive-receipt-recovered.png') })
      }
      expect(rendererErrors).toEqual([])
    } finally {
      await evidence(page, testInfo, network)
      await writeFile(testInfo.outputPath('database-rollback-evidence.json'), JSON.stringify(states, null, 2))
      await replacement?.stop()
    }
  })
}

async function deliveryRows(page: Page) {
  return page.evaluate(async () => {
    const database = await new Promise<IDBDatabase>((resolve, reject) => {
      const request = indexedDB.open('opensquilla-chat-pending-inputs')
      request.onsuccess = () => resolve(request.result)
      request.onerror = () => reject(request.error)
    })
    try {
      return await new Promise<Array<Record<string, any>>>((resolve, reject) => {
        const request = database.transaction('response_handoffs', 'readonly')
          .objectStore('response_handoffs').getAll()
        request.onsuccess = () => resolve(request.result)
        request.onerror = () => reject(request.error)
      })
    } finally { database.close() }
  })
}

for (const scenario of [
  { reload: false, stop: false, label: 'without reloading' },
  { reload: true, stop: false, label: 'after reloading' },
  { reload: true, stop: true, label: 'after explicit Stop and reloading' },
]) {
  test(`an unknown request permits an explicit next message in the same session after restart ${scenario.label}`, async ({
    page, isolatedRealGateway,
  }, testInfo) => {
    test.setTimeout(150_000)
    // Keeping the discarded request's socket open in the Stop case gives the
    // user a deterministic chance to Stop while admission is still pending.
    const network = await faultFirstSend(page, scenario.stop ? 'hold-request' : 'drop-request', true)
    const outputDir = testInfo.outputPath('isolated-real-gateway')
    const database = join(outputDir, 'state', 'sessions.db')
    const states: Record<string, DatabaseEvidence> = {}
    let replacement: RealGoalGateway | undefined
    const rendererErrors: string[] = []
    page.on('pageerror', error => rendererErrors.push(error.message))
    try {
      const { input, send, recovery } = await openChat(page, isolatedRealGateway.controlUrl)
      await input.fill('The original request never reaches this isolated Gateway.')
      await expect(send).toBeEnabled()
      await send.click()
      await expect.poll(() => network.sends.length).toBe(1)
      const original = network.sends[0]!.request.params
      expect(network.sends[0]!.forwarded).toBe(false)
      if (scenario.stop) {
        const stop = page.getByRole('button', { name: 'Stop current response', exact: true })
        await expect(stop).toBeEnabled()
        await stop.click()
        await expect.poll(async () => (await deliveryRows(page))
          .find(row => row.ownerRequestId === logicalRequestId(original))?.stop?.requested).toBe(true)
      }
      states.beforeRestart = await databaseEvidence(database)
      expect(states.beforeRestart.counts.agent_tasks).toBe(0)
      await isolatedRealGateway.stop()
      replacement = await startRealGoalGateway({
        outputDir, webuiOrigin: isolatedRealGateway.webuiOrigin, scenario: 'lifecycle',
        initialSessionKeys: [SESSION], port: Number(new URL(isolatedRealGateway.wsUrl).port),
      })
      if (scenario.reload) await reloadChat(page)
      await expect.poll(() => network.epochs.some(epoch => epoch !== original.acceptanceEpoch), {
        timeout: 40_000,
      }).toBe(true)
      await expect(page.locator('.topbar[data-connection-state="connected"]')).toBeVisible()
      await expect(recovery).toBeVisible()
      // New authority closes old admission without resolving the historical
      // result. Continue without a receipt response or a manual Check.
      await expect.poll(() => page.evaluate(ownerRequestId => {
        let component = (document.querySelector('.chat-textarea') as any)?.__vueParentComponent
        while (component) {
          const state = component.setupState
          if (state?.chatSessionBootstrap) return {
            ready: state.chatSessionBootstrap.isLiveReady(state.sessionKey),
            delivery: state.durableDelivery.snapshots().find((row: { id: string }) => row.id === ownerRequestId),
          }
          component = component.parent
        }
        return null
      }, logicalRequestId(original)), { timeout: 40_000 }).toMatchObject({
        ready: true, delivery: { phase: 'unknown', admissionClosed: true },
      })
      const oldRow = (await deliveryRows(page)).find(row => row.ownerRequestId === logicalRequestId(original))
      expect(oldRow).toMatchObject({
        phase: 'unknown', request: { request: { acceptanceEpoch: original.acceptanceEpoch } },
      })
      if (scenario.stop) expect(oldRow?.stop?.requested).toBe(true)
      expect(network.receipts.every(item => !item.response)).toBe(true)
      expect(new URL(page.url()).searchParams.get('session')).toBe(SESSION)
      expect(network.sends).toHaveLength(1)
      expect((await replacement.readProviderCalls()).length).toBe(0)

      if (!scenario.reload) {
        // Restart closes old admission; it does not authorize an unchanged
        // uncertain input to silently acquire a new request identity.
        await input.fill(String(original.message))
        await expect(send).toBeEnabled({ timeout: 40_000 })
        await send.click()
        await expectComposerValue(input, String(original.message))
        await expect(send).toBeEnabled()
        expect(network.sends).toHaveLength(1)
        expect((await replacement.readProviderCalls()).length).toBe(0)
      }
      const nextPrompt = `Explicit new message ${scenario.label}; keep the original request unresolved.`
      await input.fill(nextPrompt)
      await expect(send).toBeEnabled({ timeout: 40_000 })
      expect(network.sends).toHaveLength(1)
      await send.click()
      await expect.poll(() => network.sends[1]?.response?.ok).toBe(true)
      const next = network.sends[1]!.request.params
      expect(next.sessionKey).toBe(SESSION)
      expect(next.clientRequestId).not.toBe(original.clientRequestId)
      expect(logicalRequestId(next)).not.toBe(logicalRequestId(original))
      expect(next.acceptanceEpoch).not.toBe(original.acceptanceEpoch)
      await expect.poll(async () => (await replacement!.readProviderCalls()).length).toBe(1)
      const stop = page.getByRole('button', { name: 'Stop current response', exact: true })
      await expect(stop).toBeEnabled()
      // A real active task still owns the busy control. Typing alone cannot
      // bypass it, and a previous Stop must never turn into a session abort.
      await input.fill('An unsent next draft while the new task is active.')
      await expect(send).toHaveCount(0)
      expect(network.sends).toHaveLength(2)
      expect(network.requests.filter(item => item.method === 'chat.abort')).toEqual([])
      const queuedText = 'Keep this explicitly queued input behind the unresolved original request.'
      if (!scenario.reload) {
        // Exercise the existing queue shortcut while a real task is active.
        // A nonempty queue is necessary to prove that unknown blocks autodrain.
        await input.fill(queuedText)
        await input.press('End')
        await input.press('Alt+ArrowDown')
        await expect(page.locator('.chat-pending')).toContainText(queuedText)
        await expect.poll(() => network.requests.filter(item => item.method === 'sessions.pending_inputs.enqueue').length).toBe(1)
        expect(network.sends).toHaveLength(2)
      }
      await replacement.releaseFirstTask()
      await expect(page.locator('.msg-ai').filter({ hasText: 'Task one completed after the lifecycle checks.' })).toBeVisible({ timeout: 30_000 })
      await expect(page.locator('.msg-user').filter({ hasText: nextPrompt })).toHaveCount(1)
      await expect(recovery).toBeVisible()
      if (!scenario.reload) await expect(page.locator('.chat-pending')).toContainText(queuedText)
      await reloadChat(page)
      await expect(page.locator('.msg-user').filter({ hasText: nextPrompt })).toHaveCount(1)
      await expect(recovery).toBeVisible()
      if (!scenario.reload) await expect(page.locator('.chat-pending')).toContainText(queuedText)
      expect(network.sends).toHaveLength(2)
      expect(network.sends.filter(item => item.request.params.clientRequestId === original.clientRequestId)).toHaveLength(1)
      expect(network.requests.filter(item => item.method === 'chat.abort')).toEqual([])
      expect(network.requests.filter(item => item.method === 'sessions.pending_inputs.dispatch')).toEqual([])
      expect((await replacement.readProviderCalls()).length).toBe(1)
      const unresolved = (await deliveryRows(page)).find(row => row.ownerRequestId === logicalRequestId(original))
      expect(unresolved).toMatchObject({ phase: 'unknown', request: { request: { acceptanceEpoch: original.acceptanceEpoch } } })
      if (scenario.stop) expect(unresolved?.stop?.requested).toBe(true)
      states.completed = await databaseEvidence(database)
      expect(states.completed.counts.agent_tasks).toBe(1)
      expect(states.completed.taskStatuses.every(status => ['completed', 'succeeded'].includes(status))).toBe(true)
      expect(rendererErrors).toEqual([])
      await page.screenshot({ path: testInfo.outputPath('same-session-after-unknown.png') })
    } finally {
      await evidence(page, testInfo, network)
      await writeFile(testInfo.outputPath('restart-unknown-evidence.json'), JSON.stringify(states, null, 2))
      await replacement?.stop()
    }
  })
}

for (const schemaVersion of [3, 2] as const) {
test(schemaVersion === 3
  ? 'a prepared WAL request first dispatches once with the restarted Gateway epoch'
  : 'an origin-main schema2 prepared WAL upgrades and first dispatches once without protocol promotion', async ({
  page, isolatedRealGateway,
}, testInfo) => {
  test.setTimeout(120_000)
  const network = await faultFirstSend(page, 'none')
  const outputDir = testInfo.outputPath('isolated-real-gateway')
  const ownerRequestId = 'e2e-prepared-before-restart'
  const prompt = 'Dispatch this previously prepared request for the first time after restart.'
  let replacement: RealGoalGateway | undefined
  try {
    await openChat(page, isolatedRealGateway.controlUrl)
    const identity = await page.evaluate(() => {
      let component = (document.querySelector('.chat-textarea') as any)?.__vueParentComponent
      while (component) {
        const identity = component.setupState?.deliveryIdentity
        if (typeof identity === 'string' && identity) return identity
        component = component.parent
      }
      throw new Error('The real application has no authenticated delivery identity')
    })
    const oldEpoch = network.epochs.at(-1)!
    expect(oldEpoch).toBeTruthy()
    // A blank same-origin fixture has IndexedDB access but no running app owner.
    // This seeds the durable pre-dispatch boundary deterministically; it does
    // not simulate a timed crash or claim the UI restores prepared composer text.
    const seedUrl = `${isolatedRealGateway.controlUrl}receipt-wal-fixture`
    await page.route(seedUrl, route => route.fulfill({ contentType: 'text/html', body: '<!doctype html><title>WAL fixture</title>' }))
    await page.goto(seedUrl)
    const request = {
      kind: 'new-turn' as const,
      ...(schemaVersion === 3 ? { acceptanceProtocol: 'resolvable-v1' as const, acceptanceEpoch: oldEpoch } : {}),
      params: { sessionKey: SESSION, message: prompt, clientRequestId: ownerRequestId, clientMessageId: `${ownerRequestId}-message` },
    }
    const legacyModuleUrl = schemaVersion === 2 ? await originMainWalModule(testInfo) : undefined
    await page.evaluate(async ({ ownerRequestId, identity, sessionKey, request, schemaVersion, legacyModuleUrl }) => {
      const record = {
        schemaVersion, ownerRequestId, deliveryIdentity: identity, requestSessionKey: sessionKey,
        request: { kind: 'send', request }, phase: 'prepared', revision: 1,
        recoveryState: 'pending', createdAt: Date.now(), updatedAt: Date.now(),
      }
      if (legacyModuleUrl) {
        const { createPendingInputWal } = await import(/* @vite-ignore */ legacyModuleUrl)
        const wal = createPendingInputWal()
        try {
          if (!(await wal.prepareDelivery(record)).applied) throw new Error('The origin/main reader did not prepare schema2')
        } finally { wal.close() }
        return
      }
      const database = await new Promise<IDBDatabase>((resolve, reject) => {
        const opening = indexedDB.open('opensquilla-chat-pending-inputs')
        opening.onsuccess = () => resolve(opening.result)
        opening.onerror = () => reject(opening.error)
      })
      try {
        await new Promise<void>((resolve, reject) => {
          const transaction = database.transaction('response_handoffs', 'readwrite')
          transaction.objectStore('response_handoffs').put(record)
          transaction.oncomplete = () => resolve()
          transaction.onerror = () => reject(transaction.error)
          transaction.onabort = () => reject(transaction.error)
        })
      } finally { database.close() }
    }, { ownerRequestId, identity, sessionKey: SESSION, request, schemaVersion, legacyModuleUrl })
    expect(network.sends).toHaveLength(0)
    await isolatedRealGateway.stop()
    replacement = await startRealGoalGateway({
      outputDir, webuiOrigin: isolatedRealGateway.webuiOrigin, scenario: 'lifecycle',
      initialSessionKeys: [SESSION], port: Number(new URL(isolatedRealGateway.wsUrl).port),
    })
    await openChat(page, isolatedRealGateway.controlUrl)
    await expect.poll(() => network.epochs.at(-1)).not.toBe(oldEpoch)
    expect((await deliveryRows(page)).find(row => row.ownerRequestId === ownerRequestId)).toMatchObject({
      schemaVersion, phase: 'prepared', request: { request },
    })
    expect(network.sends).toHaveLength(0)
    // Invoke the actual injected browser owner for this explicit first dispatch.
    // No test hook, replacement delivery implementation, or generated new ID.
    await page.evaluate(async request => {
      let component = (document.querySelector('.chat-textarea') as any)?.__vueParentComponent
      while (component) {
        const delivery = component.setupState?.durableDelivery
        if (delivery) {
          await delivery.commands.send(request)
          return
        }
        component = component.parent
      }
      throw new Error('The real application has no durable delivery owner')
    }, request)
    await expect.poll(() => network.sends[0]?.response?.ok).toBe(true)
    expect(network.sends).toHaveLength(1)
    expect(network.sends[0]!.request.method).toBe(schemaVersion === 3 ? 'chat.send.v2' : 'chat.send')
    expect(network.sends[0]!.request.params).toMatchObject({ sessionKey: SESSION, message: prompt })
    if (schemaVersion === 3) {
      expect(logicalRequestId(network.sends[0]!.request.params)).toBe(ownerRequestId)
      expect(network.sends[0]!.request.params.acceptanceEpoch).toBe(network.epochs.at(-1))
      expect(network.sends[0]!.request.params.acceptanceEpoch).not.toBe(oldEpoch)
    } else {
      expect(network.sends[0]!.request.params.clientRequestId).toBe(ownerRequestId)
      expect(network.sends[0]!.request.params.acceptanceEpoch).toBeUndefined()
    }
    await expect.poll(async () => (await replacement!.readProviderCalls()).length).toBe(1)
    await replacement.releaseFirstTask()
    await expect(page.locator('.msg-ai').filter({ hasText: 'Task one completed after the lifecycle checks.' })).toBeVisible({ timeout: 30_000 })
    await reloadChat(page)
    await expect(page.locator('.msg-user').filter({ hasText: prompt })).toHaveCount(1)
    expect(network.sends).toHaveLength(1)
    expect((await replacement.readProviderCalls()).length).toBe(1)
    expect((await deliveryRows(page)).find(row => row.ownerRequestId === ownerRequestId)).toMatchObject({ phase: 'accepted' })
    await page.screenshot({ path: testInfo.outputPath('prepared-first-dispatch-after-restart.png') })
  } finally {
    await evidence(page, testInfo, network)
    await replacement?.stop()
  }
})
}

test('the origin-main WAL reader preserves a pending Stop and draft bytes before upgrading again', async ({
  page, isolatedRealGateway,
}, testInfo) => {
  test.setTimeout(120_000)
  const network = await faultFirstSend(page, 'hold-request', true)
  const draft = 'Keep this unsent draft while its earlier request remains unknown.'
  const attachment = 'Exact synthetic attachment bytes: \u03b1\u03b2\u03b3\n'
  const annotation = 'Keep this draft annotation across the reader downgrade.'
  await page.addInitScript(() => Object.assign(window, { OPENSQUILLA_FEATURES: { artifactPromptAnnotations: true } }))
  try {
    const { input, send, recovery } = await openChat(page, isolatedRealGateway.controlUrl)
    await input.fill('The pending request is dropped before admission.')
    await expect(send).toBeEnabled()
    await send.click()
    await expect.poll(() => network.sends.length).toBe(1)
    const ownerRequestId = logicalRequestId(network.sends[0]!.request.params)
    const stop = page.getByRole('button', { name: 'Stop current response', exact: true })
    await expect(stop).toBeEnabled()
    await stop.click()
    await expect.poll(async () => (await deliveryRows(page)).find(row => row.ownerRequestId === ownerRequestId))
      .toMatchObject({ stop: { requested: true } })
    // Stop records intent while the ACK is pending. Disconnect that held
    // request to establish an unknown outcome before testing reader recovery.
    network.disconnect()
    await expect.poll(async () => (await deliveryRows(page)).find(row => row.ownerRequestId === ownerRequestId))
      .toMatchObject({ phase: 'unknown', stop: { requested: true } })
    await expect(page.locator('.topbar[data-connection-state="connected"]')).toBeVisible()
    await input.fill(draft)
    await page.locator('input[type="file"]').setInputFiles({
      name: 'retained.txt', mimeType: 'text/plain', buffer: Buffer.from(attachment),
    })
    await expect(page.locator('.attachment-chip')).toContainText('retained.txt')
    await expect(page.locator('.attachment-chip--busy')).toHaveCount(0)
    await page.evaluate(async () => {
      let component = (document.querySelector('.chat-textarea') as any)?.__vueParentComponent
      while (component) {
        const attachments = component.setupState?.chatAttachments
        if (attachments) {
          if (!(await attachments.flushAttachmentDraft())) throw new Error('The real attachment draft did not persist')
          return
        }
        component = component.parent
      }
      throw new Error('The real attachment owner is unavailable')
    })
    // Annotation creation uses the same immutable fixture boundary as the
    // existing annotation test; send, Stop, file intake and reload are real.
    await page.addInitScript(({ sessionKey, annotation }) => {
      if (localStorage.getItem('wal-downgrade-annotation-seeded')) return
      localStorage.setItem('wal-downgrade-annotation-seeded', '1')
      localStorage.setItem('opensquilla.page-annotation-drafts.v1', JSON.stringify([{
        annotationId: 'wal-downgrade-annotation', sessionKey,
        documentId: 'synthetic-page', documentName: 'fixture.html', targetRef: 'heading',
        body: annotation, status: 'draft', tagName: 'h1', quote: 'Heading', targetText: 'Heading',
        createdAt: 1, updatedAt: 1,
      }]))
    }, { sessionKey: SESSION, annotation })
    await reloadChat(page)
    await expect(recovery).toBeVisible()
    await expectComposerValue(input, draft)
    await expect(page.locator('.attachment-chip')).toContainText('retained.txt')
    await expect(page.locator('.chat-prompt-annotation-chip')).toContainText(annotation)
    expect(network.sends).toHaveLength(1)

    const readerUrl = await originMainWalModule(testInfo)
    const probeUrl = `${isolatedRealGateway.controlUrl}wal-reader-probe`
    await page.route(probeUrl, route => route.fulfill({ contentType: 'text/html', body: '<!doctype html><title>Old WAL reader</title>' }))
    await page.goto(probeUrl)
    const compatibility = await page.evaluate(async ({ readerUrl, ownerRequestId, sessionKey }) => {
      async function rows(name: string, store: string) {
        const db = await new Promise<IDBDatabase>((resolve, reject) => {
          const request = indexedDB.open(name)
          request.onsuccess = () => resolve(request.result)
          request.onerror = () => reject(request.error)
        })
        try {
          return await new Promise<any[]>((resolve, reject) => {
            const request = db.transaction(store).objectStore(store).getAll()
            request.onsuccess = () => resolve(request.result)
            request.onerror = () => reject(request.error)
          })
        } finally { db.close() }
      }
      async function bytes(value: any): Promise<any> {
        if (value instanceof Blob) return { type: value.type, bytes: Array.from(new Uint8Array(await value.arrayBuffer())) }
        if (Array.isArray(value)) return Promise.all(value.map(bytes))
        if (value && typeof value === 'object') return Object.fromEntries(
          await Promise.all(Object.entries(value).map(async ([key, child]) => [key, await bytes(child)])))
        return value
      }
      const snapshot = async () => ({
        deliveries: await bytes(await rows('opensquilla-chat-pending-inputs', 'response_handoffs')),
        queue: await bytes(await rows('opensquilla-chat-pending-inputs', 'pending_chat_inputs')),
        attachments: await bytes(await rows('opensquilla-attachment-drafts', 'drafts')),
        annotations: localStorage.getItem('opensquilla.page-annotation-drafts.v1'),
        draft: localStorage.getItem(`opensquilla.chat.draft:${sessionKey}`),
      })
      const before = await snapshot()
      const original = (await rows('opensquilla-chat-pending-inputs', 'response_handoffs'))
        .find(row => row.ownerRequestId === ownerRequestId)
      const { createPendingInputWal } = await import(/* @vite-ignore */ readerUrl)
      const old = createPendingInputWal()
      try {
        const legacy = structuredClone(original)
        legacy.schemaVersion = 2
        delete legacy.request.request.acceptanceProtocol
        delete legacy.request.request.acceptanceEpoch
        const read = await old.getDelivery(ownerRequestId)
        const recoveries = await old.listRecoveryDeliveries()
        const prepare = await old.prepareDelivery(legacy)
        const replace = await old.compareAndSwapDelivery(ownerRequestId, original.revision, legacy)
        const remove = await old.compareAndSwapDelivery(ownerRequestId, original.revision, null)
        const handoffs = await old.listHandoffs(sessionKey)
        let putError = ''
        try { await old.putHandoff(original.handoff) } catch (error) { putError = String(error) }
        await old.deleteHandoff(ownerRequestId)
        return { before, after: await snapshot(), read, recoveries, prepare, replace, remove, handoffs, putError }
      } finally { old.close() }
    }, { readerUrl, ownerRequestId, sessionKey: SESSION })
    expect(compatibility.read).toBeNull()
    expect(compatibility.recoveries).toEqual({ records: [] })
    expect(compatibility.handoffs).toEqual([])
    for (const mutation of [compatibility.prepare, compatibility.replace, compatibility.remove]) {
      expect(mutation).toEqual({ applied: false, record: null })
    }
    expect(compatibility.putError).toContain('quarantined')
    expect(compatibility.after).toEqual(compatibility.before)
    expect(JSON.stringify(compatibility.before.attachments)).toContain(JSON.stringify(Array.from(Buffer.from(attachment))))
    await writeFile(testInfo.outputPath('wal-reader-compatibility.json'), JSON.stringify(compatibility, null, 2))
    expect(network.sends).toHaveLength(1)
    expect((await isolatedRealGateway.readProviderCalls()).length).toBe(0)

    await openChat(page, isolatedRealGateway.controlUrl)
    await expect(recovery).toBeVisible()
    await expectComposerValue(input, draft)
    await expect(page.locator('.attachment-chip')).toContainText('retained.txt')
    await expect(page.locator('.chat-prompt-annotation-chip')).toContainText(annotation)
    expect((await deliveryRows(page)).find(row => row.ownerRequestId === ownerRequestId))
      .toMatchObject({ schemaVersion: 3, phase: 'unknown', stop: { requested: true } })
    expect(network.sends).toHaveLength(1)
    expect(network.requests.filter(request => request.method === 'chat.abort')).toEqual([])
    expect((await isolatedRealGateway.readProviderCalls()).length).toBe(0)
    await page.screenshot({ path: testInfo.outputPath('upgraded-pending-stop-and-draft.png') })
  } finally { await evidence(page, testInfo, network) }
})
