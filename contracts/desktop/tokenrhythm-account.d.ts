/** Public desktop account boundary. Never add tokens or model credentials here. */
export type AccountStatus = 'signed_out' | 'authorizing' | 'verifying' | 'signed_in' | 'unverified' | 'reauth_required' | 'storage_unavailable'
export type ManagedKeyStatus = 'missing' | 'loading' | 'ready' | 'unavailable' | 'disabled'
export type ModelCredentialSource = 'manual' | 'account'
export type AccountMediaSource = 'inherit' | 'manual' | 'account'
export interface AccountProfile {
  accountId: string
  sessionId: string
  displayName: string
  maskedPhone: string
}
export interface AccountWallet {
  currency: string
  balances: { available: string; recharge: string; bonus: string; debt: string; frozen: string }
  asOf: string
}
export interface TokenRhythmAccountState {
  revision: number
  status: AccountStatus
  profile: AccountProfile | null
  wallet: AccountWallet | null
  walletStatus: 'idle' | 'loading' | 'ready' | 'stale' | 'error'
  keyStatus: ManagedKeyStatus
  errorCode: string | null
  walletErrorCode: string | null
  source: ModelCredentialSource
  selectedAccountId: string | null
  mediaSources: { image: AccountMediaSource; video: AccountMediaSource }
  pendingRevocation: boolean
  loginExpiresAt: number | null
}
export type AccountAction = 'login' | 'cancel' | 'reopen' | 'refresh' | 'retry-key' | 'logout' | 'test'
export type AccountLink = 'account' | 'usage' | 'recharge'
export interface TokenRhythmAccountApi {
  getState(): Promise<TokenRhythmAccountState>
  action(action: AccountAction): Promise<TokenRhythmAccountState>
  selectSource(source: ModelCredentialSource): Promise<TokenRhythmAccountState>
  selectMediaSource(kind: 'image' | 'video', source: AccountMediaSource): Promise<TokenRhythmAccountState>
  openLink(link: AccountLink): Promise<void>
  onState(callback: (state: TokenRhythmAccountState) => void): () => void
}
