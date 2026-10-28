import type { TokenRhythmAccountState } from '../../../contracts/desktop/tokenrhythm-account'
import type { WebAccountRequest } from './webAccount'

// Visual-only fixture for the local settings preview. No account request is sent.
const state: TokenRhythmAccountState = {
  revision: 1,
  status: 'signed_in',
  profile: {
    accountId: 'preview-account',
    sessionId: 'preview-session',
    displayName: 'vp6uk8',
    maskedPhone: '152****3525',
  },
  wallet: {
    currency: 'CNY',
    balances: { available: '0', recharge: '0', bonus: '0', debt: '0', frozen: '0' },
    asOf: '2026-10-28T08:42:25Z',
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

export const accountStylePreviewRequest: WebAccountRequest = async () => ({ state })
