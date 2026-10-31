import { canRecoverAccountStorage } from './local-account-secrets.js'
import type { TokenRhythmAccount } from './tokenrhythm-account.js'
import type { TokenRhythmAccountState } from '../../../contracts/desktop/tokenrhythm-account.js'

const pending = new WeakMap<TokenRhythmAccount, Promise<TokenRhythmAccountState>>()

/** All UI sign-in entry points share the same explicit recovery confirmation. */
export function requestAccountLogin(account: TokenRhythmAccount, confirm: () => Promise<boolean>): Promise<TokenRhythmAccountState> {
  const existing = pending.get(account)
  if (existing) return existing
  const operation = (async () => {
    await account.initialize()
    const state = account.snapshot()
    if (state.status === 'storage_unavailable' && canRecoverAccountStorage(state.errorCode)) {
      if (!await confirm()) return account.snapshot()
      return account.recoverLogin()
    }
    return account.login()
  })().finally(() => { pending.delete(account) })
  pending.set(account, operation)
  return operation
}
