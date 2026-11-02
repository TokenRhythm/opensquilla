import type { TokenRhythmAccountApi, TokenRhythmAccountState, AccountAction, AccountLink, ModelCredentialSource, AccountMediaSource } from '../../../contracts/desktop/tokenrhythm-account'

export type WebAccountRequest = (path: 'state' | 'action' | 'source' | 'media-source' | 'link', data?: Record<string, string>) => Promise<{ state: TokenRhythmAccountState; url?: string | null }>

function openResult(url: string | null | undefined, popup: Window | null): void {
  if (!url) { popup?.close(); return }
  const target = new URL(url)
  if (target.protocol !== 'https:' || target.username || target.password) {
    popup?.close()
    throw new Error('ACCOUNT_LINK_UNAVAILABLE')
  }
  if (popup) popup.location.replace(target.href)
  else window.location.assign(target.href)
}

/** Local development adapter backed by the account host, not browser storage. */
export function createWebAccount(localAccountRequest: WebAccountRequest): TokenRhythmAccountApi {
  return {
    async getState() { return (await localAccountRequest('state')).state },
    async action(action: AccountAction) {
      const popup = ['login', 'reopen'].includes(action) ? window.open('', 'opensquilla-account-login') : null
      try {
        const result = await localAccountRequest('action', { action })
        openResult(result.url, popup)
        return result.state
      } catch (error) { popup?.close(); throw error }
    },
    async selectSource(source: ModelCredentialSource) { return (await localAccountRequest('source', { source })).state },
    async selectMediaSource(kind: 'image' | 'video', source: AccountMediaSource) { return (await localAccountRequest('media-source', { kind, source })).state },
    async openLink(link: AccountLink) {
      const popup = window.open('', 'opensquilla-account-link')
      try { openResult((await localAccountRequest('link', { link })).url, popup) }
      catch (error) { popup?.close(); throw error }
    },
    onState(callback) {
      const timer = window.setInterval(() => { void localAccountRequest('state').then(result => callback(result.state)).catch(() => {}) }, 2_500)
      return () => window.clearInterval(timer)
    },
  }
}
