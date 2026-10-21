import { computed, inject, provide, shallowRef, watch, type InjectionKey } from 'vue'
import { GATEWAY_ACCESS_KEY } from '@/modules/gatewayAccess'
import type { ChatRenderedMessage, ChatToolCall } from '@/types/chat'
import { parseVisualizeReceipt, visualizeReceipts } from './receipts'
import type { VisualizeReceipt } from './types'

/** Published versions known to this conversation, including offscreen rows. */
export interface VisualizeVersions {
  latest(sessionKey: string, receipt: VisualizeReceipt): VisualizeReceipt | undefined
  isHistorical(sessionKey: string, receipt: VisualizeReceipt): boolean
}

/** A live turn keeps its receipt owner even before it becomes a history row. */
export interface VisualizeLivePublications {
  sessionKey: string
  deliveryIdentity: string | null
  receipts: readonly VisualizeReceipt[]
}

export const VISUALIZE_VERSIONS_KEY: InjectionKey<VisualizeVersions> = Symbol('VisualizeVersions')
const STORAGE_PREFIX = 'opensquilla.visualize-versions.v1.'
const MAX_SESSIONS = 12
const MAX_WIDGETS = 64
const MAX_RECORD = 128 * 1024

function rememberVersions(retained: Map<string, VisualizeReceipt>, observed: Iterable<VisualizeReceipt>): boolean {
  let changed = false
  for (const receipt of observed) {
    // Only explicit versions provide a monotonic ordering across partial
    // history pages and reloads. Legacy receipts keep their message ordering.
    if (receipt.version === undefined) continue
    const previous = retained.get(receipt.documentId)
    if (previous?.version !== undefined && previous.version >= receipt.version) continue
    retained.delete(receipt.documentId)
    retained.set(receipt.documentId, receipt)
    changed = true
  }
  while (retained.size > MAX_WIDGETS) retained.delete(retained.keys().next().value!)
  return changed
}

function readRememberedVersions(scope: string): Map<string, VisualizeReceipt> {
  const retained = new Map<string, VisualizeReceipt>()
  try {
    const encoded = sessionStorage.getItem(STORAGE_PREFIX + scope)
    if (!encoded || encoded.length > MAX_RECORD) return retained
    const data: unknown = JSON.parse(encoded)
    if (!Array.isArray(data) || data.length > MAX_WIDGETS) return retained
    for (const candidate of data) {
      if (!candidate || typeof candidate !== 'object' || typeof candidate.callId !== 'string') continue
      // Cached metadata may classify a historical view, but cannot authorize
      // content loading. Revalidate it, and never substitute it for a message's
      // actual tool receipt or install a canvas from this storage record.
      const receipt = parseVisualizeReceipt({
        name: 'show_widget', toolId: candidate.callId, status: 'success',
        isRunning: false, isError: false, result: JSON.stringify(candidate),
      } as ChatToolCall)
      if (receipt) rememberVersions(retained, [receipt])
    }
  } catch { /* Unavailable or invalid browser storage is not a rendering error. */ }
  return retained
}

function saveRememberedVersions(scope: string, retained: ReadonlyMap<string, VisualizeReceipt>): void {
  try {
    const encoded = JSON.stringify([...retained.values()])
    if (encoded.length > MAX_RECORD) return
    const key = STORAGE_PREFIX + scope
    // Refresh insertion order for bounded eviction; widget HTML and interaction
    // state are not part of this index.
    sessionStorage.removeItem(key)
    sessionStorage.setItem(key, encoded)
    const keys = Object.keys(sessionStorage).filter(value => value.startsWith(STORAGE_PREFIX))
    for (const old of keys.filter(value => value !== key).slice(0, Math.max(0, keys.length - MAX_SESSIONS))) sessionStorage.removeItem(old)
  } catch { /* This provider still retains bounded versions while mounted. */ }
}

export function visualizeVersionsFromMessages(messages: readonly ChatRenderedMessage[]): ReadonlyMap<string, VisualizeReceipt> {
  // Reuse the publication validator and monotonic version rule. Inspect both
  // projections before deduplication so a duplicated older legacy tool result
  // cannot overwrite a later edit. Text markers alone do not publish versions.
  const calls = messages.flatMap(message => message.displayRole === 'assistant' ? [
    ...(message.timelineItems ?? []).flatMap(item => item.type === 'tool-group' ? item.group.calls : []),
    ...(message.toolCalls ?? []),
  ] : [])
  return new Map(visualizeReceipts(calls).map(receipt => [receipt.documentId, receipt]))
}

export function provideVisualizeVersions(
  sessionKey: () => string | undefined,
  messages: () => readonly ChatRenderedMessage[],
  livePublications: () => VisualizeLivePublications | undefined = () => undefined,
): VisualizeVersions {
  // This belongs to the message list rather than mounted canvases: virtualized
  // rows must not lose knowledge of an edit when its newest row scrolls away.
  // Remember authoritative version numbers even if history pagination replaces
  // the currently loaded slice. Scope persisted metadata to delivery authority
  // as well as session; an unauthenticated/unknown gateway uses no shared cache.
  const gateway = inject(GATEWAY_ACCESS_KEY, null)
  const remembered = new Map<string, Map<string, VisualizeReceipt>>()
  const publications = computed(() => visualizeVersionsFromMessages(messages()))
  const snapshot = shallowRef<{ sessionKey?: string; documents: ReadonlyMap<string, VisualizeReceipt> }>({ documents: new Map() })
  watch(() => ({
    sessionKey: sessionKey(), identity: gateway?.deliveryIdentity, publications: publications.value, live: livePublications(),
  }), observed => {
    const documents = new Map(observed.publications)
    // A successful edit is authoritative as soon as its tool receipt arrives,
    // before the streaming answer is committed into props.messages. Never
    // relabel an outgoing/cached live turn as belonging to a newly opened task.
    if (observed.identity && observed.live?.deliveryIdentity === observed.identity
      && observed.live.sessionKey === observed.sessionKey) {
      for (const receipt of observed.live.receipts) {
        const previous = documents.get(receipt.documentId)
        if (!previous || (receipt.version === undefined && previous.version === undefined)
          || (receipt.version !== undefined && (previous.version === undefined || receipt.version > previous.version))) {
          documents.set(receipt.documentId, receipt)
        }
      }
    }
    if (observed.sessionKey && observed.identity) {
      const scope = JSON.stringify([observed.identity, observed.sessionKey])
      const retained = remembered.get(scope) ?? readRememberedVersions(scope)
      remembered.delete(scope); remembered.set(scope, retained)
      while (remembered.size > MAX_SESSIONS) remembered.delete(remembered.keys().next().value!)
      if (rememberVersions(retained, documents.values())) saveRememberedVersions(scope, retained)
      for (const [id, receipt] of retained) {
        const current = documents.get(id)
        if (!current || current.version === undefined || receipt.version! >= current.version) documents.set(id, receipt)
      }
    }
    snapshot.value = { sessionKey: observed.sessionKey, documents }
  // Batch paired session/messages prop updates before recording publications;
  // synchronous observation could cache the old rows under the new session.
  }, { immediate: true })
  const latest: VisualizeVersions['latest'] = (session, receipt) => {
    const current = snapshot.value
    return session && session === current.sessionKey ? current.documents.get(receipt.documentId) : undefined
  }
  const versions: VisualizeVersions = {
    latest,
    isHistorical(session, receipt) {
      const current = latest(session, receipt)
      if (!current || current.revisionId === receipt.revisionId) return false
      // A live canvas can receive its new receipt before the conversation
      // projection catches up. An older cached index must not collapse it.
      if (receipt.version !== undefined) return current.version !== undefined && current.version > receipt.version
      return true
    },
  }
  provide(VISUALIZE_VERSIONS_KEY, versions)
  return versions
}
