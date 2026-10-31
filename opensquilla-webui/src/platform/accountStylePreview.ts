import type { TokenRhythmAccountState } from '../../../contracts/desktop/tokenrhythm-account'
import type { WebAccountRequest } from './webAccount'

// Synthetic, in-memory fixture enabled only by the explicit development preview.
// No account, payment, or provider request is sent.
const signedIn: TokenRhythmAccountState = {
  revision: 1,
  status: 'signed_in',
  profile: {
    accountId: 'preview-account',
    sessionId: 'preview-session',
    displayName: 'Demo user',
    maskedPhone: '',
  },
  wallet: {
    currency: 'CNY',
    balances: { available: '1241.62', recharge: '1200', bonus: '41.62', debt: '0', frozen: '0' },
    asOf: '2026-01-12T08:00:00Z',
  },
  walletStatus: 'ready',
  keyStatus: 'ready',
  errorCode: null,
  walletErrorCode: null,
  source: 'account',
  selectedAccountId: 'preview-account',
  mediaSources: { image: 'inherit', video: 'inherit' },
  pendingRevocation: false,
  loginExpiresAt: null,
}

let state = structuredClone(signedIn)

export const accountStylePreviewRequest: WebAccountRequest = async (path, data) => {
  if (path === 'link') throw new Error('ACCOUNT_LINK_UNAVAILABLE')
  if (path === 'action' && data?.action === 'logout') {
    state = { ...state, revision: state.revision + 1, status: 'signed_out',
      profile: null, wallet: null, walletStatus: 'idle', keyStatus: 'missing',
      source: 'manual', selectedAccountId: null,
      mediaSources: { image: 'inherit', video: 'inherit' } }
  } else if (path === 'action' && data?.action === 'login') {
    state = { ...structuredClone(signedIn), revision: state.revision + 1 }
  } else if (path === 'source' && (data?.source === 'manual' || data?.source === 'account')) {
    state = { ...state, revision: state.revision + 1, source: data.source,
      selectedAccountId: data.source === 'account' ? state.profile?.accountId ?? null : null }
  } else if (path === 'media-source'
    && (data?.kind === 'image' || data?.kind === 'video')
    && (data.source === 'inherit' || data.source === 'manual' || data.source === 'account')) {
    state = { ...state, revision: state.revision + 1,
      mediaSources: { ...state.mediaSources, [data.kind]: data.source } }
  }
  return { state: structuredClone(state) }
}
