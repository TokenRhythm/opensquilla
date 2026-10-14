import { computed, shallowRef, watch, type Ref } from 'vue'
import { normalizeVisualizeContext } from './context'
import type { VisualizeContext, VisualizeReceipt } from './types'
import type { SlashCommandClassification } from '@/composables/chat/useChatSlashCommands'

/** Use the command registry, preserving the sender's literal/unknown-slash rules. */
export async function acceptsVisualizeContext(
  text: string,
  classifySlashCommand: (text: string) => Promise<SlashCommandClassification>,
): Promise<boolean> {
  const value = text.trim()
  if (value.startsWith('!')) return false
  if (!value.startsWith('/') || value.startsWith('//')) return true
  return await classifySlashCommand(value) === 'unknown'
}

export interface VisualizeDraftSnapshot {
  readonly id: number
  readonly sessionKey: string
  readonly documentId: string
  readonly revisionId: string
  readonly name: string
  readonly summary: string
  readonly data?: VisualizeContext['data']
}

/** An explicit attachment, frozen when the user clicks the host action. */
export function useVisualizeDraft(options: {
  sessionKey: Readonly<Ref<string>>
  deliveryIdentity: Readonly<Ref<string | null>>
  generation: Readonly<Ref<number>>
  available(key: string): boolean
}) {
  const attached = shallowRef<VisualizeDraftSnapshot | null>(null)
  let sequence = 0
  const clear = () => { attached.value = null }
  watch([options.sessionKey, options.deliveryIdentity, options.generation], clear, { flush: 'sync' })

  function attach(key: string, receipt: VisualizeReceipt, input: VisualizeContext) {
    if (key !== options.sessionKey.value || !options.available(key)) return null
    let context: VisualizeContext | null
    try { context = normalizeVisualizeContext(input) } catch { return null }
    if (!context) return null
    const snapshot = Object.freeze({
      id: ++sequence, sessionKey: key, documentId: receipt.documentId,
      revisionId: receipt.revisionId, name: receipt.name,
      summary: context.summary, ...(context.data === undefined ? {} : { data: context.data }),
    })
    attached.value = snapshot
    return context
  }

  function capture() {
    const snapshot = attached.value
    if (!snapshot || snapshot.sessionKey !== options.sessionKey.value || !options.available(snapshot.sessionKey)) return null
    return {
      snapshot,
      isCurrent: () => attached.value === snapshot && options.available(snapshot.sessionKey),
      accepted: () => { if (attached.value === snapshot) clear() },
      message: (text: string, label = 'Canvas conditions') => [
        text.trim(), `${label} · ${snapshot.name}\n${snapshot.summary}`,
        ...(snapshot.data === undefined ? [] : [JSON.stringify(snapshot.data)]),
      ].join('\n\n'),
    }
  }
  return { snapshot: computed(() => attached.value), attach, capture, clear }
}
