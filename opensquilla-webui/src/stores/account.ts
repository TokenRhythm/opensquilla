import { computed, ref, shallowRef } from 'vue'
import { createPinia, defineStore, getActivePinia } from 'pinia'
import { usePlatform } from '@/platform'
import type { AccountAction, AccountLink, AccountMediaSource, AccountPrimaryActivation, ModelCredentialSource, TokenRhythmAccountState } from '../../../contracts/desktop/tokenrhythm-account'

const accountStore = defineStore('tokenrhythm-account', () => {
  const api = usePlatform().account
  const supported = Boolean(api)
  const state = shallowRef<TokenRhythmAccountState | null>(null)
  const busy = ref(false)
  const refreshing = ref(false)
  const primaryMutationBusy = ref(false)
  const error = ref('')
  const primaryActivationSuppressed = ref(false)
  const cancelledPrimaryActivations = shallowRef(new Set<string>())
  let started = false
  let refreshPromise: Promise<TokenRhythmAccountState> | null = null
  // A restored session may be offline before its profile can be fetched.
  // Keep logout/retry available without inventing cached identity data.
  const loggedIn = computed(() => ['signed_in', 'unverified'].includes(state.value?.status || ''))
  const ready = computed(() => state.value?.status === 'signed_in' && state.value.keyStatus === 'ready')
  const primaryActivation = computed(() => {
    const request = state.value?.primaryActivation
    return request && !primaryActivationSuppressed.value && !cancelledPrimaryActivations.value.has(request.id)
      ? request : null
  })
  const name = computed(() => state.value?.profile?.displayName || state.value?.profile?.maskedPhone || 'TokenRhythm')
  const initial = computed(() => Array.from(name.value)[0] || 'T')
  function accept(value: TokenRhythmAccountState) {
    if (!state.value || value.revision >= state.value.revision) state.value = value
  }
  async function run(operation: () => Promise<TokenRhythmAccountState>, blocking = true) {
    if (blocking) busy.value = true
    error.value = ''
    try { const result = await operation(); accept(result); return result }
    catch (cause) {
      const message = cause instanceof Error ? cause.message : ''
      error.value = message.match(/(?:ACCOUNT|MANAGED_KEY|INSUFFICIENT_SCOPE)[A-Z_]*/)?.[0] || 'ACCOUNT_NETWORK_UNAVAILABLE'
      throw cause
    } finally { if (blocking) busy.value = false }
  }
  async function init() {
    if (started || !api) return
    started = true
    api.onState(accept)
    window.addEventListener('focus', () => { if (!busy.value && !refreshing.value) void action('refresh').catch(() => {}) })
    try { accept(await api.getState()) } catch { error.value = 'ACCOUNT_NETWORK_UNAVAILABLE' }
  }
  async function cancelPrimaryActivation(id?: string): Promise<void> {
    const requestId = id ?? state.value?.primaryActivation?.id
    if (requestId) {
      const cancelled = new Set(cancelledPrimaryActivations.value)
      cancelled.add(requestId)
      if (cancelled.size > 64) cancelled.delete(cancelled.values().next().value!)
      cancelledPrimaryActivations.value = cancelled
    }
    // A manual choice also supersedes an OAuth attempt whose identity has not
    // arrived yet. A late completion must not undo that choice.
    if (id === undefined) primaryActivationSuppressed.value = true
    if (api?.cancelPrimaryActivation) {
      const latest = await api.cancelPrimaryActivation(id)
      if (latest) accept(latest)
    }
  }
  function action(value: AccountAction): Promise<TokenRhythmAccountState> {
    if (value === 'login') primaryActivationSuppressed.value = false
    if (value === 'cancel' || value === 'logout') {
      // Suppress locally immediately, without delaying the normal logout or
      // cancel IPC; those host operations invalidate the same intent too.
      void cancelPrimaryActivation().catch(() => {})
    }
    if (value !== 'refresh') return run(() => api!.action(value))
    if (refreshPromise) return refreshPromise
    // Revalidation must not dim or block unrelated account controls.
    refreshing.value = true
    refreshPromise = run(() => api!.action(value), false).finally(() => {
      refreshing.value = false
      refreshPromise = null
    })
    return refreshPromise
  }
  const selectSource = (value: ModelCredentialSource, expected?: AccountPrimaryActivation) => run(async () => {
    if (!expected) await cancelPrimaryActivation()
    return api!.selectSource(value, expected)
  })
  const selectMediaSource = (kind: 'image' | 'video', value: AccountMediaSource) => run(() => api!.selectMediaSource(kind, value))
  async function openLink(link: AccountLink) { try { await api?.openLink(link) } catch { error.value = 'ACCOUNT_LINK_UNAVAILABLE' } }
  return { supported, state, busy, refreshing, primaryMutationBusy, primaryActivation, error, loggedIn, ready, name, initial, init, action, selectSource, selectMediaSource, cancelPrimaryActivation, openLink }
})

// Also supports independently mounted desktop recovery/settings surfaces.
const standalonePinia = createPinia()
export const useAccountStore = () => accountStore(getActivePinia() ?? standalonePinia)
