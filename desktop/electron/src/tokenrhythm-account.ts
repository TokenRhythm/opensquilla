import { randomBytes, randomUUID, createHash, timingSafeEqual } from 'node:crypto'
import { createServer, type Server } from 'node:http'
import { mkdir, readFile, writeFile, rename, rm } from 'node:fs/promises'
import { join } from 'node:path'
import type { AddressInfo } from 'node:net'
import { TokenRhythmLoginResult } from './tokenrhythm-login-result.js'
import { LocalAccountStorageError, canRecoverAccountStorage } from './local-account-secrets.js'
import type { AccountLink, AccountMediaSource, AccountPrimaryActivation, AccountProfile, AccountWallet, ModelCredentialSource, TokenRhythmAccountState } from '../../../contracts/desktop/tokenrhythm-account.js'

const SCOPES = 'profile.read wallet.read managed_key.read managed_key.obtain session.revoke offline_access'
const CLIENT = 'opensquilla-desktop'
const API = '/api/v1/integrations/opensquilla/'
const ENDED = new Set(['ACCOUNT_SESSION_REVOKED', 'ACCOUNT_SESSION_EXPIRED', 'invalid_grant'])
const randomToken = () => randomBytes(32).toString('base64url')
export class AccountError extends Error {
  constructor(readonly code: string) { super(code) }
}
interface LoginTokens { accessToken: string; refreshToken: string; expiresAt: number; accountId: string; sessionId: string }
interface ModelKey { secret: string; keyId: string; generation: number; accountId: string }
interface SavedAccount {
  version: 1
  recoveryPending?: boolean
  tokens: LoginTokens | null
  key: ModelKey | null
  source: ModelCredentialSource
  selectedAccountId: string | null
  mediaSources: { image: AccountMediaSource; video: AccountMediaSource }
  mediaAccountIds: { image: string | null; video: string | null }
  videoJobs: Record<string, string>
  pending: LoginTokens[]
}
function emptySavedAccount(): SavedAccount {
  return { version: 1, tokens: null, key: null, source: 'account', selectedAccountId: null, mediaSources: { image: 'inherit', video: 'inherit' }, mediaAccountIds: { image: null, video: null }, videoJobs: {}, pending: [] }
}
interface LoginAttempt { state: string; verifier: string; redirect: string; url: string; server: Server; timer: NodeJS.Timeout; epoch: number; primaryActivationId: string; primaryActivationVersion: number }
interface AccountDeviceInfo { name: string; os: string }
export function describeAccountDevice(platform: string, systemVersion: string): AccountDeviceInfo | undefined {
  if (!/^[A-Za-z0-9][A-Za-z0-9 ._+()-]{0,47}$/.test(systemVersion)) return undefined
  const [major, minor, build] = systemVersion.split('.')
  if (platform === 'darwin') return { name: 'Mac', os: `macOS ${major === '10' ? `${major}.${minor}` : major}` }
  if (platform === 'win32') {
    const legacyVersions: Record<string, string> = { '1': '7', '2': '8', '3': '8.1' }
    const version = major === '10' ? (Number(build) >= 22000 ? '11' : '10')
      : major === '6' ? (legacyVersions[minor!] || systemVersion) : systemVersion
    return { name: 'PC', os: `Windows ${version}` }
  }
  if (platform === 'linux') return { name: 'Linux', os: `Linux ${systemVersion}` }
  return undefined
}
export interface AccountDependencies {
  directory: string
  storageFile?: string
  legacyStorageFile?: string
  origin: string
  deviceInfo?: AccountDeviceInfo
  assertSecureStorage(): void
  encrypt(value: string): string
  decrypt(value: string): string
  recoverStorage?(value: string): void
  openExternal(url: string): Promise<unknown>
  returnToWebUI?: string
  onState?(state: TokenRhythmAccountState): void
  onInvalidate?(): void
  onSignedIn?(): Promise<void>
  openAppViaProtocol?: boolean
  onOpenDesktop?(): Promise<void>
  fetch?: typeof fetch
  now?: () => number
  loginTimeoutMs?: number
}
export interface AccountMaintenancePause {
  wait(signal: AbortSignal): Promise<void>
  resume(): void
}

/** Hold admission through maintenance, including a timed-out drain's late writes. */
export function pauseAccountsForMaintenance(accounts: Iterable<TokenRhythmAccount>): AccountMaintenancePause {
  const pauses = [...accounts].map(account => account.pauseForMaintenance())
  const drained = Promise.all(pauses.map(pause => pause.drained)).then(() => {})
  return {
    wait: signal => new Promise<void>((resolve, reject) => {
      const abort = () => reject(signal.reason)
      if (signal.aborted) reject(signal.reason)
      else signal.addEventListener('abort', abort, { once: true })
      drained.then(resolve, reject).finally(() => signal.removeEventListener('abort', abort))
    }),
    resume: () => { for (const pause of pauses) pause.resume() },
  }
}
function string(value: unknown, limit = 2048): string {
  if (typeof value !== 'string' || !value || value.length > limit) throw new AccountError('INVALID_ACCOUNT_RESPONSE')
  return value
}
function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new AccountError('INVALID_ACCOUNT_RESPONSE')
  return value as Record<string, unknown>
}
function code(error: unknown): string { return error instanceof AccountError ? error.code : 'ACCOUNT_NETWORK_UNAVAILABLE' }
export function accountOrigin(value: string): string {
  const url = new URL(value)
  if (url.protocol !== 'https:' || url.username || url.password || url.pathname !== '/' || url.search || url.hash) throw new AccountError('INVALID_ACCOUNT_ORIGIN')
  return url.origin
}
export function equalToken(actual: string, expected: string): boolean {
  const a = Buffer.from(actual), b = Buffer.from(expected)
  return a.length === b.length && a.length > 0 && timingSafeEqual(a, b)
}

function activationId(value: unknown): string {
  if (typeof value !== 'string' || !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value)) throw new AccountError('INVALID_ACCOUNT_ACTIVATION')
  return value
}

/** Validate the renderer's complete expected identity before considering a bind. */
export function accountPrimaryActivation(value: unknown): AccountPrimaryActivation {
  if (!value || typeof value !== 'object' || Array.isArray(value)
    || Object.keys(value).sort().join(',') !== 'accountId,id,sessionId') throw new AccountError('INVALID_ACCOUNT_ACTIVATION')
  const request = value as Record<string, unknown>
  for (const key of ['accountId', 'sessionId']) {
    if (typeof request[key] !== 'string' || !/^[^\u0000-\u0020\u007f]{1,256}$/.test(request[key])) throw new AccountError('INVALID_ACCOUNT_ACTIVATION')
  }
  return { id: activationId(request.id), accountId: request.accountId as string, sessionId: request.sessionId as string }
}

/** The sole owner of account tokens and the shared model key. */
export class TokenRhythmAccount {
  readonly origin: string
  private readonly request: typeof fetch
  private readonly now: () => number
  private saved: SavedAccount = { version: 1, tokens: null, key: null, source: 'manual', selectedAccountId: null, mediaSources: { image: 'inherit', video: 'inherit' }, mediaAccountIds: { image: null, video: null }, videoJobs: {}, pending: [] }
  private state: TokenRhythmAccountState = { revision: 0, status: 'signed_out', profile: null, wallet: null, walletStatus: 'idle', keyStatus: 'missing', errorCode: null, walletErrorCode: null, source: 'manual', selectedAccountId: null, mediaSources: { image: 'inherit', video: 'inherit' }, pendingRevocation: false, loginExpiresAt: null }
  private installationId = ''
  private attempt: LoginAttempt | null = null
  private epoch = 0
  private writes = Promise.resolve()
  private refreshPromise: Promise<void> | null = null
  private revokePromise: Promise<void> | null = null
  private keyPromise: Promise<void> | null = null
  private modelKeyRejectedPromise: Promise<void> | null = null
  private modelKeyFence = 0
  private modelKeyRejectedPersistencePending = false
  private verifyPromise: Promise<void> | null = null
  private initialized: Promise<void> | null = null
  private loginStartPromise: Promise<TokenRhythmAccountState> | null = null
  private recoveryPromise: Promise<TokenRhythmAccountState> | null = null
  private loginCompletionPromise: Promise<void> | null = null
  private logoutPromise: Promise<TokenRhythmAccountState> | null = null
  private closePromise: Promise<void> | null = null
  private closing = false
  private readonly maintenancePauses = new Set<symbol>()
  private maintenanceDrain: Promise<void> | null = null
  private logoutPersistencePending = false
  private revocationPersistencePending = false
  private verifiedAt = 0
  private links: Partial<Record<AccountLink, string>> = {}
  private lease = ''
  private poll: NodeJS.Timeout | null = null
  private loginResult: TokenRhythmLoginResult | null = null
  private primaryActivation: AccountPrimaryActivation | null = null
  private primaryActivationLogin: { request: AccountPrimaryActivation; epoch: number; version: number } | null = null
  private primaryActivationVersion = 0
  private sourceSelectionVersion = 0
  constructor(private readonly deps: AccountDependencies) {
    this.origin = accountOrigin(deps.origin)
    this.request = deps.fetch ?? fetch
    this.now = deps.now ?? Date.now
  }
  snapshot(): TokenRhythmAccountState { return structuredClone(this.state) }
  private get admissionClosed(): boolean { return this.closing || this.maintenancePauses.size > 0 }
  private assertOpen(): void {
    if (this.admissionClosed) throw new AccountError('ACCOUNT_CLOSING')
  }
  private publish(patch: Partial<TokenRhythmAccountState> = {}): void {
    this.state = { ...this.state, ...patch, revision: this.state.revision + 1, source: this.saved.source, selectedAccountId: this.saved.selectedAccountId, mediaSources: { ...this.saved.mediaSources }, pendingRevocation: this.saved.pending.length > 0, primaryActivation: this.primaryActivation ? { ...this.primaryActivation } : null }
    this.deps.onState?.(this.snapshot())
  }
  private suppressPrimaryActivation(): void {
    this.primaryActivationVersion++
    this.primaryActivation = null
    this.primaryActivationLogin = null
  }
  private offerPrimaryActivation(): void {
    const login = this.primaryActivationLogin
    if (!login) return
    this.primaryActivationLogin = null
    if (login.epoch === this.epoch && login.version === this.primaryActivationVersion
      && login.request.accountId === this.saved.tokens?.accountId
      && login.request.sessionId === this.saved.tokens?.sessionId) this.primaryActivation = login.request
  }
  /** A discarded renderer cannot replay its login default in the next document. */
  invalidatePrimaryActivation(): void {
    // Metadata only: lifecycle invalidation also runs while maintenance/quit
    // closes public admission. Authentication and saved source choices survive.
    this.suppressPrimaryActivation()
    try { this.publish() } catch { /* The old renderer may already be gone. */ }
  }
  async cancelPrimaryActivation(id?: string): Promise<TokenRhythmAccountState> {
    this.assertOpen()
    if (id !== undefined) {
      activationId(id)
      if (this.primaryActivation?.id !== id && this.primaryActivationLogin?.request.id !== id) return this.snapshot()
    }
    // Also suppress a login whose exchange/verification has not produced an offer yet.
    this.suppressPrimaryActivation()
    this.publish()
    return this.snapshot()
  }
  private invalidate(): void {
    this.verifiedAt = 0
    this.lease = ''
    this.deps.onInvalidate?.()
  }
  /** Whether any account-backed model binding is still tied to the selected account. */
  private hasLocalModelBinding(): boolean {
    const accountId = this.saved.tokens?.accountId
    if (this.saved.recoveryPending === true || !accountId || this.saved.key?.accountId !== accountId) return false
    const primary = this.saved.source === 'account' && this.saved.selectedAccountId === accountId
    const media = (['image', 'video'] as const).some(kind => this.saved.mediaSources[kind] === 'account' && this.saved.mediaAccountIds[kind] === accountId)
    return primary || media
  }
  /** Restore a process-local lease without waiting for remote account verification. */
  private primeLocalModel(): boolean {
    if (!this.hasLocalModelBinding()) return false
    if (!this.lease) this.lease = `os_local_${randomToken()}`
    if (this.state.status !== 'signed_in' || this.state.keyStatus !== 'ready' || this.state.errorCode !== null) {
      this.publish({ status: 'signed_in', keyStatus: 'ready', errorCode: null })
    }
    return true
  }
  private queueWrite(write: () => Promise<void>): Promise<void> {
    const operation = this.writes.then(write)
    this.writes = operation.catch(() => {})
    return operation
  }
  private async persist(loginEpoch?: number): Promise<void> {
    this.deps.assertSecureStorage()
    const encrypted = this.deps.encrypt(JSON.stringify(this.saved))
    await this.queueWrite(async () => {
      await mkdir(this.deps.directory, { recursive: true, mode: 0o700 })
      const target = join(this.deps.directory, this.deps.storageFile ?? 'account.enc')
      const temp = `${target}.${randomUUID()}.tmp`
      try {
        await writeFile(temp, encrypted, { mode: 0o600, flag: 'wx' })
        await rename(temp, target)
        // Marker removal belongs to this login's disk transaction. Logout
        // queues its marker behind it, so a cancelled login cannot erase it.
        if (loginEpoch !== undefined && loginEpoch === this.epoch) {
          await rm(join(this.deps.directory, 'logged-out'), { force: true })
        }
      } finally { await rm(temp, { force: true }).catch(() => {}) }
    })
  }
  private async markModelKeyRejected(): Promise<void> {
    await this.queueWrite(async () => {
      await mkdir(this.deps.directory, { recursive: true, mode: 0o700 })
      const target = join(this.deps.directory, 'model-key-rejected')
      try {
        await readFile(target)
        return
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error
      }
      const temp = `${target}.${randomUUID()}.tmp`
      try {
        await writeFile(temp, '1', { mode: 0o600, flag: 'wx' })
        await rename(temp, target)
      } catch (error) {
        // Another rejection may have won the race to create the marker.
        if ((error as NodeJS.ErrnoException).code !== 'EEXIST') throw error
      } finally { await rm(temp, { force: true }).catch(() => {}) }
    })
  }
  private async clearModelKeyRejected(): Promise<void> {
    await this.queueWrite(async () => { await rm(join(this.deps.directory, 'model-key-rejected'), { force: true }) })
  }
  async initialize(): Promise<void> {
    this.assertOpen()
    if (!this.initialized) this.initialized = this.restore()
    await this.initialized
  }
  private async restore(): Promise<void> {
    try {
      const raw = await readFile(join(this.deps.directory, this.deps.storageFile ?? 'account.enc'), 'utf8').catch(async error => {
        if ((error as NodeJS.ErrnoException).code !== 'ENOENT' || !this.deps.legacyStorageFile) throw error
        // Reading the old OS-encrypted record would trigger native authorization.
        // Preserve it and let an explicit login establish the new local record.
        await readFile(join(this.deps.directory, this.deps.legacyStorageFile))
        throw new AccountError('ACCOUNT_STORAGE_REAUTH_REQUIRED')
      })
      this.deps.assertSecureStorage()
      const parsed = object(JSON.parse(this.deps.decrypt(raw)))
      if (parsed.version !== 1 || !['manual', 'account'].includes(String(parsed.source)) || !Array.isArray(parsed.pending)) throw new AccountError('ACCOUNT_STORAGE_INVALID')
      this.saved = parsed as unknown as SavedAccount
      this.saved.mediaSources ??= { image: 'inherit', video: 'inherit' }
      this.saved.mediaAccountIds ??= { image: null, video: null }
      this.saved.videoJobs ??= {}
      for (const item of [...this.saved.pending, ...(this.saved.tokens ? [this.saved.tokens] : [])]) {
        string(item.accessToken); string(item.refreshToken); string(item.accountId, 256); string(item.sessionId, 256)
        if (!Number.isFinite(item.expiresAt)) throw new AccountError('ACCOUNT_STORAGE_INVALID')
      }
      for (const kind of ['image', 'video'] as const) if (!['inherit', 'manual', 'account'].includes(this.saved.mediaSources[kind])) throw new AccountError('ACCOUNT_STORAGE_INVALID')
      if (this.saved.key) { string(this.saved.key.secret); string(this.saved.key.accountId, 256) }
      // A logout tombstone wins over an older encrypted record after a failed save.
      try {
        await readFile(join(this.deps.directory, 'logged-out'))
        if (this.saved.tokens) this.saved.pending.push(this.saved.tokens)
        this.saved.tokens = null; this.saved.key = null
        await this.persist()
      } catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error }
      try {
        await readFile(join(this.deps.directory, 'model-key-rejected'))
        this.saved.key = null
        this.invalidate()
      } catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error }
      const localModelReady = this.hasLocalModelBinding()
      if (localModelReady && !this.lease) this.lease = `os_local_${randomToken()}`
      this.publish({
        status: localModelReady ? 'signed_in' : this.saved.tokens ? 'verifying' : 'signed_out',
        keyStatus: localModelReady ? 'ready' : this.saved.tokens ? 'loading' : 'missing',
      })
    } catch (error) {
      if (error instanceof AccountError && error.code === 'ACCOUNT_STORAGE_REAUTH_REQUIRED') {
        this.saved.source = 'account'
        this.publish({ status: 'reauth_required', errorCode: 'ACCOUNT_LOGIN_REQUIRED' })
      } else if ((error as NodeJS.ErrnoException).code !== 'ENOENT') {
        // Never silently replace a record we could not decrypt.
        this.saved = emptySavedAccount()
        this.invalidate()
        const errorCode = error instanceof LocalAccountStorageError ? error.code
          : error instanceof SyntaxError || (error instanceof AccountError && ['ACCOUNT_STORAGE_INVALID', 'INVALID_ACCOUNT_RESPONSE'].includes(error.code))
            ? 'ACCOUNT_LOCAL_DATA_INVALID' : 'ACCOUNT_STORAGE_UNAVAILABLE'
        this.publish({ status: 'storage_unavailable', errorCode, profile: null, wallet: null, walletStatus: 'idle', keyStatus: 'missing' })
        return
      }
    }
    this.startPolling()
  }
  private startPolling(): void {
    if (!this.closing && !this.poll) {
      this.poll = setInterval(() => { void this.verify().catch(() => {}) }, 60_000)
      this.poll.unref()
      void this.verify().catch(() => {})
    }
  }
  private async installation(): Promise<string> {
    if (this.installationId) return this.installationId
    const path = join(this.deps.directory, 'installation-id')
    await mkdir(this.deps.directory, { recursive: true, mode: 0o700 })
    try {
      const value = (await readFile(path, 'utf8')).trim()
      if (!/^[0-9a-f-]{36}$/i.test(value)) throw new AccountError('ACCOUNT_STORAGE_INVALID')
      return this.installationId = value
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error
    }
    const value = randomUUID()
    await writeFile(path, value, { flag: 'wx', mode: 0o600 })
    return this.installationId = value
  }
  login(): Promise<TokenRhythmAccountState> {
    if (this.loginStartPromise) return this.loginStartPromise
    this.loginStartPromise = this.startLogin().finally(() => { this.loginStartPromise = null })
    return this.loginStartPromise
  }
  /** Explicit recovery only; ordinary login must never replace unreadable credentials. */
  recoverLogin(): Promise<TokenRhythmAccountState> {
    if (this.recoveryPromise) return this.recoveryPromise
    this.recoveryPromise = this.recoverAndLogin().finally(() => { this.recoveryPromise = null })
    return this.recoveryPromise
  }
  private async recoverAndLogin(): Promise<TokenRhythmAccountState> {
    await this.initialize()
    await this.writes
    this.assertOpen()
    if (this.state.status !== 'storage_unavailable' || !canRecoverAccountStorage(this.state.errorCode) || !this.deps.recoverStorage) throw new AccountError('ACCOUNT_RECOVERY_UNAVAILABLE')
    const next = emptySavedAccount()
    next.recoveryPending = true
    try {
      this.deps.assertSecureStorage()
      this.deps.recoverStorage(JSON.stringify(next))
    } catch { throw new AccountError('ACCOUNT_RECOVERY_FAILED') }
    this.saved = next
    this.epoch++
    this.modelKeyFence++
    this.sourceSelectionVersion++
    this.suppressPrimaryActivation()
    this.invalidate()
    this.links = {}
    this.logoutPersistencePending = false
    this.revocationPersistencePending = false
    this.publish({ status: 'signed_out', errorCode: null, profile: null, wallet: null, walletStatus: 'idle', keyStatus: 'missing', walletErrorCode: null, loginExpiresAt: null })
    this.startPolling()
    return this.login()
  }
  private async startLogin(): Promise<TokenRhythmAccountState> {
    // A manual choice made while OAuth is opening suppresses this default,
    // without cancelling the user's authentication.
    const primaryActivationVersion = this.primaryActivationVersion
    await this.initialize()
    this.assertOpen()
    if (this.state.status === 'storage_unavailable') throw new AccountError('ACCOUNT_STORAGE_UNAVAILABLE')
    try { this.deps.assertSecureStorage() } catch { throw new AccountError('ACCOUNT_STORAGE_UNAVAILABLE') }
    if (this.attempt) return this.snapshot()
    // A cancelled exchange can still replace the backend session for this
    // installation. Finish its cleanup before accepting a new authorization.
    await Promise.all([
      this.loginCompletionPromise, this.logoutPromise,
      this.verifyPromise, this.refreshPromise, this.keyPromise, this.modelKeyRejectedPromise,
    ].map(task => task?.catch(() => {})))
    this.assertOpen()
    if (this.state.status === 'verifying') throw new AccountError('ACCOUNT_LOGIN_IN_PROGRESS')
    if (this.saved.tokens) throw new AccountError('ACCOUNT_LOGOUT_REQUIRED')
    this.primaryActivation = null
    this.primaryActivationLogin = null
    this.sourceSelectionVersion++
    this.loginResult?.close()
    this.loginResult = null
    const installation = await this.installation()
    this.assertOpen()
    const state = randomToken(), verifier = randomToken(), epoch = ++this.epoch
    let result: TokenRhythmLoginResult | null = null
    const server = createServer((req, res) => {
      res.setHeader('Cache-Control', 'no-store')
      res.setHeader('Referrer-Policy', 'no-referrer')
      res.setHeader('Content-Security-Policy', "default-src 'none'; frame-ancestors 'none'")
      const attempt = this.attempt
      let url: URL
      try { url = new URL(req.url ?? '', 'http://127.0.0.1') } catch { res.writeHead(400).end(); return }
      if (result?.handle(req, res)) return
      if (!attempt || attempt.server !== server || req.method !== 'GET' || url.pathname !== '/oauth/callback' || url.searchParams.getAll('state').length !== 1 || !equalToken(url.searchParams.get('state') ?? '', attempt.state)) {
        res.writeHead(400).end('Invalid authorization callback.'); return
      }
      const authCode = url.searchParams.get('code')
      const denied = url.searchParams.get('error')
      if (url.searchParams.getAll('code').length > 1 || url.searchParams.getAll('error').length > 1 || (authCode && denied) || (!authCode && !denied)) { res.writeHead(400).end(); return }
      clearTimeout(attempt.timer)
      this.attempt = null
      const receipt = new TokenRhythmLoginResult(server, new URL(attempt.redirect).origin, this.deps.openAppViaProtocol ?? false,
        async () => {
          this.assertOpen()
          if (!this.deps.onOpenDesktop) throw new AccountError('ACCOUNT_BROWSER_UNAVAILABLE')
          await this.deps.onOpenDesktop()
        }, 300_000, this.deps.returnToWebUI)
      result = receipt
      this.loginResult = receipt
      // Remove the authorization code and state from the visible result URL.
      res.writeHead(303, { Location: receipt.path }).end()
      if (denied) {
        receipt.finish('cancelled')
        this.publish({ status: 'signed_out', errorCode: 'ACCOUNT_LOGIN_CANCELLED', loginExpiresAt: null })
      }
      else this.loginCompletionPromise = this.completeLogin(authCode!, attempt).catch(error => {
        if (epoch === this.epoch && !this.admissionClosed) this.publish({ status: this.saved.tokens ? 'unverified' : 'signed_out', errorCode: code(error), loginExpiresAt: null })
      }).finally(() => {
        receipt.finish(epoch !== this.epoch ? 'cancelled'
          : !this.admissionClosed && this.state.status === 'signed_in' && this.state.keyStatus === 'ready' ? 'success' : 'error')
        this.loginCompletionPromise = null
      })
    })
    await new Promise<void>((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve) })
    if (this.admissionClosed) { server.close(); throw new AccountError('ACCOUNT_CLOSING') }
    const redirect = `http://127.0.0.1:${(server.address() as AddressInfo).port}/oauth/callback`
    const url = new URL('/api/oauth/authorize', this.origin)
    url.search = new URLSearchParams({ response_type: 'code', client_id: CLIENT, redirect_uri: redirect, state, code_challenge: createHash('sha256').update(verifier).digest('base64url'), code_challenge_method: 'S256', scope: SCOPES, installation_id: installation }).toString()
    const timeout = this.deps.loginTimeoutMs ?? 300_000
    const timer = setTimeout(() => { this.cancel(); this.publish({ errorCode: 'ACCOUNT_LOGIN_TIMEOUT' }) }, timeout)
    timer.unref()
    this.attempt = { state, verifier, redirect, url: url.toString(), server, timer, epoch, primaryActivationId: randomUUID(), primaryActivationVersion }
    this.publish({ status: 'authorizing', errorCode: null, loginExpiresAt: this.now() + timeout })
    try { await this.deps.openExternal(url.toString()) } catch { this.publish({ errorCode: 'ACCOUNT_BROWSER_UNAVAILABLE' }) }
    return this.snapshot()
  }
  private closeAttempt(): void {
    if (!this.attempt) return
    clearTimeout(this.attempt.timer)
    this.attempt.server.close()
    this.attempt = null
  }
  cancel(): TokenRhythmAccountState {
    this.assertOpen()
    const hadActivation = Boolean(this.primaryActivation || this.primaryActivationLogin)
    this.suppressPrimaryActivation()
    if (!['authorizing', 'verifying'].includes(this.state.status)) {
      if (hadActivation) this.publish()
      return this.snapshot()
    }
    this.loginResult?.finish('cancelled')
    if (this.saved.tokens) {
      void this.logout().catch(() => this.publish({ errorCode: 'ACCOUNT_STORAGE_UNAVAILABLE' }))
      return this.snapshot()
    }
    this.epoch++
    this.closeAttempt()
    this.publish({ status: this.saved.tokens ? 'unverified' : 'signed_out', loginExpiresAt: null, errorCode: null })
    return this.snapshot()
  }
  async reopen(): Promise<TokenRhythmAccountState> {
    this.assertOpen()
    if (this.attempt) await this.deps.openExternal(this.attempt.url)
    return this.snapshot()
  }
  private async json(path: string, init: RequestInit): Promise<Record<string, unknown>> {
    let response: Response
    try { response = await this.request(this.origin + path, { ...init, redirect: 'error', signal: AbortSignal.timeout(20_000), cache: 'no-store' }) }
    catch { throw new AccountError('ACCOUNT_NETWORK_UNAVAILABLE') }
    if (response.status === 204) return {}
    if (!response.ok) {
      let body: Record<string, unknown> | null = null
      try { body = object(await response.json()) } catch { /* Status is authoritative for malformed error bodies. */ }
      const value = body && (typeof body.error === 'string' ? body.error : body.code)
      if (response.status === 401) throw new AccountError(path === '/api/oauth/token' && value === 'invalid_grant' ? 'invalid_grant' : 'ACCOUNT_HTTP_401')
      throw new AccountError(typeof value === 'string' && /^[a-zA-Z][a-zA-Z0-9_]{0,95}$/.test(value) ? value : `ACCOUNT_HTTP_${response.status}`)
    }
    let body: Record<string, unknown>
    try { body = object(await response.json()) } catch { throw new AccountError('INVALID_ACCOUNT_RESPONSE') }
    if (path === '/api/oauth/token') return body
    if (body.code !== 0) throw new AccountError('INVALID_ACCOUNT_RESPONSE')
    return object(body.data)
  }
  private async exchange(params: Record<string, string>): Promise<LoginTokens> {
    const headers: Record<string, string> = { 'Content-Type': 'application/x-www-form-urlencoded' }
    const device = this.deps.deviceInfo ?? describeAccountDevice(process.platform, process.getSystemVersion?.() || '')
    if (device) {
      headers['X-TokenRhythm-Device-Name'] = device.name
      headers['X-TokenRhythm-Device-OS'] = device.os
    }
    const raw = await this.json('/api/oauth/token', { method: 'POST', headers, body: new URLSearchParams({ client_id: CLIENT, ...params }).toString() })
    if (raw.token_type !== 'Bearer' || (raw.client_id && raw.client_id !== CLIENT) || typeof raw.expires_in !== 'number' || raw.expires_in <= 0 || raw.expires_in > 86400) throw new AccountError('INVALID_ACCOUNT_RESPONSE')
    const scopes = string(raw.scope).split(' ')
    if (SCOPES.split(' ').some(scope => !scopes.includes(scope))) throw new AccountError('INSUFFICIENT_SCOPE')
    return { accessToken: string(raw.access_token), refreshToken: string(raw.refresh_token), expiresAt: this.now() + raw.expires_in * 1000, accountId: string(raw.account_id, 256), sessionId: string(raw.session_id, 256) }
  }
  private async completeLogin(authCode: string, attempt: LoginAttempt): Promise<void> {
    this.publish({ status: 'verifying', loginExpiresAt: null })
    const tokens = await this.exchange({ grant_type: 'authorization_code', code: authCode, redirect_uri: attempt.redirect, code_verifier: attempt.verifier })
    if (attempt.epoch !== this.epoch) { await this.revokeUnacceptedLogin(tokens); return }
    this.saved.tokens = tokens
    try { await this.persist(attempt.epoch) } catch {
      this.saved.tokens = null
      await this.logoutTombstone().catch(() => {})
      await this.revokeUnacceptedLogin(tokens)
      throw new AccountError('ACCOUNT_STORAGE_UNAVAILABLE')
    }
    if (attempt.epoch !== this.epoch) return
    if (attempt.primaryActivationVersion === this.primaryActivationVersion) {
      this.primaryActivationLogin = {
        request: { id: attempt.primaryActivationId, accountId: tokens.accountId, sessionId: tokens.sessionId },
        epoch: attempt.epoch, version: attempt.primaryActivationVersion,
      }
    }
    if (!this.admissionClosed) {
      await this.verify(true)
      if (!this.admissionClosed && this.state.status === 'signed_in' && this.state.keyStatus === 'ready') await this.deps.onSignedIn?.().catch(() => {})
    }
  }
  private async refreshTokens(): Promise<void> {
    if (this.refreshPromise) return this.refreshPromise
    const original = this.saved.tokens, epoch = this.epoch
    if (!original) throw new AccountError('ACCOUNT_LOGIN_REQUIRED')
    this.refreshPromise = (async () => {
      const next = await this.exchange({ grant_type: 'refresh_token', refresh_token: original.refreshToken })
      if (next.accountId !== original.accountId || next.sessionId !== original.sessionId) throw new AccountError('ACCOUNT_IDENTITY_MISMATCH')
      if (epoch !== this.epoch) {
        const pending = this.saved.pending.find(item => item.sessionId === original.sessionId)
        if (pending) { Object.assign(pending, next); await this.persist() }
        else await this.revoke(next).catch(() => {})
        return
      }
      this.saved.tokens = next
      try { await this.persist() } catch {
        // The old refresh token cannot safely be reused after rotation.
        await this.logoutTombstone().catch(() => {})
        this.saved.tokens = null; this.saved.key = null; this.invalidate()
        this.publish({ status: 'reauth_required', errorCode: 'ACCOUNT_STORAGE_UNAVAILABLE', profile: null, wallet: null, keyStatus: 'missing' })
        throw new AccountError('ACCOUNT_STORAGE_UNAVAILABLE')
      }
    })().finally(() => { this.refreshPromise = null })
    return this.refreshPromise
  }
  private async resource(name: string, method = 'GET', retry = true): Promise<Record<string, unknown>> {
    this.assertOpen()
    if (!this.saved.tokens) throw new AccountError('ACCOUNT_LOGIN_REQUIRED')
    if (this.saved.tokens.expiresAt <= this.now() + 30_000) await this.refreshTokens()
    this.assertOpen()
    const tokens = this.saved.tokens
    if (!tokens) throw new AccountError('ACCOUNT_LOGIN_REQUIRED')
    try { return await this.json(API + name, { method, headers: { Authorization: `Bearer ${tokens.accessToken}` } }) }
    catch (error) {
      if (retry && code(error) === 'ACCOUNT_TOKEN_EXPIRED') { await this.refreshTokens(); return this.resource(name, method, false) }
      throw error
    }
  }
  async verify(force = false): Promise<void> {
    if (this.verifyPromise) return this.verifyPromise
    if (this.admissionClosed) return
    if (!this.saved.tokens) { void this.retryRevocations().catch(() => {}); return }
    if (!force && this.state.status === 'signed_in' && this.now() - this.verifiedAt < 60_000) return
    const epoch = this.epoch
    const modelKeyFence = this.modelKeyFence
    this.verifyPromise = (async () => {
      try {
        const raw = await this.resource('account')
        if (epoch !== this.epoch || this.admissionClosed || modelKeyFence !== this.modelKeyFence) return
        if (raw.account_id !== this.saved.tokens?.accountId || raw.session_id !== this.saved.tokens?.sessionId) throw new AccountError('ACCOUNT_IDENTITY_MISMATCH')
        const profile: AccountProfile = { accountId: string(raw.account_id, 256), sessionId: string(raw.session_id, 256), displayName: typeof raw.display_name === 'string' ? raw.display_name : '', maskedPhone: typeof raw.masked_phone === 'string' ? raw.masked_phone : '' }
        this.links = {}
        const links = object(raw.links)
        for (const name of ['account', 'usage', 'recharge'] as const) {
          const href = links[name]
          // Resolve root-relative links; keep same-origin absolute URLs for older backends.
          if (typeof href !== 'string' || href.length > 2048 || !/^(?:\/(?!\/)|https:\/\/)/.test(href) || /[\\\u0000-\u0020\u007f]/.test(href)) continue
          try { const url = new URL(href, this.origin); if (url.origin === this.origin && !url.username && !url.password) this.links[name] = url.toString() } catch { /* unavailable link */ }
        }
        this.verifiedAt = this.now()
        // Only an explicit exchange can leave this offer. A restored session,
        // polling, or key repair cannot create an activation request.
        this.offerPrimaryActivation()
        // Keep a locally bound model lease ready while the remote profile and
        // managed-key checks finish in the background.
        this.publish({
          status: 'signed_in', profile, errorCode: null,
          keyStatus: this.lease ? this.state.keyStatus : 'loading',
        })
        await Promise.all([this.refreshWallet(epoch), this.ensureKey(epoch)])
        if (epoch === this.epoch && !this.admissionClosed && this.saved.recoveryPending && this.state.keyStatus === 'ready') {
          // The old binding was encrypted with the lost key. Explicit recovery
          // binds only the newly verified account so existing account-backed
          // model configuration can resume without changing config.toml.
          this.saved.selectedAccountId = profile.accountId
          this.saved.recoveryPending = false
          try { await this.persist() } catch {
            this.saved.selectedAccountId = null
            this.saved.recoveryPending = true
            throw new AccountError('ACCOUNT_STORAGE_UNAVAILABLE')
          }
          this.publish()
        }
      } catch (error) {
        if (epoch !== this.epoch || this.admissionClosed || modelKeyFence !== this.modelKeyFence) return
        const errorCode = code(error)
        const terminal = ENDED.has(errorCode) || errorCode === 'ACCOUNT_HTTP_401' || !this.saved.tokens
          || ['ACCOUNT_IDENTITY_MISMATCH', 'ACCOUNT_STORAGE_UNAVAILABLE', 'ACCOUNT_LOCAL_DATA_INVALID', 'ACCOUNT_STORAGE_INVALID'].includes(errorCode)
        if (terminal) {
          this.invalidate()
          await this.logoutTombstone().catch(() => {})
          this.saved.tokens = null; this.saved.key = null
          this.suppressPrimaryActivation()
          await this.persist().catch(() => {})
          this.publish({ status: 'reauth_required', profile: null, wallet: null, walletStatus: 'idle', keyStatus: 'missing', errorCode })
        } else if (!this.primeLocalModel()) {
          this.publish({ status: 'unverified', errorCode: errorCode })
        }
      }
      // An old session's offline revocation must not delay a new account's
      // validation, activation or model requests.
      if (!this.admissionClosed) void this.retryRevocations().catch(() => {})
    })().finally(() => { this.verifyPromise = null })
    return this.verifyPromise
  }
  private async refreshWallet(epoch: number): Promise<void> {
    // Keep the last result visible during revalidation. A cached wallet only
    // becomes stale when a request actually fails, not when one starts.
    if (!this.state.wallet) this.publish({ walletStatus: 'loading' })
    try {
      const raw = await this.resource('wallet')
      if (epoch !== this.epoch) return
      if (raw.account_id !== this.saved.tokens?.accountId) throw new AccountError('ACCOUNT_IDENTITY_MISMATCH')
      const balances = object(raw.balances)
      for (const name of ['available', 'recharge', 'bonus', 'debt', 'frozen']) if (typeof balances[name] !== 'string' || !/^-?\d+(\.\d{1,16})?$/.test(balances[name] as string)) throw new AccountError('INVALID_ACCOUNT_RESPONSE')
      const wallet: AccountWallet = { currency: string(raw.currency, 8), balances: balances as unknown as AccountWallet['balances'], asOf: string(raw.as_of, 64) }
      this.publish({ wallet, walletStatus: 'ready', walletErrorCode: null })
    } catch (error) {
      if (epoch === this.epoch) this.publish({ walletStatus: this.state.wallet ? 'stale' : 'error', walletErrorCode: code(error) })
      if (ENDED.has(code(error))) throw error
    }
  }
  private async ensureKey(epoch: number, force = false): Promise<void> {
    if (this.keyPromise) return this.keyPromise
    const modelKeyFence = this.modelKeyFence
    this.keyPromise = this.loadKey(epoch, force, modelKeyFence).finally(() => { this.keyPromise = null })
    return this.keyPromise
  }
  private async loadKey(epoch: number, force: boolean, modelKeyFence: number): Promise<void> {
    if (!this.saved.key || force) this.publish({ keyStatus: 'loading' })
    try {
      let obtain = force || !this.saved.key
      if (!obtain) {
        const metadata = await this.resource('managed-key')
        if (epoch !== this.epoch || modelKeyFence !== this.modelKeyFence) return
        if (metadata.account_id !== this.saved.tokens?.accountId || metadata.app_id !== 'opensquilla') throw new AccountError('ACCOUNT_IDENTITY_MISMATCH')
        if (metadata.status !== 'active') throw new AccountError('MANAGED_KEY_DISABLED')
        obtain = metadata.key_id !== this.saved.key?.keyId || metadata.generation !== this.saved.key?.generation
      }
      if (obtain) {
        const raw = await this.resource('managed-key/credential', 'POST')
        if (epoch !== this.epoch || modelKeyFence !== this.modelKeyFence) return
        if (raw.account_id !== this.saved.tokens?.accountId || raw.app_id !== 'opensquilla' || raw.status !== 'active' || !Number.isInteger(raw.generation)) throw new AccountError('ACCOUNT_IDENTITY_MISMATCH')
        this.saved.key = { secret: string(raw.secret), keyId: string(raw.key_id, 256), generation: Number(raw.generation), accountId: string(raw.account_id, 256) }
        await this.persist()
        if (epoch !== this.epoch || modelKeyFence !== this.modelKeyFence) return
        try { await this.clearModelKeyRejected() } catch { throw new AccountError('ACCOUNT_STORAGE_UNAVAILABLE') }
      }
      if (epoch !== this.epoch || modelKeyFence !== this.modelKeyFence) return
      if (!this.lease) this.lease = `os_local_${randomToken()}`
      this.publish({ keyStatus: 'ready', errorCode: null })
    } catch (error) {
      if (epoch !== this.epoch || modelKeyFence !== this.modelKeyFence) return
      const errorCode = code(error)
      const terminal = ENDED.has(errorCode) || errorCode === 'ACCOUNT_HTTP_401'
        || ['ACCOUNT_IDENTITY_MISMATCH', 'MANAGED_KEY_DISABLED', 'ACCOUNT_STORAGE_UNAVAILABLE'].includes(errorCode)
      if (terminal) {
        this.invalidate()
        this.saved.key = null
        this.publish({ keyStatus: errorCode === 'MANAGED_KEY_DISABLED' ? 'disabled' : 'unavailable', errorCode })
        this.modelKeyRejectedPersistencePending = true
        try {
          await this.markModelKeyRejected()
          await this.persist()
          this.modelKeyRejectedPersistencePending = false
        } catch {
          this.publish({ status: 'storage_unavailable', keyStatus: 'missing', errorCode: 'ACCOUNT_STORAGE_UNAVAILABLE' })
        }
        if (ENDED.has(errorCode)) throw error
      } else if (!this.primeLocalModel()) {
        this.publish({ keyStatus: this.saved.key ? 'ready' : 'unavailable' })
      }
    }
  }
  async testModel(): Promise<TokenRhythmAccountState> {
    this.assertOpen()
    await this.verify(true)
    const lease = this.lease
    const secret = this.readModelCredential(lease, false)
    let response: Response
    try { response = await this.request(this.origin + '/v1/models', { headers: { Authorization: `Bearer ${secret}` }, redirect: 'error', signal: AbortSignal.timeout(20_000) }) }
    catch { throw new AccountError('ACCOUNT_MODEL_TEST_FAILED') }
    await response.body?.cancel()
    if (!response.ok) { if (response.status === 401) await this.modelKeyRejected(lease, secret); throw new AccountError('ACCOUNT_MODEL_TEST_FAILED') }
    return this.snapshot()
  }
  async retryKey(): Promise<TokenRhythmAccountState> {
    this.assertOpen()
    await this.verify(true)
    this.assertOpen()
    if (this.state.status === 'signed_in') {
      await this.ensureKey(this.epoch, true)
      if (!this.admissionClosed && this.state.keyStatus === 'ready') await this.deps.onSignedIn?.().catch(() => {})
    }
    return this.snapshot()
  }
  async selectSource(source: ModelCredentialSource, expected?: AccountPrimaryActivation): Promise<TokenRhythmAccountState> {
    this.assertOpen()
    if (source !== 'manual' && source !== 'account') throw new AccountError('INVALID_ACCOUNT_SOURCE')
    const request = expected === undefined ? undefined : accountPrimaryActivation(expected)
    if (request && source !== 'account') throw new AccountError('INVALID_ACCOUNT_ACTIVATION')
    const selectionVersion = ++this.sourceSelectionVersion
    if (!request) {
      this.suppressPrimaryActivation()
      this.publish()
    }
    await this.initialize()
    this.assertOpen()
    const assertCurrent = () => {
      if (selectionVersion !== this.sourceSelectionVersion || request && (
        this.primaryActivation?.id !== request.id
        || this.primaryActivation.accountId !== request.accountId
        || this.primaryActivation.sessionId !== request.sessionId
        || this.saved.tokens?.accountId !== request.accountId
        || this.saved.tokens?.sessionId !== request.sessionId
      )) throw new AccountError(request ? 'ACCOUNT_PRIMARY_ACTIVATION_STALE' : 'ACCOUNT_SOURCE_CHANGED')
    }
    assertCurrent()
    if (source === 'manual' && this.saved.source === 'manual') return this.snapshot()
    if (source === 'account') {
      await this.verify()
      this.assertOpen()
      // No await may separate this comparison from the source/binding mutation.
      assertCurrent()
      if (this.state.status !== 'signed_in' || this.state.keyStatus !== 'ready' || !this.saved.tokens) throw new AccountError('ACCOUNT_MODEL_UNAVAILABLE')
    }
    const selectionEpoch = this.epoch
    const previous = { source: this.saved.source, selectedAccountId: this.saved.selectedAccountId }
    this.saved.source = source
    if (source === 'account') this.saved.selectedAccountId = this.saved.tokens!.accountId
    try { await this.persist() } catch (error) {
      // A later choice/logout owns the current state, including when its disk
      // transaction is queued behind this failed write.
      if (selectionVersion === this.sourceSelectionVersion && selectionEpoch === this.epoch) {
        Object.assign(this.saved, previous)
        this.publish()
      }
      throw error
    }
    assertCurrent()
    if (request && this.primaryActivation?.id === request.id) this.primaryActivation = null
    this.publish()
    return this.snapshot()
  }
  async selectMediaSource(kind: 'image' | 'video', source: AccountMediaSource): Promise<TokenRhythmAccountState> {
    await this.initialize()
    this.assertOpen()
    if (!['image', 'video'].includes(kind) || !['inherit', 'manual', 'account'].includes(source)) throw new AccountError('INVALID_ACCOUNT_SOURCE')
    if (source === 'account' && (this.state.status !== 'signed_in' || this.state.keyStatus !== 'ready')) throw new AccountError('ACCOUNT_MODEL_UNAVAILABLE')
    const previous = { source: this.saved.mediaSources[kind], accountId: this.saved.mediaAccountIds[kind] }
    if (source === 'account') this.saved.mediaAccountIds[kind] = this.saved.tokens!.accountId
    this.saved.mediaSources[kind] = source
    try { await this.persist() } catch (error) {
      this.saved.mediaSources[kind] = previous.source
      this.saved.mediaAccountIds[kind] = previous.accountId
      throw error
    }
    this.publish()
    return this.snapshot()
  }
  async openLink(name: AccountLink): Promise<void> {
    this.assertOpen()
    if (!['account', 'usage', 'recharge'].includes(name) || !this.links[name]) throw new AccountError('ACCOUNT_LINK_UNAVAILABLE')
    await this.deps.openExternal(this.links[name]!)
  }
  private async revoke(tokens: LoginTokens): Promise<void> {
    let current = tokens
    if (current.expiresAt <= this.now()) {
      current = await this.exchange({ grant_type: 'refresh_token', refresh_token: tokens.refreshToken })
      if (current.accountId !== tokens.accountId || current.sessionId !== tokens.sessionId) throw new AccountError('ACCOUNT_IDENTITY_MISMATCH')
      Object.assign(tokens, current)
      await this.persist()
    }
    await this.json(API + 'session', { method: 'DELETE', headers: { Authorization: `Bearer ${current.accessToken}` } })
  }
  private async revokeUnacceptedLogin(tokens: LoginTokens): Promise<void> {
    // Cancellation can overtake the token response. Persist its exact session
    // before network cleanup so a failure remains retryable after restart.
    if (!this.saved.pending.some(item => item.sessionId === tokens.sessionId)) this.saved.pending.push(tokens)
    this.revocationPersistencePending = true
    try {
      await this.persist()
      this.revocationPersistencePending = false
    } catch {
      this.publish({ errorCode: 'ACCOUNT_STORAGE_UNAVAILABLE' })
    }
    this.publish()
    await this.retryRevocations()
  }
  private async retryRevocations(): Promise<void> {
    if (this.revokePromise) return this.revokePromise
    this.revokePromise = this.drainRevocations().finally(() => { this.revokePromise = null })
    return this.revokePromise
  }
  private async drainRevocations(): Promise<void> {
    if (this.refreshPromise) await this.refreshPromise.catch(() => {})
    for (const tokens of [...this.saved.pending]) {
      if (this.admissionClosed) break
      try { await this.revoke(tokens) } catch (error) { if (!ENDED.has(code(error))) continue }
      this.saved.pending = this.saved.pending.filter(item => item !== tokens)
      await this.persist().catch(() => {})
      this.publish()
    }
  }
  private async logoutTombstone(): Promise<void> {
    // A failed marker cannot make an older encrypted login safe to restore.
    // Shutdown must save the current cleared state before it can succeed.
    this.logoutPersistencePending = true
    await this.queueWrite(async () => {
      await mkdir(this.deps.directory, { recursive: true, mode: 0o700 })
      await writeFile(join(this.deps.directory, 'logged-out'), '1', { mode: 0o600 })
    })
    this.logoutPersistencePending = false
  }
  logout(): Promise<TokenRhythmAccountState> {
    if (this.logoutPromise) return this.logoutPromise
    this.logoutPromise = this.performLogout().finally(() => { this.logoutPromise = null })
    return this.logoutPromise
  }
  private async performLogout(): Promise<TokenRhythmAccountState> {
    this.assertOpen()
    this.sourceSelectionVersion++
    this.suppressPrimaryActivation()
    this.loginResult?.finish('cancelled')
    this.epoch++; this.modelKeyFence++; this.closeAttempt(); this.invalidate()
    const tombstone = this.logoutTombstone()
    if (this.saved.tokens) this.saved.pending.push(this.saved.tokens)
    this.saved.tokens = null; this.saved.key = null; this.links = {}
    this.publish({ status: 'signed_out', profile: null, wallet: null, walletStatus: 'idle', keyStatus: 'missing', errorCode: null, walletErrorCode: null, loginExpiresAt: null })
    await tombstone
    await this.persist()
    if (!this.admissionClosed) void this.retryRevocations().catch(() => {})
    return this.snapshot()
  }
  /** Private Gateway boundary; never expose this through preload. */
  async runtimeState(): Promise<Record<string, unknown>> {
    await this.initialize()
    const localModelReady = this.primeLocalModel()
    if (this.state.status !== 'storage_unavailable') void this.verify().catch(() => {})
    return { revision: this.state.revision, source: this.saved.source, selectedAccountId: this.saved.selectedAccountId, accountId: this.saved.tokens?.accountId ?? null, ready: localModelReady, credential: localModelReady ? this.lease : '', origin: this.origin, mediaSources: this.saved.mediaSources, mediaAccountIds: this.saved.mediaAccountIds }
  }
  private readModelCredential(lease: string, requireBinding: boolean): string {
    this.assertOpen()
    const key = this.saved.key
    const accountId = this.saved.tokens?.accountId
    if (!equalToken(lease, this.lease) || !accountId || this.saved.recoveryPending === true || !key || key.accountId !== accountId || requireBinding && !this.hasLocalModelBinding()) throw new AccountError('ACCOUNT_MODEL_UNAVAILABLE')
    return key.secret
  }
  async modelCredential(lease: string): Promise<string> { return this.readModelCredential(lease, true) }
  async recordVideoJob(id: string, lease: string): Promise<void> {
    this.assertOpen()
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(id) || !equalToken(lease, this.lease) || !this.saved.tokens) throw new AccountError('ACCOUNT_VIDEO_UNAVAILABLE')
    this.saved.videoJobs[id] = this.saved.tokens.accountId
    await this.persist()
  }
  ownsVideoJob(id: string): boolean {
    return !!this.saved.tokens && this.saved.videoJobs[id] === this.saved.tokens.accountId
  }
  async modelKeyRejected(lease?: string, secret?: string): Promise<void> {
    if (lease !== undefined && !equalToken(lease, this.lease)) return
    if (secret !== undefined && this.saved.key?.secret !== secret) return
    if (this.modelKeyRejectedPromise) return this.modelKeyRejectedPromise
    if (!this.saved.tokens && !this.saved.key) return
    this.modelKeyRejectedPromise = (async () => {
      // An upstream 401 is a terminal rejection of the credential currently in
      // use. Drop the process lease and persisted managed key so the next model
      // request cannot replay a credential the provider has rejected.
      this.modelKeyFence++
      this.modelKeyRejectedPersistencePending = true
      this.invalidate()
      this.saved.key = null
      if (!this.closing) this.publish({ keyStatus: 'unavailable', errorCode: 'ACCOUNT_MODEL_UNAUTHORIZED' })
      try {
        // The marker is written before the encrypted record so a failed record
        // write cannot resurrect the rejected key after restart.
        await this.markModelKeyRejected()
        await this.persist()
        this.modelKeyRejectedPersistencePending = false
      } catch (error) {
        if (!this.closing) this.publish({ status: 'storage_unavailable', keyStatus: 'missing', errorCode: 'ACCOUNT_STORAGE_UNAVAILABLE' })
        throw error instanceof AccountError ? error : new AccountError('ACCOUNT_STORAGE_UNAVAILABLE')
      }
    })().finally(() => { this.modelKeyRejectedPromise = null })
    return this.modelKeyRejectedPromise
  }
  private async drainOperations(): Promise<void> {
    await Promise.allSettled([
      this.initialized, this.loginStartPromise, this.loginCompletionPromise, this.recoveryPromise,
      this.verifyPromise, this.refreshPromise, this.keyPromise, this.modelKeyRejectedPromise,
      this.logoutPromise, this.revokePromise,
    ])
    await this.writes
    if (this.modelKeyRejectedPersistencePending) {
      await this.markModelKeyRejected()
      await this.persist()
      this.modelKeyRejectedPersistencePending = false
    }
    if (this.logoutPersistencePending || this.revocationPersistencePending) {
      await this.persist()
      this.logoutPersistencePending = false
      this.revocationPersistencePending = false
    }
  }
  pauseForMaintenance(): { drained: Promise<void>; resume(): void } {
    if (this.closing) throw new AccountError('ACCOUNT_CLOSING')
    const owner = Symbol('account maintenance pause')
    this.maintenancePauses.add(owner)
    this.loginResult?.close()
    this.loginResult = null
    if (this.attempt) {
      this.closeAttempt()
      this.publish({ status: 'signed_out', loginExpiresAt: null, errorCode: null })
    }
    if (!this.maintenanceDrain) {
      this.maintenanceDrain = this.drainOperations().finally(() => {
        this.invalidate()
        this.maintenanceDrain = null
      })
    }
    const drained = this.maintenanceDrain
    let released = false
    const resume = () => {
      if (released) return
      released = true
      const release = () => {
        this.maintenancePauses.delete(owner)
        if (!this.admissionClosed && this.initialized) {
          void this.verify(true).then(async () => {
            if (!this.admissionClosed && this.state.status === 'signed_in' && this.state.keyStatus === 'ready') await this.deps.onSignedIn?.()
          }).catch(() => {})
        }
      }
      // A timeout releases only this operation's ownership after its accepted
      // token exchange and disk writes settle. Subsequent maintenance keeps its
      // own hold, and a real Quit can never be reopened by a late callback.
      void drained.then(release, release)
    }
    return { drained, resume }
  }
  close(): Promise<void> {
    if (this.closePromise) return this.closePromise
    this.closing = true
    this.closeAttempt()
    this.loginResult?.close()
    this.loginResult = null
    if (this.poll) clearInterval(this.poll)
    // A refresh may already have rotated the server token while its response
    // or encrypted write is pending. Keep its epoch valid until it is saved;
    // shutdown is not logout and must not revoke that session.
    this.closePromise = (async () => {
      await this.drainOperations()
      this.epoch++
      this.invalidate()
    })().catch(error => {
      // Keep admission closed, but let another Quit retry the durable save
      // after the filesystem or secure storage becomes available again.
      this.closePromise = null
      throw error
    })
    return this.closePromise
  }
}
