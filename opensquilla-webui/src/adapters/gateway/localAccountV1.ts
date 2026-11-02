import type { TokenRhythmAccountState } from '../../../../contracts/desktop/tokenrhythm-account'
import { createPrivateHttpTransport, HttpTransportError } from './privateHttpTransport'

export interface LocalAccountReply {
  state: TokenRhythmAccountState
  url?: string | null
}

let http: ReturnType<typeof createPrivateHttpTransport> | null = null

/** Account presentation boundary. The transport never carries website tokens. */
export async function localAccountRequest(path: 'state' | 'action' | 'source' | 'media-source' | 'link', data?: Record<string, string>): Promise<LocalAccountReply> {
  try {
    http ??= createPrivateHttpTransport({ authToken: () => '' })
    return await http.requestJson<LocalAccountReply>(`/api/local-account/${path}`, data
      ? { method: 'POST', json: data, cache: 'no-store' }
      : { method: 'GET', cache: 'no-store' })
  } catch (error) {
    if (error instanceof HttpTransportError) {
      const payload = error.payload as { error?: unknown } | undefined
      throw new Error(typeof payload?.error === 'string' ? payload.error : 'ACCOUNT_NETWORK_UNAVAILABLE')
    }
    throw error
  }
}
