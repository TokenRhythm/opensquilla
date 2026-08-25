// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { App } from 'vue'

const mounted: Array<{ app: App; el: HTMLElement }> = []

async function settle(): Promise<void> {
  for (let i = 0; i < 8; i++) await Promise.resolve()
  await new Promise(resolve => setTimeout(resolve, 10))
}

async function waitForText(el: HTMLElement, text: string): Promise<void> {
  await vi.waitFor(() => {
    expect(el.textContent).toContain(text)
  }, { timeout: 1000, interval: 10 })
}

function importInfo(overrides: Record<string, unknown> = {}) {
  return {
    schemaVersion: 1,
    available: true,
    provider: 'synthetic-provider',
    model: 'synthetic-model',
    isLoopback: false,
    maxRawBytes: 262144,
    promptVersion: 'profile-fusion-v1',
    recentImport: null,
    ...overrides,
  }
}

function importPreview(overrides: Record<string, unknown> = {}) {
  return {
    schemaVersion: 1,
    previewId: 'preview-1',
    batchId: 'batch-1',
    candidateHash: 'candidate-hash',
    provider: 'synthetic-provider',
    model: 'synthetic-model',
    summary: ['Added one confirmed preference.'],
    decisionCounts: { applied: 1, duplicate: 2, unresolved: 1 },
    files: [{
      target: 'MEMORY',
      displayName: 'MEMORY.md',
      relativePath: 'MEMORY.md',
      status: 'modified',
      additions: 1,
      deletions: 1,
      diff: '--- a/MEMORY.md\n+++ b/MEMORY.md\n@@ -1 +1 @@\n-old preference\n+new preference',
    }],
    ...overrides,
  }
}

function importJob(overrides: Record<string, unknown> = {}) {
  return {
    schemaVersion: 1,
    jobId: 'job-1',
    batchId: 'batch-1',
    status: 'ready',
    stage: 'diff',
    provider: 'synthetic-provider',
    model: 'synthetic-model',
    startedAt: '2026-07-28T08:00:00Z',
    canRetry: false,
    preview: importPreview(),
    ...overrides,
  }
}

async function mountPanel(options: {
  methods?: string[]
  call?: ReturnType<typeof vi.fn>
} = {}) {
  vi.resetModules()
  document.body.innerHTML = ''
  const methods = options.methods ?? [
    'memory.import.info',
    'memory.import.start',
    'memory.import.status',
    'memory.import.cancel',
    'memory.import.retry',
    'memory.import.apply',
    'memory.import.undo',
    'memory.import.discard',
  ]
  const call = options.call ?? vi.fn(async (method: string) => {
    if (method === 'memory.import.info') return importInfo()
    return {}
  })
  const rpc = {
    waitForConnection: vi.fn(async () => {}),
    supportsMethod: vi.fn((method: string) => methods.includes(method)),
    markMethodUnavailable: vi.fn(),
    call,
  }
  vi.doMock('@/stores/rpc', () => ({ useRpcStore: () => rpc }))

  const { createApp, nextTick } = await import('vue')
  const i18n = (await import('@/i18n')).default
  i18n.global.locale.value = 'en'
  const Component = (await import('./SettingsMemoryPanel.vue')).default
  const el = document.createElement('div')
  // State transitions are covered by browser E2E. Keep component tests
  // deterministic even when another suite has loaded the global 200 ms
  // duration tokens into happy-dom.
  el.style.setProperty('--dur-base', '0ms')
  el.style.setProperty('--dur-fast', '0ms')
  document.body.appendChild(el)
  const app = createApp(Component)
  app.use(i18n)
  app.mount(el)
  mounted.push({ app, el })
  await settle()
  await nextTick()
  return { el, rpc, call }
}

afterEach(() => {
  while (mounted.length) mounted.pop()!.app.unmount()
  vi.doUnmock('@/stores/rpc')
  vi.restoreAllMocks()
  document.body.innerHTML = ''
})

describe('SettingsMemoryPanel', () => {
  it('feature-gates old gateways without calling an unavailable method', async () => {
    const { el, call } = await mountPanel({ methods: [] })

    expect(el.textContent).toContain('Update OpenSquilla to import a profile')
    expect(call).not.toHaveBeenCalled()
  })

  it('explains the two-step import path and keeps the localized prompt copyable', async () => {
    const writeText = vi.fn(async (_text: string) => {})
    vi.stubGlobal('navigator', {
      ...navigator,
      clipboard: { writeText },
      languages: ['en'],
      language: 'en',
    })
    const { el } = await mountPanel()
    const steps = el.querySelector<HTMLOListElement>('.memory-import__steps')!
    const stepHeadings = Array.from(
      steps.querySelectorAll<HTMLElement>('.memory-import__step-head h4'),
    ).map(heading => heading.textContent?.trim())
    const prompt = el.querySelector<HTMLElement>('[data-testid="memory-import-export-prompt"]')!

    expect(steps.getAttribute('aria-label')).toBe('Steps to import memory')
    expect(steps.querySelectorAll('.memory-import__step')).toHaveLength(2)
    expect(stepHeadings).toEqual([
      'Copy the prompt and send it to your other AI',
      "Paste the AI's complete response",
    ])
    expect(el.textContent).toContain(
      'OpenSquilla will prepare a reviewable preview before changing memory.',
    )
    expect(prompt.style.display).not.toBe('none')
    expect(prompt.textContent).toContain('Create a portable profile of the user')
    const promptToggle = el.querySelector<HTMLButtonElement>(
      '[aria-controls="memory-import-export-prompt"]',
    )!
    expect(promptToggle.getAttribute('aria-expanded')).toBe('true')
    promptToggle.click()
    await settle()
    expect(promptToggle.getAttribute('aria-expanded')).toBe('false')
    expect(prompt.style.display).toBe('none')

    const copy = el.querySelector<HTMLButtonElement>('[data-testid="memory-import-copy-prompt"]')!
    expect(copy).toBeTruthy()
    copy.click()
    await settle()

    expect(writeText).toHaveBeenCalledTimes(1)
    expect(writeText.mock.calls[0][0]).toContain('Imported from: <AI assistant name>')
    expect(copy.textContent).toContain('Copied')
    expect(el.textContent).toContain(
      'To generate a preview, OpenSquilla sends the pasted text, current profile, and prior import records',
    )
    expect(el.textContent).toContain('Chat history is excluded')
    expect(el.textContent).not.toContain('for at most two calls')
  })

  it('previews with the advertised single model, renders a real diff, and applies by opaque ids', async () => {
    const call = vi.fn(async (method: string, _params?: unknown) => {
      if (method === 'memory.import.info') return importInfo()
      if (method === 'memory.import.start') return importJob()
      if (method === 'memory.import.apply') {
        return {
          schemaVersion: 1,
          status: 'applied',
          receiptId: 'receipt-1',
          batchId: 'batch-1',
          indexStatus: 'pending',
          appliedAt: '2026-07-28T08:00:00Z',
          recentImport: {
            receiptId: 'receipt-1',
            batchId: 'batch-1',
            status: 'applied',
            provider: 'synthetic-provider',
            model: 'synthetic-model',
            summary: ['Claimed projects were imported, but no project file was written.'],
            appliedAt: '2026-07-28T08:00:00Z',
            fileCount: 1,
            targets: ['MEMORY'],
          },
        }
      }
      return { schemaVersion: 1, status: 'discarded' }
    })
    const { el } = await mountPanel({ call })
    const textarea = el.querySelector<HTMLTextAreaElement>('[data-testid="memory-import-textarea"]')!
    textarea.value = 'The user said "Keep answers concise."'
    textarea.dispatchEvent(new Event('input', { bubbles: true }))
    el.querySelector<HTMLButtonElement>('[data-testid="memory-import-preview"]')!.click()
    await waitForText(el, 'Review every file change')

    expect(call).toHaveBeenCalledWith('memory.import.start', expect.objectContaining({
      schemaVersion: 1,
      agentId: 'main',
      rawText: 'The user said "Keep answers concise."',
      uiLocale: 'en',
      exportPromptVersion: 'profile-export-v1',
      expectedProvider: 'synthetic-provider',
      expectedModel: 'synthetic-model',
      expectedIsLocal: false,
      clientRequestId: expect.any(String),
    }))
    expect(el.textContent).toContain('Review every file change')
    expect(el.textContent).toContain('Model analysis (verify against the file changes)')
    expect(el.textContent).toContain('Long-term preferences')
    expect(el.textContent).not.toContain('Claimed projects were imported')
    expect(el.textContent).not.toContain('Projects and history')
    expect(el.textContent).toContain('Changes in long-term preferences affect future conversations.')
    expect(el.textContent).toContain('old preference')
    expect(el.textContent).toContain('new preference')
    expect(el.textContent).toContain('Added')
    expect(el.textContent).toContain('Removed')
    expect(el.textContent).not.toContain('This preview includes removal-only changes')
    expect(el.querySelector('.memory-import__diff')?.getAttribute('tabindex')).toBe('0')

    el.querySelector<HTMLButtonElement>('[data-testid="memory-import-apply"]')!.click()
    await waitForText(el, 'Profile imported')

    expect(call).toHaveBeenCalledWith('memory.import.apply', expect.objectContaining({
      schemaVersion: 1,
      agentId: 'main',
      previewId: 'preview-1',
      candidateHash: 'candidate-hash',
      idempotencyKey: expect.any(String),
    }))
    expect(el.textContent).toContain('Profile imported')
    expect(el.textContent).toContain('Most recent import')
    expect(el.textContent).toContain('Updated the following memory categories')
    expect(el.textContent).toContain('Long-term preferences')
    const details = el.querySelector<HTMLDetailsElement>('[data-testid="memory-import-details"]')!
    expect(details.open).toBe(false)
    expect(details.querySelector('summary')?.textContent).toContain('View processing details')
  })

  it('warns only when a canonical profile change removes content without replacement', async () => {
    const removal = importPreview({
      files: [{
        target: 'MEMORY',
        displayName: 'MEMORY.md',
        relativePath: 'MEMORY.md',
        status: 'modified',
        additions: 0,
        deletions: 1,
        diff: '--- a/MEMORY.md\n+++ b/MEMORY.md\n@@ -1 +0,0 @@\n-old preference',
      }],
    })
    const call = vi.fn(async (method: string) => {
      if (method === 'memory.import.info') return importInfo()
      if (method === 'memory.import.start') return importJob({ preview: removal })
      return {}
    })
    const { el } = await mountPanel({ call })
    const textarea = el.querySelector<HTMLTextAreaElement>('[data-testid="memory-import-textarea"]')!
    textarea.value = 'Synthetic profile source'
    textarea.dispatchEvent(new Event('input', { bubbles: true }))
    el.querySelector<HTMLButtonElement>('[data-testid="memory-import-preview"]')!.click()
    await waitForText(el, 'This preview includes removal-only changes')

    expect(el.textContent).toContain('no files change until you apply')
  })

  it('shows the dedicated no-change result without offering an apply action', async () => {
    const call = vi.fn(async (
      method: string,
      _params?: Record<string, unknown>,
    ) => {
      if (method === 'memory.import.info') return importInfo()
      if (method === 'memory.import.start') {
        return importJob({
          preview: importPreview({
            summary: [
              'The pasted content appears to be an export prompt, not a returned profile.',
            ],
            decisionCounts: { applied: 0, duplicate: 3, unresolved: 0 },
            files: [],
            noChanges: true,
          }),
        })
      }
      if (method === 'memory.import.apply') {
        return {
          schemaVersion: 1,
          status: 'noChanges',
          receiptId: 'receipt-no-change',
          batchId: 'batch-1',
          indexStatus: 'pending',
          appliedAt: '2026-07-28T08:00:00Z',
        }
      }
      return { schemaVersion: 1, status: 'discarded' }
    })
    const { el } = await mountPanel({ call })
    const textarea = el.querySelector<HTMLTextAreaElement>('[data-testid="memory-import-textarea"]')!
    textarea.value = 'Known profile content'
    textarea.dispatchEvent(new Event('input', { bubbles: true }))
    el.querySelector<HTMLButtonElement>('[data-testid="memory-import-preview"]')!.click()
    await waitForText(el, 'No new information to import')

    expect(el.textContent).toContain('No new information to import')
    expect(el.textContent).toContain(
      'The validated preview contained no file changes, so nothing was written.',
    )
    expect(el.textContent).not.toContain(
      'The pasted content appears to be an export prompt, not a returned profile.',
    )
    expect(el.querySelector('[data-testid="memory-import-apply"]')).toBeNull()
    expect(el.querySelector('[data-testid="memory-import-undo"]')).toBeNull()
    expect(el.querySelector('[role="status"]')?.textContent).toContain(
      'No new information to import',
    )
    expect(document.activeElement?.textContent).toContain('No new information to import')
    expect(call).toHaveBeenCalledWith('memory.import.apply', expect.objectContaining({
      previewId: 'preview-1',
      candidateHash: 'candidate-hash',
      idempotencyKey: expect.any(String),
    }))
  })

  it('turns a stale undo into a new reviewable diff instead of overwriting newer edits', async () => {
    const call = vi.fn(async (method: string) => {
      if (method === 'memory.import.info') {
        return importInfo({
          recentImport: {
            receiptId: 'receipt-1',
            batchId: 'batch-1',
            status: 'applied',
            provider: 'synthetic-provider',
            model: 'synthetic-model',
            summary: ['Previous import'],
            appliedAt: '2026-07-28T08:00:00Z',
            fileCount: 2,
          },
        })
      }
      if (method === 'memory.import.undo') {
        return {
          schemaVersion: 1,
          status: 'reviewRequired',
          receiptId: 'receipt-1',
          preview: importPreview({
            previewId: 'undo-preview',
            candidateHash: 'undo-hash',
            summary: ['Preserved a later local edit.'],
          }),
        }
      }
      return {}
    })
    const { el } = await mountPanel({ call })
    el.querySelector<HTMLButtonElement>('[data-testid="memory-import-undo"]')!.click()
    await waitForText(el, 'The profile changed after this import')

    expect(call).toHaveBeenCalledWith('memory.import.undo', expect.objectContaining({
      schemaVersion: 1,
      agentId: 'main',
      receiptId: 'receipt-1',
      clientRequestId: expect.any(String),
      expectedProvider: 'synthetic-provider',
      expectedModel: 'synthetic-model',
      expectedIsLocal: false,
    }))
    expect(el.textContent).toContain('The profile changed after this import')
    expect(el.textContent).toContain('Preserved a later local edit.')
    expect(el.querySelector('[data-testid="memory-import-apply"]')?.textContent).toContain('Confirm undo')
  })

  it('reuses the same apply idempotency key after a recoverable write failure', async () => {
    let applyAttempts = 0
    const writeFailure = Object.assign(new Error('synthetic write failure'), {
      code: 'MEMORY_IMPORT_WRITE_FAILED',
    })
    const call = vi.fn(async (method: string, _params?: unknown) => {
      if (method === 'memory.import.info') return importInfo()
      if (method === 'memory.import.start') return importJob()
      if (method === 'memory.import.apply') {
        applyAttempts += 1
        if (applyAttempts === 1) throw writeFailure
        return {
          schemaVersion: 1,
          status: 'applied',
          receiptId: 'receipt-1',
          batchId: 'batch-1',
          indexStatus: 'ready',
          appliedAt: '2026-07-28T08:00:00Z',
        }
      }
      return {}
    })
    const { el } = await mountPanel({ call })
    const textarea = el.querySelector<HTMLTextAreaElement>('[data-testid="memory-import-textarea"]')!
    textarea.value = 'The user said "Keep answers concise."'
    textarea.dispatchEvent(new Event('input', { bubbles: true }))
    el.querySelector<HTMLButtonElement>('[data-testid="memory-import-preview"]')!.click()
    await waitForText(el, 'Review every file change')
    el.querySelector<HTMLButtonElement>('[data-testid="memory-import-apply"]')!.click()
    await waitForText(el, 'could not apply the batch safely')

    expect(el.textContent).toContain('could not apply the batch safely')
    const retry = Array.from(el.querySelectorAll('button'))
      .find(button => button.textContent?.includes('Try again'))!
    retry.click()
    await waitForText(el, 'Profile imported')

    const applyCalls = call.mock.calls.filter(([method]) => method === 'memory.import.apply')
    expect(applyCalls).toHaveLength(2)
    expect(applyCalls[0][1]).toEqual(expect.objectContaining({
      idempotencyKey: expect.any(String),
    }))
    expect((applyCalls[1][1] as Record<string, unknown>).idempotencyKey)
      .toBe((applyCalls[0][1] as Record<string, unknown>).idempotencyKey)
    expect(el.textContent).toContain('Profile imported')
  })

  it('reuses the same preview request id after a recoverable model failure', async () => {
    let previewAttempts = 0
    const modelFailure = Object.assign(new Error('synthetic model failure'), {
      code: 'MEMORY_IMPORT_MODEL_FAILED',
    })
    const call = vi.fn(async (
      method: string,
      _params?: Record<string, unknown>,
    ) => {
      if (method === 'memory.import.info') return importInfo()
      if (method === 'memory.import.start') {
        previewAttempts += 1
        if (previewAttempts === 1) throw modelFailure
        return importJob()
      }
      return {}
    })
    const { el } = await mountPanel({ call })
    const textarea = el.querySelector<HTMLTextAreaElement>(
      '[data-testid="memory-import-textarea"]',
    )!
    textarea.value = 'The user said "Keep answers concise."'
    textarea.dispatchEvent(new Event('input', { bubbles: true }))
    el.querySelector<HTMLButtonElement>('[data-testid="memory-import-preview"]')!.click()
    await waitForText(el, 'model call did not complete')

    expect(el.textContent).toContain('model call did not complete')
    const retry = Array.from(el.querySelectorAll('button'))
      .find(button => button.textContent?.includes('Try again'))!
    retry.click()
    await waitForText(el, 'Review every file change')

    const previewCalls = call.mock.calls.filter(([method]) => (
      method === 'memory.import.start'
    ))
    expect(previewCalls).toHaveLength(2)
    expect((previewCalls[1][1] as Record<string, unknown>).clientRequestId)
      .toBe((previewCalls[0][1] as Record<string, unknown>).clientRequestId)
    expect(el.textContent).toContain('Review every file change')
  })

  it('requires confirmation for a zero-file stale undo and then hides repeat undo', async () => {
    const call = vi.fn(async (method: string) => {
      if (method === 'memory.import.info') {
        return importInfo({
          recentImport: {
            receiptId: 'receipt-1',
            batchId: 'batch-1',
            status: 'applied',
            provider: 'synthetic-provider',
            model: 'synthetic-model',
            summary: ['Previous import'],
            appliedAt: '2026-07-28T08:00:00Z',
            fileCount: 1,
          },
        })
      }
      if (method === 'memory.import.undo') {
        return {
          schemaVersion: 1,
          status: 'reviewRequired',
          receiptId: 'receipt-1',
          preview: importPreview({
            previewId: 'undo-preview',
            candidateHash: 'undo-hash',
            summary: ['The later edit already excludes this import.'],
            files: [],
            noChanges: true,
          }),
        }
      }
      if (method === 'memory.import.apply') {
        return {
          schemaVersion: 1,
          status: 'applied',
          receiptId: 'receipt-1',
          batchId: 'batch-1',
          indexStatus: 'pending',
          appliedAt: '2026-07-28T08:00:00Z',
        }
      }
      return {}
    })
    const { el } = await mountPanel({ call })
    el.querySelector<HTMLButtonElement>('[data-testid="memory-import-undo"]')!.click()
    await waitForText(el, 'The profile changed after this import')

    expect(el.textContent).toContain('The profile changed after this import')
    const confirm = el.querySelector<HTMLButtonElement>('[data-testid="memory-import-apply"]')!
    expect(confirm.textContent).toContain('Confirm undo')
    confirm.click()
    await waitForText(el, 'Import undone')

    expect(el.textContent).toContain('Import undone')
    expect(el.querySelector('[data-testid="memory-import-undo"]')).toBeNull()
  })

  it('keeps exact undo available from the recent card when the model is unavailable', async () => {
    const call = vi.fn(async (method: string) => {
      if (method === 'memory.import.info') {
        return importInfo({
          available: false,
          provider: 'previous-provider',
          model: 'previous-model',
          recentImport: {
            receiptId: 'receipt-1',
            batchId: 'batch-1',
            status: 'applied',
            provider: 'previous-provider',
            model: 'previous-model',
            summary: ['Previous import'],
            appliedAt: '2026-07-28T08:00:00Z',
            fileCount: 1,
          },
        })
      }
      if (method === 'memory.import.undo') {
        return {
          schemaVersion: 1,
          status: 'undone',
          receiptId: 'receipt-1',
          indexStatus: 'pending',
        }
      }
      return {}
    })
    const { el } = await mountPanel({ call })

    expect(el.textContent).toContain('Configure the default model and its credentials')
    expect(el.textContent).toContain('Most recent import')
    el.querySelector<HTMLButtonElement>('[data-testid="memory-import-undo"]')!.click()
    await waitForText(el, 'Import undone')

    expect(call).toHaveBeenCalledWith('memory.import.undo', expect.objectContaining({
      receiptId: 'receipt-1',
      expectedProvider: 'previous-provider',
      expectedModel: 'previous-model',
      expectedIsLocal: false,
    }))
    expect(el.textContent).toContain('Import undone')
  })

  it('regenerates a stale undo preview instead of retrying the expired apply', async () => {
    let undoAttempts = 0
    const stale = Object.assign(new Error('synthetic stale undo preview'), {
      code: 'MEMORY_IMPORT_STALE_PREVIEW',
    })
    const call = vi.fn(async (method: string) => {
      if (method === 'memory.import.info') {
        return importInfo({
          recentImport: {
            receiptId: 'receipt-1',
            batchId: 'batch-1',
            status: 'applied',
            provider: 'synthetic-provider',
            model: 'synthetic-model',
            summary: ['Previous import'],
            appliedAt: '2026-07-28T08:00:00Z',
            fileCount: 1,
          },
        })
      }
      if (method === 'memory.import.undo') {
        undoAttempts += 1
        return {
          schemaVersion: 1,
          status: 'reviewRequired',
          receiptId: 'receipt-1',
          preview: importPreview({
            previewId: `undo-preview-${undoAttempts}`,
            candidateHash: `undo-hash-${undoAttempts}`,
            summary: [`Undo review ${undoAttempts}`],
          }),
        }
      }
      if (method === 'memory.import.apply') throw stale
      return { schemaVersion: 1, status: 'discarded' }
    })
    const { el } = await mountPanel({ call })
    el.querySelector<HTMLButtonElement>('[data-testid="memory-import-undo"]')!.click()
    await waitForText(el, 'Undo review 1')
    el.querySelector<HTMLButtonElement>('[data-testid="memory-import-apply"]')!.click()
    await waitForText(el, 'This preview can no longer be applied safely')

    const retry = Array.from(el.querySelectorAll('button'))
      .find(button => button.textContent?.includes('Try again'))!
    retry.click()
    await waitForText(el, 'Undo review 2')

    expect(call.mock.calls.filter(([method]) => method === 'memory.import.undo'))
      .toHaveLength(2)
    expect(call.mock.calls.filter(([method]) => method === 'memory.import.apply'))
      .toHaveLength(1)
    expect(el.textContent).toContain('Undo review 2')
  })

  it('keeps pasted text after a model failure and exposes a localized retry', async () => {
    const failure = Object.assign(new Error('synthetic failure'), {
      code: 'MEMORY_IMPORT_MODEL_FAILED',
    })
    const call = vi.fn(async (method: string) => {
      if (method === 'memory.import.info') return importInfo()
      if (method === 'memory.import.start') throw failure
      return {}
    })
    const { el } = await mountPanel({ call })
    const textarea = el.querySelector<HTMLTextAreaElement>('[data-testid="memory-import-textarea"]')!
    textarea.value = 'Retain this input after failure'
    textarea.dispatchEvent(new Event('input', { bubbles: true }))
    el.querySelector<HTMLButtonElement>('[data-testid="memory-import-preview"]')!.click()
    await waitForText(el, 'The model call did not complete')

    expect(el.textContent).toContain('The model call did not complete')
    expect(el.querySelector<HTMLTextAreaElement>('[data-testid="memory-import-textarea"]')?.value)
      .toBe('Retain this input after failure')
    const retry = Array.from(el.querySelectorAll('button')).find(button => button.textContent?.includes('Try again'))
    expect(retry).toBeTruthy()
  })

  it('restores a slow background job and preserves it when cancelled', async () => {
    const running = importJob({
      status: 'analyzing',
      stage: 'model',
      startedAt: '2020-01-01T00:00:00Z',
      preview: null,
    })
    const call = vi.fn(async (method: string) => {
      if (method === 'memory.import.info') return importInfo({ draftJob: running })
      if (method === 'memory.import.cancel') {
        return importJob({
          status: 'cancelled',
          stage: 'model',
          preview: null,
          canRetry: true,
        })
      }
      if (method === 'memory.import.status') return running
      return {}
    })
    const { el } = await mountPanel({ call })
    await waitForText(el, 'current model is taking longer than usual')

    el.querySelector<HTMLButtonElement>('[data-testid="memory-import-cancel"]')!.click()
    await waitForText(el, 'Preview generation cancelled')

    expect(call).toHaveBeenCalledWith('memory.import.cancel', expect.objectContaining({
      jobId: 'job-1',
    }))
    expect(el.textContent).toContain('Regenerate preview')
    expect(el.textContent).toContain('Discard import')
  })

  it('explains a failed model result and regenerates a preview from the retained source', async () => {
    const failed = importJob({
      status: 'failed',
      stage: 'model',
      preview: null,
      canRetry: true,
      errorCode: 'MEMORY_IMPORT_INVALID_OUTPUT',
    })
    const call = vi.fn(async (method: string) => {
      if (method === 'memory.import.info') return importInfo({ draftJob: failed })
      if (method === 'memory.import.retry') return importJob()
      return {}
    })
    const { el } = await mountPanel({ call })
    await waitForText(el, 'result did not pass validation')

    expect(el.textContent).toContain('The model result did not pass validation')
    expect(el.textContent).toContain('Regenerate preview')
    expect(el.textContent).not.toContain('Continue import')

    el.querySelector<HTMLButtonElement>('[data-testid="memory-import-retry-job"]')!.click()
    await waitForText(el, 'Review every file change')

    expect(call).toHaveBeenCalledWith('memory.import.retry', expect.objectContaining({
      jobId: 'job-1',
    }))
    expect(call.mock.calls.some(([method]) => method === 'memory.import.start')).toBe(false)
  })

  it('falls back to the generic retry explanation when an older gateway omits errorCode', async () => {
    const failed = importJob({
      status: 'failed',
      stage: 'model',
      preview: null,
      canRetry: true,
    })
    const call = vi.fn(async (method: string) => {
      if (method === 'memory.import.info') return importInfo({ draftJob: failed })
      return {}
    })
    const { el } = await mountPanel({ call })
    await waitForText(el, 'This attempt did not produce a validated, reviewable preview')

    expect(el.textContent).toContain('Regenerate preview')
  })

  it('does not render a model summary as a fact in the recent import card', async () => {
    const { el } = await mountPanel({
      call: vi.fn(async (method: string) => method === 'memory.import.info'
        ? importInfo({
            recentImport: {
              receiptId: 'receipt-1',
              batchId: 'batch-1',
              status: 'applied',
              provider: 'synthetic-provider',
              model: 'synthetic-model',
              summary: ['Projects and history were imported.'],
              appliedAt: '2026-07-28T08:00:00Z',
              fileCount: 1,
              targets: ['MEMORY'],
            },
          })
        : {}),
    })

    expect(el.textContent).toContain('Long-term preferences')
    expect(el.textContent).not.toContain('Projects and history were imported.')
  })

  it('uses a generic paused message for a non-model job error', async () => {
    const failed = importJob({
      status: 'failed',
      stage: 'diff',
      preview: null,
      canRetry: true,
      errorCode: 'MEMORY_IMPORT_WRITE_FAILED',
    })
    const { el } = await mountPanel({
      call: vi.fn(async (method: string) => method === 'memory.import.info'
        ? importInfo({ draftJob: failed })
        : {}),
    })

    await waitForText(el, 'did not produce a validated, reviewable preview')
    expect(el.textContent).not.toContain('could not apply the batch safely')
  })

  it('shows a retry request failure without replacing the paused job error', async () => {
    const failed = importJob({
      status: 'failed',
      stage: 'model',
      preview: null,
      canRetry: true,
      errorCode: 'MEMORY_IMPORT_INVALID_OUTPUT',
    })
    const call = vi.fn(async (method: string) => {
      if (method === 'memory.import.info') return importInfo({ draftJob: failed })
      if (method === 'memory.import.retry') throw new Error('synthetic retry transport failure')
      return {}
    })
    const { el } = await mountPanel({ call })

    el.querySelector<HTMLButtonElement>('[data-testid="memory-import-retry-job"]')!.click()
    await waitForText(el, 'The preview could not be regenerated')

    expect(el.querySelector('[data-testid="memory-import-retry-error"]')?.getAttribute('role'))
      .toBe('alert')
    expect(el.textContent).toContain('The model result did not pass validation')
    expect(el.textContent).toContain('no files were changed')
  })

  it('rejects a retry response with an incompatible schema version', async () => {
    const failed = importJob({
      status: 'failed',
      stage: 'model',
      preview: null,
      canRetry: true,
      errorCode: 'MEMORY_IMPORT_INVALID_OUTPUT',
    })
    const call = vi.fn(async (method: string) => {
      if (method === 'memory.import.info') return importInfo({ draftJob: failed })
      if (method === 'memory.import.retry') return importJob({ schemaVersion: 2 })
      return {}
    })
    const { el } = await mountPanel({ call })

    el.querySelector<HTMLButtonElement>('[data-testid="memory-import-retry-job"]')!.click()
    await waitForText(el, 'The preview could not be regenerated')
    expect(el.querySelector('[data-testid="memory-import-retry-error"]')).toBeTruthy()
  })
})
