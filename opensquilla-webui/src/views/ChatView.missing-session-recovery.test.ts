import { describe, expect, it, vi } from 'vitest'
import { ModuleKind, ScriptTarget, transpileModule } from '@typescript/typescript6'
import { ref, watch } from 'vue'
import rawChatViewSource from './ChatView.vue?raw'
import type { DeliveryWalRecord } from '@/utils/chat/pendingInputWal'
import { retiredMissingSessionCandidate } from '@/utils/chat/retiredMissingSession'

const chatViewSource = rawChatViewSource.replace(/\r\n/g, '\n')
const start = chatViewSource.indexOf('let missingSessionDraftRecovery:')
const end = chatViewSource.indexOf('\nwatch([', chatViewSource.indexOf('function recoverMissingRetiredSession()', start))
const factory = new Function('deps', transpileModule(`
  const { deliveryIdentity, sessionKey, route, props, shareMode, forkTransition, historyState,
    pendingInputWal, chatSend, inputText, pendingAttachments, selectedSkills, localPaths,
    pendingWorkspaceId, boundWorkspaceId, cancelPendingAutoSend, startDraftSession, router,
    isDraftRoute, draftProjectHydration, syncDraftProjectFromRoute, persistDraftHistoryState,
    retiredMissingSessionCandidate, draftPersistence, window, ref, watch,
    chatAttachments, attachmentDraftIdentity } = deps
  const agentIdFromSessionKey = () => 'main'
  let chatViewDisposed = false
  let draftRouteCanonicalization = null
  let retainedMissingAttachmentDraft = null
  ${chatViewSource.slice(start, end)}
  return recoverMissingRetiredSession
`, { compilerOptions: { target: ScriptTarget.ES2022, module: ModuleKind.None } }).outputText)

function saved(phase: DeliveryWalRecord['phase'] = 'not-sent'): DeliveryWalRecord {
  return { schemaVersion: 2, revision: 1, ownerRequestId: 'request', deliveryIdentity: 'identity',
    requestSessionKey: 'missing', phase, ...(phase === 'not-sent' ? { retired: true as const } : {}),
    createdAt: 1, updatedAt: 1, request: { kind: 'send', request: { kind: 'new-turn', params: {
      sessionKey: 'missing', message: 'saved text', clientRequestId: 'request',
    } } } }
}

function harness() {
  const sessionKey = ref('missing')
  const historyState = ref({ sessionMissing: true })
  const route = { fullPath: '/chat?session=missing' }
  const listSessionDeliveries = vi.fn(async (): Promise<DeliveryWalRecord[]> => [saved()])
  const deps = {
    deliveryIdentity: ref('identity'), sessionKey, route, props: { embedded: false },
    ref, watch, attachmentDraftIdentity: ref('identity'),
    chatAttachments: { flushAttachmentDraft: vi.fn(async () => {}), completeDraftHandoff: vi.fn(async () => {}) },
    shareMode: ref(false), forkTransition: ref(null), historyState,
    pendingInputWal: { listSessionDeliveries },
    chatSend: { recoverResponseHandoffs: vi.fn(async () => {}), deliveryRecoveryEntries: ref([]) },
    inputText: ref('saved text'), pendingAttachments: ref([]), selectedSkills: ref([]), localPaths: ref([]),
    pendingWorkspaceId: ref(null), boundWorkspaceId: ref(null), cancelPendingAutoSend: vi.fn(),
    startDraftSession: vi.fn(async (_agent: string, options: { isCurrent: () => boolean }) => {
      if (!options.isCurrent()) return undefined
      sessionKey.value = 'fresh'
      historyState.value.sessionMissing = false
      return 'fresh'
    }),
    router: { replace: vi.fn(async () => { route.fullPath = '/chat/new' }), resolve: () => ({ href: '/chat/new' }) },
    window: { location: { pathname: '/chat/new', href: 'http://localhost/chat/new' } },
    draftPersistence: { completeDraftRebind: vi.fn() },
    isDraftRoute: () => route.fullPath === '/chat/new', draftProjectHydration: { begin: vi.fn(() => 1) },
    syncDraftProjectFromRoute: vi.fn(async () => true), persistDraftHistoryState: vi.fn(),
    retiredMissingSessionCandidate,
  }
  return { ...deps, listSessionDeliveries, recover: factory(deps) as () => Promise<void> }
}

describe('missing-session retired draft resumption', () => {
  it('resumes a persisted retired draft after reload, without creating a server request', async () => {
    const h = harness()
    await h.recover()
    expect(h.listSessionDeliveries).toHaveBeenCalledExactlyOnceWith('identity', 'missing')
    expect(h.startDraftSession).toHaveBeenCalledOnce()
    expect(h.startDraftSession.mock.calls[0]![1]).toMatchObject({ preserveComposer: true })
    expect(h.inputText.value).toBe('saved text')
    expect(h.router.replace).toHaveBeenCalledOnce()
    expect(h.sessionKey.value).toBe('fresh')
    expect(h.draftPersistence.completeDraftRebind).toHaveBeenCalledExactlyOnceWith('missing', 'fresh')
    expect(h.chatAttachments.completeDraftHandoff).toHaveBeenCalledExactlyOnceWith('missing', 'fresh')
  })

  it('retains the source draft when reload interrupts the browser history mutation', async () => {
    const h = harness()
    h.window.location.pathname = '/chat'
    h.window.location.href = 'http://localhost/chat?session=missing'
    await h.recover()
    expect(h.router.replace).toHaveBeenCalledOnce()
    expect(h.draftPersistence.completeDraftRebind).not.toHaveBeenCalled()
    expect(h.chatAttachments.completeDraftHandoff).not.toHaveBeenCalled()
  })

  it('waits for attachment restoration before moving a recovered composer', async () => {
    const h = harness()
    let release!: () => void
    h.chatAttachments.flushAttachmentDraft.mockImplementationOnce(() => new Promise(resolve => { release = resolve }))
    const recovery = h.recover()
    await vi.waitFor(() => expect(h.chatAttachments.flushAttachmentDraft).toHaveBeenCalledOnce())
    expect(h.startDraftSession).not.toHaveBeenCalled()
    release()
    await recovery
    expect(h.startDraftSession).toHaveBeenCalledOnce()
  })

  it.each(['navigation', 'identity'] as const)('does not hydrate a successor after %s changes during attachment completion', async change => {
    const h = harness()
    let release!: () => void
    h.chatAttachments.completeDraftHandoff.mockImplementationOnce(() => new Promise(resolve => { release = resolve }))
    const recovery = h.recover()
    await vi.waitFor(() => expect(h.chatAttachments.completeDraftHandoff).toHaveBeenCalledOnce())
    if (change === 'navigation') {
      h.sessionKey.value = 'other'
      h.route.fullPath = '/chat?session=other'
    } else h.deliveryIdentity.value = 'different-account'
    release()
    await recovery
    expect(h.draftProjectHydration.begin).not.toHaveBeenCalled()
    expect(h.syncDraftProjectFromRoute).not.toHaveBeenCalled()
    expect(h.persistDraftHistoryState).not.toHaveBeenCalled()
  })

  it('does not lose a late retirement event behind an older unknown snapshot', async () => {
    const h = harness()
    let release!: (records: DeliveryWalRecord[]) => void
    h.listSessionDeliveries.mockImplementationOnce(() => new Promise(resolve => { release = resolve }))
    const reading = h.recover()
    await vi.waitFor(() => expect(h.listSessionDeliveries).toHaveBeenCalledOnce())
    // Retirement/editor event arrives while the original IDB snapshot is old.
    const joined = h.recover()
    release([saved('unknown')])
    await Promise.all([reading, joined])
    await vi.waitFor(() => expect(h.router.replace).toHaveBeenCalledOnce())
    expect(h.listSessionDeliveries).toHaveBeenCalledTimes(2)
    expect(h.startDraftSession).toHaveBeenCalledOnce()
  })

  it.each(['accepted', 'unknown'] as const)('does not transfer a %s request or start a polling loop', async phase => {
    const h = harness()
    h.listSessionDeliveries.mockResolvedValue([saved(phase)])
    await h.recover()
    expect(h.startDraftSession).not.toHaveBeenCalled()
    expect(h.listSessionDeliveries).toHaveBeenCalledOnce()
    expect(h.sessionKey.value).toBe('missing')
  })

  it('does not resurrect an already consumed empty draft', async () => {
    const h = harness()
    h.inputText.value = ''
    await h.recover()
    expect(h.startDraftSession).not.toHaveBeenCalled()
  })

  it('discards scoped proof if identity changes during the WAL read', async () => {
    const h = harness()
    let release!: (records: DeliveryWalRecord[]) => void
    h.listSessionDeliveries.mockImplementationOnce(() => new Promise(resolve => { release = resolve }))
    const reading = h.recover()
    await vi.waitFor(() => expect(h.listSessionDeliveries).toHaveBeenCalledOnce())
    h.deliveryIdentity.value = 'different-user'
    release([saved()])
    await reading
    expect(h.startDraftSession).not.toHaveBeenCalled()
    expect(h.inputText.value).toBe('saved text')
  })
})
