import { expect, test, type Page } from '@playwright/test'

const CONTROL_URL = '/control/'
const IMPORT_METHODS = [
  'memory.import.info',
  'memory.import.start',
  'memory.import.status',
  'memory.import.cancel',
  'memory.import.retry',
  'memory.import.apply',
  'memory.import.undo',
  'memory.import.discard',
] as const

type RpcFrame = {
  id?: string | number
  method?: string
  params?: Record<string, unknown>
  protocol?: number
  features?: {
    methods?: unknown[]
  }
  type?: string
}

type ImportGateway = {
  applyParams?: Record<string, unknown>
  previewParams?: Record<string, unknown>
  releasePreview?: () => void
}

function response(id: string | number | undefined, payload: unknown) {
  return JSON.stringify({ type: 'res', id, ok: true, payload })
}

function previewPayload() {
  return {
    schemaVersion: 1,
    previewId: 'preview-e2e-1',
    batchId: 'batch-e2e-1',
    candidateHash: 'candidate-hash-e2e',
    provider: 'synthetic-provider',
    model: 'synthetic-model',
    summary: ['Added one confirmed working-style preference.'],
    decisionCounts: { applied: 1, duplicate: 2, unresolved: 1 },
    files: [{
      target: 'MEMORY',
      displayName: 'MEMORY.md',
      relativePath: 'MEMORY.md',
      status: 'modified',
      additions: 1,
      deletions: 1,
      diff: [
        '--- a/MEMORY.md',
        '+++ b/MEMORY.md',
        '@@ -1 +1 @@',
        '-Keep the existing local preference.',
        '+Keep answers concise and evidence-backed.',
      ].join('\n'),
    }],
  }
}

function jobPayload(status: 'analyzing' | 'ready') {
  return {
    schemaVersion: 1,
    jobId: 'job-e2e-1',
    batchId: 'batch-e2e-1',
    status,
    stage: status === 'ready' ? 'diff' : 'model',
    provider: 'synthetic-provider',
    model: 'synthetic-model',
    startedAt: '2026-07-28T08:00:00Z',
    canRetry: false,
    errorCode: null,
    preview: status === 'ready' ? previewPayload() : null,
  }
}

async function installImportGateway(page: Page): Promise<ImportGateway> {
  const capture: ImportGateway = {}
  let previewReady = false

  await page.routeWebSocket(/\/ws$/, ws => {
    ws.onMessage(message => {
      let frame: RpcFrame | undefined
      try {
        frame = JSON.parse(String(message)) as RpcFrame
      } catch {
        return
      }

      if (frame.type === 'req' && frame.method === 'connect') {
        ws.send(JSON.stringify({
          type: 'hello-ok',
          protocol: 3,
          server: { version: 'e2e', conn_id: 'memory-import-e2e' },
          features: { methods: [...IMPORT_METHODS], events: [] },
          snapshot: {},
          policy: { tick_interval_ms: 30000 },
          auth: {},
        }))
        return
      }

      if (frame.type !== 'req' || !IMPORT_METHODS.includes(
        frame.method as (typeof IMPORT_METHODS)[number],
      )) {
        if (frame.type === 'req') ws.send(response(frame.id, {}))
        return
      }

      if (frame.method === 'memory.import.info') {
        ws.send(response(frame.id, {
          schemaVersion: 1,
          available: true,
          provider: 'synthetic-provider',
          model: 'synthetic-model',
          isLocal: true,
          maxInputBytes: 262144,
          promptVersion: 'profile-fusion-v3',
          recentImport: null,
          draftJob: null,
        }))
        return
      }

      if (frame.method === 'memory.import.start') {
        capture.previewParams = frame.params
        capture.releasePreview = () => {
          previewReady = true
        }
        ws.send(response(frame.id, jobPayload('analyzing')))
        return
      }

      if (frame.method === 'memory.import.status') {
        ws.send(response(frame.id, jobPayload(previewReady ? 'ready' : 'analyzing')))
        return
      }

      if (frame.method === 'memory.import.apply') {
        capture.applyParams = frame.params
        ws.send(response(frame.id, {
          schemaVersion: 1,
          status: 'applied',
          receiptId: 'receipt-e2e-1',
          batchId: 'batch-e2e-1',
          appliedAt: '2026-07-28T08:00:00Z',
          indexStatus: 'pending',
          recentImport: {
            receiptId: 'receipt-e2e-1',
            batchId: 'batch-e2e-1',
            appliedAt: '2026-07-28T08:00:00Z',
            summary: ['Added one confirmed working-style preference.'],
            provider: 'synthetic-provider',
            model: 'synthetic-model',
            status: 'applied',
            indexStatus: 'pending',
            fileCount: 1,
            targets: ['MEMORY'],
          },
        }))
        return
      }

      ws.send(response(frame.id, { schemaVersion: 1, status: 'discarded' }))
    })
    ws.send(JSON.stringify({
      type: 'event',
      event: 'connect.challenge',
      payload: { nonce: 'memory-import-e2e' },
    }))
  })

  return capture
}

test('imports a profile through analysis, diff confirmation, and a recoverable result', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.addInitScript(() => localStorage.setItem('opensquilla-locale', 'en'))
  const gateway = await installImportGateway(page)

  await page.goto(CONTROL_URL + 'settings/memory')
  const dialog = page.getByRole('dialog', { name: 'Settings' })
  const panel = dialog.getByTestId('settings-memory-panel')
  await expect(panel).toBeVisible({ timeout: 15000 })
  await expect(panel.getByRole('heading', { name: 'Import memory from another AI' })).toBeVisible()

  const steps = panel.locator('.memory-import__steps')
  await expect(steps).toHaveAttribute('aria-label', 'Steps to import memory')
  await expect(steps.locator('.memory-import__step')).toHaveCount(2)
  await expect(panel.getByText('Copy the prompt and send it to your other AI')).toBeVisible()
  await expect(panel.getByText("Paste the AI's complete response")).toBeVisible()

  const prompt = panel.getByTestId('memory-import-export-prompt')
  const promptToggle = panel.getByRole('button', { name: 'Hide prompt' })
  await expect(prompt).toBeVisible()
  await expect(promptToggle).toHaveAttribute('aria-expanded', 'true')
  await promptToggle.click()
  await expect(prompt).not.toBeVisible()
  await expect(panel.getByRole('button', { name: 'View prompt' })).toHaveAttribute(
    'aria-expanded',
    'false',
  )

  const textarea = panel.getByTestId('memory-import-textarea')
  await expect(textarea).toBeVisible()
  await textarea.fill('The user said "Keep answers concise and evidence-backed."')

  // The primary input action remains reachable above mobile browser/keyboard
  // chrome instead of becoming a detached desktop-only footer.
  const inputActions = panel.locator('.memory-import__actions--input')
  await expect(inputActions).toHaveCSS('position', 'sticky')
  const actionBox = await inputActions.boundingBox()
  expect(actionBox).not.toBeNull()
  expect(actionBox!.y + actionBox!.height).toBeLessThanOrEqual(844)

  await panel.getByTestId('memory-import-preview').click()
  await expect(panel.getByRole('heading', { name: 'Preparing a safe preview' })).toBeVisible()
  await expect(panel.locator('.memory-import__analyzing')).toHaveAttribute('aria-live', 'polite')
  await expect(panel.getByText('Wait for synthetic-model')).toBeVisible()
  await expect.poll(() => gateway.previewParams).toBeTruthy()

  expect(gateway.previewParams).toMatchObject({
    schemaVersion: 1,
    agentId: 'main',
    rawText: 'The user said "Keep answers concise and evidence-backed."',
    uiLocale: 'en',
    exportPromptVersion: 'profile-export-v1',
    expectedProvider: 'synthetic-provider',
    expectedModel: 'synthetic-model',
    expectedIsLocal: true,
  })
  expect(gateway.previewParams?.clientRequestId).toEqual(expect.any(String))

  gateway.releasePreview?.()
  const previewHeading = panel.getByRole('heading', { name: 'Review every file change' })
  await expect(previewHeading).toBeVisible()
  await expect(previewHeading).toBeFocused()
  await expect(panel.getByText('Long-term preferences', { exact: true })).toBeVisible()

  const fileSummary = panel.locator('.memory-import__file summary')
  const diff = panel.locator('.memory-import__diff')
  await fileSummary.click()
  await expect(panel.getByText('Added', { exact: true })).toBeVisible()
  await expect(panel.getByText('Removed', { exact: true })).toBeVisible()
  await expect(panel.getByText('Keep answers concise and evidence-backed.')).toBeVisible()
  await expect(diff).toHaveAttribute('tabindex', '0')
  await fileSummary.focus()
  await page.keyboard.press('Tab')
  await expect(diff).toBeFocused()

  await panel.getByTestId('memory-import-apply').click()
  await expect(panel.getByRole('heading', { name: 'Profile imported' })).toBeVisible()
  await expect(panel.getByRole('heading', { name: 'Most recent import' })).toBeVisible()
  await expect(panel).toContainText('Updated the following memory categories')
  await expect(panel.getByText('Long-term preferences', { exact: true })).toBeVisible()
  const processingDetails = panel.getByTestId('memory-import-details')
  await expect(processingDetails).not.toHaveAttribute('open', '')
  await expect(panel.getByText('synthetic-provider / synthetic-model')).not.toBeVisible()
  await processingDetails.locator('summary').click()
  await expect(panel.getByText('synthetic-provider / synthetic-model')).toBeVisible()
  await expect(panel).toContainText('Memory search will refresh automatically.')

  expect(gateway.applyParams).toMatchObject({
    schemaVersion: 1,
    agentId: 'main',
    previewId: 'preview-e2e-1',
    candidateHash: 'candidate-hash-e2e',
  })
  expect(gateway.applyParams?.idempotencyKey).toEqual(expect.any(String))
  expect(gateway.applyParams).not.toHaveProperty('candidate')
  expect(gateway.applyParams).not.toHaveProperty('files')
})
