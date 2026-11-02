import { accountOrigin } from './tokenrhythm-account.js'

export const DEFAULT_TOKENRHYTHM_ORIGIN = 'https://tokenrhythm.studio'

/** Package metadata survives a normal Finder launch without shell variables. */
export function resolveAccountOrigin(packagedOrigin: unknown, runtimeOverride?: string): string {
  const origin = runtimeOverride || packagedOrigin || DEFAULT_TOKENRHYTHM_ORIGIN
  if (typeof origin !== 'string') throw new Error('Invalid packaged TokenRhythm origin')
  return accountOrigin(origin)
}
