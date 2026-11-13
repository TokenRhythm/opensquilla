import type { JevConfiguration, JevRouting, JevSnapshot } from '@/modules/jevRouting'
import { JEV_GET_METHOD, type JevSnapshot as GetResult } from '@/contracts/generated/v4/jevGet'
import { validateParams as validateGetParams, validateResult as validateGetResult } from '@/contracts/generated/v4/jevGetValidators.mjs'
import { JEV_CONFIGURE_METHOD, type JevSnapshot as ConfigureResult } from '@/contracts/generated/v4/jevConfigure'
import { validateParams as validateConfigureParams, validateResult as validateConfigureResult } from '@/contracts/generated/v4/jevConfigureValidators.mjs'
import { JEV_RESPOND_METHOD, type JevSnapshot as RespondResult } from '@/contracts/generated/v4/jevRespond'
import { validateParams as validateRespondParams, validateResult as validateRespondResult } from '@/contracts/generated/v4/jevRespondValidators.mjs'
import { JEV_CHANGED_EVENT, type JevSnapshot as ChangedPayload } from '@/contracts/generated/v4/jevChanged'
import { validateJevSnapshot as validateChangedPayload } from '@/contracts/generated/v4/jevChangedValidators.mjs'
import { JEV_PREVIEW_METHOD, type JevSnapshot as PreviewResult } from '@/contracts/generated/v4/jevPreview'
import { validateParams as validatePreviewParams, validateResult as validatePreviewResult } from '@/contracts/generated/v4/jevPreviewValidators.mjs'
import type { TransportCallOptions, TransportEventHandler } from './transportTypes'

interface JevRpcTransport {
  request<T = unknown>(method: string, params?: Record<string, unknown>, options?: TransportCallOptions): Promise<T>
  supports?(method: string): boolean
}
interface JevEventTransport {
  subscribe(event: string, handler: TransportEventHandler): { close(): void }
}

function matchesSession(snapshot: JevSnapshot, sessionKey: string) {
  return snapshot.sessionKey === sessionKey
    && [snapshot.recommendation, ...snapshot.decisions].every(decision => !decision
      || (decision.sessionKey === sessionKey && (!decision.handoff || decision.handoff.sourceSessionKey === sessionKey)))
}

export function createV4JevRouting(rpc: JevRpcTransport, events: JevEventTransport): JevRouting {
  let disposed = false
  const listeners = new Set<(snapshot: JevSnapshot) => void>()
  const available = () => !disposed && [JEV_GET_METHOD, JEV_CONFIGURE_METHOD, JEV_RESPOND_METHOD]
    .every(method => rpc.supports?.(method) !== false)
  const subscription = events.subscribe(JEV_CHANGED_EVENT, payload => {
    if (disposed || !validateChangedPayload(payload)) return
    const value = payload as ChangedPayload
    if (!matchesSession(value, value.sessionKey)) return
    for (const listener of listeners) listener(value)
  })
  async function query<T extends JevSnapshot>(
    method: string,
    params: { sessionKey: string } & Record<string, unknown>,
    validateParams: (value: unknown) => boolean,
    validate: (value: unknown) => boolean,
    mutation = false,
    signal?: AbortSignal,
    ephemeral = false,
  ): Promise<JevSnapshot> {
    if (!available()) throw new Error('JEV recommendations are unavailable')
    if (!validateParams(params)) throw new Error('Invalid JEV request')
    const value = await rpc.request<T>(method, params, {
      timeoutMs: method === JEV_RESPOND_METHOD ? 90_000 : 10_000,
      timeoutAction: 'reject',
      recoveryClass: ephemeral ? 'ephemeral' : mutation ? 'mutation' : 'safe-read',
      ...(signal ? { signal, cancelOnAbort: true, abortAction: 'reject' as const } : {}),
    })
    if (!validate(value) || !matchesSession(value, params.sessionKey)) {
      throw new Error('JEV returned an invalid decision snapshot')
    }
    return value
  }
  return {
    available,
    previewAvailable: () => available() && rpc.supports?.(JEV_PREVIEW_METHOD) !== false,
    preview: (sessionKey, draftId, text, signal) => {
      if (rpc.supports?.(JEV_PREVIEW_METHOD) === false) return Promise.reject(new Error('JEV input assessment is unavailable'))
      return query<PreviewResult>(JEV_PREVIEW_METHOD, { sessionKey, draftId, text }, validatePreviewParams, validatePreviewResult, false, signal, true)
    },
    get: sessionKey => query<GetResult>(JEV_GET_METHOD, { sessionKey }, validateGetParams, validateGetResult),
    configure: (sessionKey: string, settings: JevConfiguration) => query<ConfigureResult>(
      JEV_CONFIGURE_METHOD, { sessionKey, ...settings }, validateConfigureParams, validateConfigureResult, true,
    ),
    respond: (sessionKey, decisionId, accepted) => query<RespondResult>(
      JEV_RESPOND_METHOD, { sessionKey, decisionId, accepted }, validateRespondParams, validateRespondResult, true,
    ),
    subscribe(listener) {
      if (!disposed) listeners.add(listener)
      return { close: () => { listeners.delete(listener) } }
    },
    dispose() {
      if (disposed) return
      disposed = true
      listeners.clear()
      subscription.close()
    },
  }
}
