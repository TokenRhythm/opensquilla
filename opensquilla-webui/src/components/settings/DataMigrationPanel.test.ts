// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { App } from 'vue'

const mounted: Array<{ app: App; el: HTMLElement }> = []

function setDesktopApi(api: unknown): void {
  ;(window as unknown as { opensquillaDesktop?: unknown }).opensquillaDesktop = api
}

async function settle(): Promise<void> {
  for (let i = 0; i < 8; i++) await Promise.resolve()
  await new Promise(resolve => setTimeout(resolve, 10))
}

interface MountOptions {
  desktopApi?: Record<string, unknown>
  rpc?: Record<string, unknown>
  confirm?: ReturnType<typeof vi.fn>
}

async function mountPanel(options: MountOptions = {}) {
  vi.resetModules()
  document.body.innerHTML = ''
  setDesktopApi(options.desktopApi)

  const rpc = {
    waitForConnection: vi.fn(async () => {}),
    supportsMethod: vi.fn(() => true),
    call: vi.fn(),
    ...options.rpc,
  }
  vi.doMock('@/stores/rpc', () => ({ useRpcStore: () => rpc }))
  const confirm = options.confirm ?? vi.fn(async () => true)
  vi.doMock('@/composables/useConfirm', () => ({ useConfirm: () => ({ confirm }) }))

  const { createApp, nextTick } = await import('vue')
  const i18n = (await import('@/i18n')).default
  i18n.global.locale.value = 'en'
  const Component = (await import('./DataMigrationPanel.vue')).default
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(Component)
  app.use(i18n)
  app.mount(el)
  mounted.push({ app, el })
  await settle()
  await nextTick()
  return { el, rpc, confirm }
}

beforeEach(() => {
  document.body.innerHTML = ''
  setDesktopApi(undefined)
})

afterEach(() => {
  while (mounted.length) mounted.pop()!.app.unmount()
  setDesktopApi(undefined)
  vi.doUnmock('@/stores/rpc')
  vi.doUnmock('@/composables/useConfirm')
  vi.restoreAllMocks()
})

function desktopCandidate() {
  return {
    kind: 'cli-home',
    path: '/synthetic/migration-source/.opensquilla',
    version: '0.4.0',
    estimated_activity_at: '2026-07-18T12:00:00Z',
    session_count: 26,
    size_bytes: 4096,
    previously_imported: false,
  }
}

function desktopReport() {
  return {
    items: [
      { kind: 'sessions', status: 'planned' },
      {
        kind: 'preflight/target',
        status: 'error',
        reason: 'Target /synthetic/migration-target/current already contains data.',
      },
      {
        kind: 'database',
        status: 'error',
        reason: 'Could not read /synthetic/migration-source/.opensquilla/state.db.',
      },
    ],
    paused_jobs: [],
    preflight: { disk_required_bytes: 1024, disk_free_bytes: 8192 },
    notes: [],
  }
}

function cleanupReport(
  mode: 'reset-current-settings' | 'delete-current-profile' | 'delete-all-user-data',
  outcome: 'ready' | 'blocked' = 'ready',
) {
  return {
    schema_version: 1 as const,
    outcome,
    stable_code: outcome === 'ready' ? 'cleanup_ready' : 'cleanup_history_invalid',
    mode,
    items: [
      {
        kind: 'primary-home',
        path: '/synthetic/user-data/opensquilla',
        exists: true,
        identity: '1:2',
      },
      {
        kind: 'recovery-profiles-container',
        path: '/synthetic/user-data/recovery-profiles',
        exists: false,
        identity: null,
      },
    ],
    transaction_id: 'synthetic-cleanup',
    revision: 42,
    scope_fingerprint: 'a'.repeat(64),
  }
}

function desktopMaintenanceApi(overrides: Record<string, unknown> = {}) {
  return {
    getDesktopProfileKind: async () => 'primary',
    getRecoveryState: async () => ({ inspection: { outcome: 'ready', stable_code: 'ready' } }),
    migrationSummary: vi.fn(async () => ({ ok: true, candidates: [], candidate: null, report: null })),
    migrationRun: vi.fn(),
    ...overrides,
  }
}

describe('DataMigrationPanel desktop provider', () => {
  it('scans on panel mount, keeps paths in technical details, and previews through the Desktop bridge', async () => {
    const candidate = desktopCandidate()
    let progress: ((state: { phase: string; detail?: string }) => void) | undefined
    const migrationSummary = vi.fn(async (payload?: { source?: string }) => payload?.source
      ? { ok: true, candidate, candidates: [candidate], report: desktopReport(), previewId: 'preview-1' }
      : { ok: true, candidate: null, candidates: [candidate], report: null, requiresSelection: true })
    const { el } = await mountPanel({
      desktopApi: {
        getOsLocale: async () => 'en',
        getDesktopProfileKind: async () => 'primary',
        getRecoveryState: async () => ({ inspection: { outcome: 'ready', stable_code: 'ready' } }),
        migrationSummary,
        migrationRun: vi.fn(),
        onMigrationProgress: (callback: typeof progress) => {
          progress = callback
          return () => { progress = undefined }
        },
      },
    })

    expect(migrationSummary).toHaveBeenCalledTimes(1)
    const sourceButton = el.querySelector<HTMLButtonElement>('.migration-candidate')!
    expect(sourceButton.textContent).toContain('26 sessions')
    expect(sourceButton.textContent).not.toContain(candidate.path)

    sourceButton.click()
    await settle()
    expect(migrationSummary).toHaveBeenLastCalledWith({ source: candidate.path })
    const primarySummary = el.querySelector('[data-testid="data-migration-primary-summary"]')
    expect(primarySummary?.textContent).not.toContain('/synthetic/migration-')
    const technical = el.querySelector('.migration-summary__technical')
    expect(technical?.textContent).toContain(candidate.path)
    expect(technical?.textContent).toContain('/synthetic/migration-target/current')
    expect(el.querySelector('[data-testid="data-migration-run"]')).toBeTruthy()

    progress?.({ phase: 'validating', detail: 'Reading /synthetic/migration-progress/source.db' })
    await settle()
    const phase = el.querySelector('.migration-summary__phase')
    expect(phase?.textContent).toContain('validating')
    expect(phase?.textContent).not.toContain('/synthetic/migration-progress')
  })

  it('shows known legacy Agent compatibility state only in this settings panel', async () => {
    const chooseLegacyAgentDataLocation = vi.fn(async () => ({
      ok: false,
      error: 'Could not select /synthetic/legacy-agent-data.',
    }))
    const { el } = await mountPanel({
      desktopApi: {
        getOsLocale: async () => 'en',
        getDesktopProfileKind: async () => 'primary',
        getRecoveryState: async () => ({
          inspection: { outcome: 'attention', stable_code: 'workspace_conflict' },
        }),
        chooseLegacyAgentDataLocation,
        migrationSummary: vi.fn(async () => ({ ok: true, candidates: [], candidate: null, report: null })),
        migrationRun: vi.fn(),
      },
    })

    const compatibility = el.querySelector('[data-testid="data-migration-compatibility"]')
    expect(compatibility?.textContent).toContain('Legacy Agent data location')
    compatibility?.querySelector<HTMLButtonElement>('button')!.click()
    await settle()
    expect(chooseLegacyAgentDataLocation).toHaveBeenCalledWith({})
    expect(el.querySelector('[data-testid="data-migration-error"]')?.textContent)
      .toContain('could not inspect import sources')
    expect(el.querySelector('[data-testid="data-migration-error"]')?.textContent)
      .not.toContain('/synthetic/legacy-agent-data')
  })

  it('confirms an empty-target copy and sends only the opaque preview approval', async () => {
    const candidate = desktopCandidate()
    const migrationRun = vi.fn(async () => ({ ok: true, migrationApplied: true, restartOk: true }))
    const migrationSummary = vi.fn(async (payload?: { source?: string }) => payload?.source
      ? { ok: true, candidate, candidates: [candidate], report: {
        items: [{ kind: 'sessions', status: 'planned' }],
        paused_jobs: [],
        preflight: {},
      }, previewId: 'empty-preview' }
      : { ok: true, candidate: null, candidates: [candidate], report: null })
    const confirm = vi.fn(async () => true)
    const { el } = await mountPanel({
      confirm,
      desktopApi: {
        getOsLocale: async () => 'en',
        getDesktopProfileKind: async () => 'primary',
        migrationSummary,
        migrationRun,
      },
    })
    el.querySelector<HTMLButtonElement>('.migration-candidate')!.click()
    await settle()
    el.querySelector<HTMLButtonElement>('[data-testid="data-migration-run"]')!.click()
    await settle()

    expect(confirm).toHaveBeenCalledTimes(1)
    expect(migrationRun).toHaveBeenCalledWith({ overwrite: false, previewId: 'empty-preview' })
  })

  it('requires overwrite acknowledgement before handing replacement to native confirmation', async () => {
    const candidate = desktopCandidate()
    const migrationRun = vi.fn(async () => ({ ok: true, migrationApplied: true, restartOk: true }))
    const migrationSummary = vi.fn(async (payload?: { source?: string }) => payload?.source
      ? { ok: true, candidate, candidates: [candidate], report: {
        items: [{
          kind: 'preflight/target',
          status: 'error',
          reason: 'Current data already exists.',
        }],
        paused_jobs: [],
        preflight: {},
      }, previewId: 'replace-preview' }
      : { ok: true, candidate: null, candidates: [candidate], report: null })
    const confirm = vi.fn(async () => true)
    const { el } = await mountPanel({
      confirm,
      desktopApi: {
        getOsLocale: async () => 'en',
        getDesktopProfileKind: async () => 'primary',
        migrationSummary,
        migrationRun,
      },
    })
    el.querySelector<HTMLButtonElement>('.migration-candidate')!.click()
    await settle()
    const run = el.querySelector<HTMLButtonElement>('[data-testid="data-migration-run"]')!
    expect(run.disabled).toBe(true)
    const checkbox = el.querySelector<HTMLInputElement>('.migration-summary__replacement input')!
    checkbox.checked = true
    checkbox.dispatchEvent(new Event('change', { bubbles: true }))
    await settle()
    expect(run.disabled).toBe(false)
    run.click()
    await settle()

    expect(confirm).not.toHaveBeenCalled()
    expect(migrationRun).toHaveBeenCalledWith({ overwrite: true, previewId: 'replace-preview' })
  })

  it('reads and dismisses a terminal result only when this panel mounts', async () => {
    const migrationPeekLastResult = vi.fn(async () => ({
      ok: true,
      migrationApplied: true,
      restartOk: true,
      source: '/synthetic/migration-source/old-profile',
      sourceKind: 'cli-home',
    }))
    const migrationDismissLastResult = vi.fn(async () => ({ ok: true }))
    const { el } = await mountPanel({
      desktopApi: {
        getOsLocale: async () => 'en',
        getDesktopProfileKind: async () => 'primary',
        migrationSummary: vi.fn(async () => ({ ok: true, candidates: [], candidate: null, report: null })),
        migrationRun: vi.fn(),
        migrationPeekLastResult,
        migrationDismissLastResult,
      },
    })

    expect(migrationPeekLastResult).toHaveBeenCalledTimes(1)
    const result = el.querySelector<HTMLElement>('.data-migration__result')!
    expect(result.textContent).toContain('Data transfer complete')
    expect(result.querySelector('details')?.textContent).toContain('/synthetic/migration-source/old-profile')
    const dismiss = Array.from(result.querySelectorAll<HTMLButtonElement>('button'))
      .find(button => button.textContent?.includes('Dismiss'))!
    dismiss.click()
    await settle()
    expect(migrationDismissLastResult).toHaveBeenCalledTimes(1)
    expect(el.querySelector('.data-migration__result')).toBeNull()
  })

  it('keeps a migration restart failure visible when the desktop retry is rejected', async () => {
    const migrationDismissLastResult = vi.fn(async () => ({ ok: true }))
    const retryStartup = vi.fn(async () => ({
      ok: false,
      error: 'The previous gateway is still shutting down.',
    }))
    const { el } = await mountPanel({
      desktopApi: {
        getOsLocale: async () => 'en',
        getDesktopProfileKind: async () => 'primary',
        retryStartup,
        migrationPeekLastResult: async () => ({
          ok: false,
          migrationApplied: true,
          restartOk: false,
          failureCode: 'gateway_restart_failed',
          failureStage: 'restart',
        }),
        migrationDismissLastResult,
        migrationSummary: vi.fn(async () => ({ ok: true, candidates: [], candidate: null, report: null })),
        migrationRun: vi.fn(),
      },
    })

    el.querySelector<HTMLButtonElement>('[data-testid="data-migration-restart"]')!.click()
    await settle()

    expect(retryStartup).toHaveBeenCalledTimes(1)
    expect(migrationDismissLastResult).not.toHaveBeenCalled()
    expect(el.querySelector('.data-migration__result')).toBeTruthy()
    expect(el.querySelector('[data-testid="data-migration-error"]')?.textContent)
      .toContain('Restart failed: The previous gateway is still shutting down.')
  })

  it('keeps a migration restart failure visible when refreshed status cannot be read', async () => {
    const migrationDismissLastResult = vi.fn(async () => ({ ok: true }))
    const retryStartup = vi.fn(async () => ({ ok: true }))
    const getGatewayStatus = vi.fn(async () => {
      throw new Error('status unavailable')
    })
    const { el } = await mountPanel({
      desktopApi: {
        getOsLocale: async () => 'en',
        getDesktopProfileKind: async () => 'primary',
        getGatewayStatus,
        retryStartup,
        migrationPeekLastResult: async () => ({
          ok: false,
          migrationApplied: true,
          restartOk: false,
          failureCode: 'gateway_restart_failed',
          failureStage: 'restart',
        }),
        migrationDismissLastResult,
        migrationSummary: vi.fn(async () => ({ ok: true, candidates: [], candidate: null, report: null })),
        migrationRun: vi.fn(),
      },
    })

    el.querySelector<HTMLButtonElement>('[data-testid="data-migration-restart"]')!.click()
    await settle()

    expect(retryStartup).toHaveBeenCalledTimes(1)
    expect(getGatewayStatus).toHaveBeenCalledTimes(1)
    expect(migrationDismissLastResult).not.toHaveBeenCalled()
    expect(el.querySelector('.data-migration__result')).toBeTruthy()
    expect(el.querySelector('[data-testid="data-migration-error"]')?.textContent)
      .toContain('Restart failed: status unavailable')
  })
})

describe('DataMigrationPanel desktop cleanup', () => {
  it('hides cleanup controls when an older shell has no safe cleanup bridge', async () => {
    const { el } = await mountPanel({ desktopApi: desktopMaintenanceApi() })
    expect(el.querySelector('[data-testid="data-migration-cleanup-all"]')).toBeNull()
  })

  it('lists every inspected location and sends only an opaque preview approval', async () => {
    const inspectDesktopCleanup = vi.fn(async () => ({
      ok: true,
      previewId: 'opaque-preview',
      report: cleanupReport('delete-all-user-data'),
      profile: { kind: 'primary' as const, recoveryId: null },
    }))
    const applyDesktopCleanup = vi.fn(async () => ({ ok: true, scheduled: true }))
    const { el } = await mountPanel({
      desktopApi: desktopMaintenanceApi({
        inspectDesktopCleanup,
        discardDesktopCleanup: vi.fn(async () => true),
        applyDesktopCleanup,
        revealDesktopUserData: vi.fn(async () => true),
      }),
    })

    el.querySelector<HTMLButtonElement>('[data-testid="data-migration-cleanup-all"]')!.click()
    await settle()

    expect(inspectDesktopCleanup).toHaveBeenCalledWith({ mode: 'delete-all-user-data' })
    const summary = el.querySelector('[data-testid="data-migration-cleanup-summary"]')
    expect(summary?.textContent).toContain('/synthetic/user-data/opensquilla')
    expect(summary?.textContent).toContain('/synthetic/user-data/recovery-profiles')
    expect(summary?.textContent).toContain('1 of 2 listed locations currently exist')
    expect(document.activeElement?.id).toBe('cleanup-summary-title')

    const apply = el.querySelector<HTMLButtonElement>('[data-testid="data-migration-cleanup-apply"]')
    expect(apply?.disabled).toBe(true)
    const checkbox = summary?.querySelector<HTMLInputElement>('input[type="checkbox"]')
    checkbox!.checked = true
    checkbox!.dispatchEvent(new Event('change', { bubbles: true }))
    const phrase = summary?.querySelector<HTMLInputElement>('input[type="text"]')
    phrase!.value = 'DELETE ALL OPENSQUILLA DATA'
    phrase!.dispatchEvent(new Event('input', { bubbles: true }))
    await settle()
    expect(apply?.disabled).toBe(false)

    apply!.click()
    await settle()
    expect(applyDesktopCleanup).toHaveBeenCalledWith({
      previewId: 'opaque-preview',
      acknowledged: true,
      confirmation: 'DELETE ALL OPENSQUILLA DATA',
    })
    const payload = (applyDesktopCleanup.mock.calls as unknown[][])[0]?.[0] as Record<string, unknown>
    expect(payload).not.toHaveProperty('mode')
    expect(payload).not.toHaveProperty('path')
    expect(payload).not.toHaveProperty('transaction_id')
    expect(payload).not.toHaveProperty('revision')
  })

  it('shows blocked inspection recovery information without an apply button', async () => {
    const report = cleanupReport('delete-current-profile', 'blocked')
    const { el } = await mountPanel({
      desktopApi: desktopMaintenanceApi({
        inspectDesktopCleanup: vi.fn(async () => ({
          ok: false,
          previewId: null,
          report,
          profile: { kind: 'recovery' as const, recoveryId: '01234567-89ab-4cde-8fab-0123456789ab' },
        })),
        discardDesktopCleanup: vi.fn(async () => true),
        applyDesktopCleanup: vi.fn(),
        revealDesktopUserData: vi.fn(async () => true),
      }),
    })

    el.querySelector<HTMLButtonElement>('[data-testid="data-migration-cleanup-profile"]')!.click()
    await settle()

    const summary = el.querySelector('[data-testid="data-migration-cleanup-summary"]')
    expect(summary?.getAttribute('aria-labelledby')).toBe('cleanup-summary-title')
    expect(summary?.textContent).toContain('cleanup_history_invalid')
    expect(summary?.textContent).toContain('Recovery profile')
    expect(el.querySelector('[data-testid="data-migration-cleanup-apply"]')).toBeNull()
    expect(summary?.querySelector('input[type="checkbox"]')).toBeNull()
    expect(summary?.querySelector('input[type="text"]')).toBeNull()
  })

  it('discards the main-process preview and returns focus to the trigger on cancel', async () => {
    const discardDesktopCleanup = vi.fn(async () => true)
    const { el } = await mountPanel({
      desktopApi: desktopMaintenanceApi({
        inspectDesktopCleanup: vi.fn(async () => ({
          ok: true,
          previewId: 'cancel-preview',
          report: cleanupReport('delete-current-profile'),
          profile: { kind: 'primary' as const, recoveryId: null },
        })),
        discardDesktopCleanup,
        applyDesktopCleanup: vi.fn(),
        revealDesktopUserData: vi.fn(async () => true),
      }),
    })

    const trigger = el.querySelector<HTMLButtonElement>('[data-testid="data-migration-cleanup-profile"]')!
    trigger.click()
    await settle()
    el.querySelector<HTMLButtonElement>('[data-testid="data-migration-cleanup-cancel"]')!.click()
    await settle()

    expect(discardDesktopCleanup).toHaveBeenCalledWith({ previewId: 'cancel-preview' })
    expect(el.querySelector('[data-testid="data-migration-cleanup-summary"]')).toBeNull()
    expect(document.activeElement).toBe(trigger)
  })

  it('re-presents a changed inventory with a new one-shot preview', async () => {
    const initial = cleanupReport('delete-current-profile')
    const changed = {
      ...cleanupReport('delete-current-profile'),
      items: [{
        kind: 'new-profile-log',
        path: '/synthetic/user-data/new-log',
        exists: true,
        identity: '9:9',
      }],
      revision: 43,
      scope_fingerprint: 'b'.repeat(64),
    }
    const applyDesktopCleanup = vi.fn(async () => ({
      ok: false,
      previewId: 'replacement-preview',
      report: changed,
      profile: { kind: 'primary' as const, recoveryId: null },
      detail: 'The cleanup locations changed while the local runtime stopped. Review them again.',
    }))
    const { el } = await mountPanel({
      desktopApi: desktopMaintenanceApi({
        inspectDesktopCleanup: vi.fn(async () => ({
          ok: true,
          previewId: 'initial-preview',
          report: initial,
          profile: { kind: 'primary' as const, recoveryId: null },
        })),
        discardDesktopCleanup: vi.fn(async () => true),
        applyDesktopCleanup,
        revealDesktopUserData: vi.fn(async () => true),
      }),
    })

    el.querySelector<HTMLButtonElement>('[data-testid="data-migration-cleanup-profile"]')!.click()
    await settle()
    const checkbox = el.querySelector<HTMLInputElement>(
      '[data-testid="data-migration-cleanup-summary"] input[type="checkbox"]',
    )!
    checkbox.checked = true
    checkbox.dispatchEvent(new Event('change', { bubbles: true }))
    await settle()
    el.querySelector<HTMLButtonElement>('[data-testid="data-migration-cleanup-apply"]')!.click()
    await settle()

    const summary = el.querySelector('[data-testid="data-migration-cleanup-summary"]')
    expect(summary?.textContent).toContain('/synthetic/user-data/new-log')
    expect(summary?.textContent).not.toContain('/synthetic/user-data/opensquilla')
    expect(summary?.querySelector<HTMLInputElement>('input[type="checkbox"]')?.checked).toBe(false)
    expect(el.querySelector<HTMLButtonElement>('[data-testid="data-migration-cleanup-apply"]')?.disabled).toBe(true)
    expect(document.activeElement?.id).toBe('cleanup-summary-title')
  })
})

describe('DataMigrationPanel gateway preview provider', () => {
  it('uses read-only RPC, renders the safe summary, and never exposes a server path', async () => {
    const call = vi.fn(async (method: string) => {
      if (method === 'migration.sources.list') {
        return {
          schemaVersion: 1,
          mode: 'preview_only',
          capabilities: { discover: true, preview: true, apply: false, manualSource: false },
          candidates: [{
            candidateId: 'opaque-candidate',
            sourceKind: 'cli-home',
            version: '/srv/private/version-leak',
            sessionCount: 12,
            sizeBytes: 2048,
            path: '/srv/private/legacy',
          }],
        }
      }
      if (method === 'migration.sources.preview') {
        return {
          schemaVersion: 1,
          mode: 'preview_only',
          previewStatus: 'available',
          targetAction: 'copy',
          summary: {
            sessionCount: 12,
            itemCounts: { planned: 7, skipped: 1, error: 0 },
            pausedJobCount: 2,
            diskRequiredBytes: 1024,
            diskFreeBytes: 8192,
          },
          blockers: [],
          notices: ['scheduled_jobs_will_be_paused'],
          sourcePath: '/srv/private/legacy',
        }
      }
      throw new Error(`Unexpected method ${method}`)
    })
    const { el } = await mountPanel({ rpc: { call } })

    expect(call).toHaveBeenCalledWith('migration.sources.list', {})
    expect(el.textContent).not.toContain('/srv/private/legacy')
    expect(el.textContent).not.toContain('/srv/private/version-leak')
    expect(el.querySelector('[data-testid="data-migration-run"]')).toBeNull()

    el.querySelector<HTMLButtonElement>('.migration-candidate')!.click()
    await settle()
    expect(call).toHaveBeenCalledWith('migration.sources.preview', { candidateId: 'opaque-candidate' })
    expect(el.textContent).toContain('7 planned')
    expect(el.textContent).toContain('Scheduled jobs (2, kept paused)')
    expect(el.textContent).not.toContain('/srv/private/legacy')
    expect(el.querySelector('[data-testid="data-migration-run"]')).toBeNull()
  })

  it('shows an upgrade state without calling an older gateway', async () => {
    const call = vi.fn()
    const { el } = await mountPanel({
      rpc: {
        supportsMethod: vi.fn(() => false),
        call,
      },
    })
    expect(call).not.toHaveBeenCalled()
    expect(el.querySelector('[data-testid="data-migration-unsupported"]')).toBeTruthy()
  })
})
