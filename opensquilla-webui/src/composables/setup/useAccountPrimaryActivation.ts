import { inject, onScopeDispose, watch } from 'vue'
import i18n from '@/i18n'
import { APP_SETTINGS_KEY, AppSettingsError, type AppSettings } from '@/modules/appSettings'
import type { GatewayAccess } from '@/modules/gatewayAccess'
import { ProviderConfigurationError } from '@/modules/providerConfiguration'
import { SETUP_WORKFLOW_KEY, SetupWorkflowError, type SetupWorkflow } from '@/modules/setupWorkflow'
import { useAccountStore } from '@/stores/account'
import { useToasts } from '@/composables/useToasts'
import { isSetupCapabilityUnsupported, setupSaveFailedMessage } from '@/utils/setupErrorPresentation'
import type { AccountPrimaryActivation, TokenRhythmAccountState } from '../../../../contracts/desktop/tokenrhythm-account'
import {
  AccountPrimaryActivationError,
  activateAccountPrimary,
  primaryProviderIsConfigured,
  representativeAccountProviderModel,
} from './accountPrimaryActivation'
import { invalidateReadiness, onReadinessInvalidated } from './readinessInvalidation'

type ActivationRequest = AccountPrimaryActivation

interface ActivationAccount {
  readonly supported: boolean
  readonly ready: boolean
  readonly state: TokenRhythmAccountState | null
  readonly primaryActivation: ActivationRequest | null | undefined
  primaryMutationBusy: boolean
  init(): Promise<void>
  selectSource(source: 'account' | 'manual', expected?: ActivationRequest): Promise<unknown>
  cancelPrimaryActivation(id?: string): Promise<unknown>
}

interface ActivationDependencies {
  account: ActivationAccount
  setupWorkflow: SetupWorkflow
  appSettings: AppSettings
  pushToast?: ReturnType<typeof useToasts>['pushToast']
}

type ActivationGateway = Pick<GatewayAccess,
  'isAvailable' | 'isLocalOwner' | 'deliveryIdentity' | 'subscriptionEpoch'>

class ActivationSuperseded extends Error {
  constructor() { super('ACCOUNT_PRIMARY_ACTIVATION_STALE') }
}

function uncertain(error: unknown): boolean {
  return !(error instanceof SetupWorkflowError || error instanceof ProviderConfigurationError
    || error instanceof AppSettingsError) || error.code === 'unavailable'
}

/** Consume explicit host login intents for the lifetime of App, including while Settings is closed. */
export function useAccountPrimaryActivation(
  gateway: ActivationGateway,
  dependencies?: ActivationDependencies,
): void {
  const account = dependencies?.account ?? useAccountStore()
  const setupWorkflow = dependencies?.setupWorkflow ?? inject(SETUP_WORKFLOW_KEY, null)
  const appSettings = dependencies?.appSettings ?? inject(APP_SETTINGS_KEY, null)
  const pushToast = dependencies?.pushToast ?? useToasts().pushToast
  if (!account.supported || !setupWorkflow || !appSettings) return
  const workflow = setupWorkflow
  const settings = appSettings
  const t = i18n.global.t
  const attempted = new Set<string>()
  let alive = true
  let running = false
  let queued = false

  function eligible(): ActivationRequest | null {
    const request = account.primaryActivation
    const profile = account.state?.profile
    return alive && account.ready && gateway.isAvailable && gateway.isLocalOwner
      && request && profile?.accountId === request.accountId && profile.sessionId === request.sessionId
      && !attempted.has(request.id) ? { ...request } : null
  }

  async function consume(request: ActivationRequest) {
    const delivery = gateway.deliveryIdentity
    const epoch = gateway.subscriptionEpoch
    let claimed = false
    let preparationFailed = false
    let bindingAcknowledged = false
    const isCurrent = () => {
      const profile = account.state?.profile
      const pending = account.primaryActivation
      return alive && account.ready && gateway.isAvailable && gateway.isLocalOwner
        && gateway.deliveryIdentity === delivery && gateway.subscriptionEpoch === epoch
        && profile?.accountId === request.accountId && profile.sessionId === request.sessionId
        && ((pending?.id === request.id && pending.accountId === request.accountId
          && pending.sessionId === request.sessionId) || (bindingAcknowledged && !pending
          && account.state?.source === 'account' && account.state.selectedAccountId === request.accountId))
    }
    const assertCurrent = () => { if (!isCurrent()) throw new ActivationSuperseded() }
    async function readSnapshot() {
      // Every result belongs to the captured login and Gateway connection. The
      // effective source distinguishes an operator primary from materialized defaults.
      const [catalog, status, config, effective] = await Promise.all([
        workflow.catalog().then(value => { assertCurrent(); return value }),
        workflow.status().then(value => { assertCurrent(); return value }),
        settings.readAll().then(value => { assertCurrent(); return value }),
        settings.readEffective().then(value => { assertCurrent(); return value }),
      ])
      assertCurrent()
      return { catalog, status, config, effective }
    }
    async function prepareSnapshot() {
      for (let attempt = 0; ; attempt += 1) {
        try { return await readSnapshot() }
        catch (error) {
          assertCurrent()
          if (attempt >= 2 || !uncertain(error)) throw error
          // Startup can advertise transport readiness just before configuration
          // reads succeed. Only these pre-mutation reads get bounded retries.
          await new Promise(resolve => setTimeout(resolve, attempt === 0 ? 250 : 750))
          assertCurrent()
        }
      }
    }
    try {
      const snapshot = await prepareSnapshot()
      assertCurrent()
      // Loading and readiness reads do not claim the mutation lock. A catalog
      // that has not arrived yet can be reconsidered on a readiness notification.
      if (!snapshot.catalog.providers?.some(provider => (
        String(provider.providerId || '').trim().toLowerCase() === 'tokenrhythm'
        && provider.runtimeSupported === true
      )) || account.primaryMutationBusy) return
      const config = snapshot.config as Parameters<typeof representativeAccountProviderModel>[0]
        & { llm?: { provider?: unknown } }
      const llm = config.llm
      const configured = primaryProviderIsConfigured(llm,
        snapshot.status as Parameters<typeof primaryProviderIsConfigured>[1], snapshot.effective)
      const currentProvider = configured ? String(llm?.provider || '').trim().toLowerCase() : ''
      const hasStoredProfile = Object.keys(config.llm_profiles || {}).some(id => id.trim().toLowerCase() === 'tokenrhythm')
      const providerLabel = (id: string) => String(snapshot.catalog.providers?.find(provider => (
        String(provider.providerId || '').trim().toLowerCase() === id.trim().toLowerCase()
      ))?.label || id)
      account.primaryMutationBusy = true
      claimed = true
      attempted.add(request.id)
      assertCurrent()
      const result = await activateAccountPrimary({
        setupWorkflow: workflow,
        currentProvider,
        hasStoredProfile,
        model: representativeAccountProviderModel(config, snapshot.catalog),
        providerLabel,
        assertCurrent,
        bind: async () => {
          assertCurrent()
          await account.selectSource('account', request)
          bindingAcknowledged = true
          assertCurrent()
        },
        refresh: async () => { await readSnapshot(); assertCurrent() },
      })
      assertCurrent()
      if (result.status === 'cancelled') return
      invalidateReadiness()
      pushToast(t(result.routerAction === 'disable' ? 'setup.provider.routerOutcomeDisabled'
        : currentProvider !== 'tokenrhythm' || result.routerAction === 'use_recommended'
          ? 'setup.provider.routerOutcomeSynchronized' : 'setup.provider.routerOutcomePreserved'),
      { tone: 'ok', dedupeKey: 'account.primary-activation' })
    } catch (error) {
      const failure = error instanceof AccountPrimaryActivationError ? error.cause : error
      const providerCommitted = error instanceof AccountPrimaryActivationError && error.providerCommitted
      const bindingCommitted = error instanceof AccountPrimaryActivationError && error.bindingCommitted
      if (providerCommitted || bindingCommitted) invalidateReadiness()
      // An obsolete login, cancelled intent, or changed connection cannot
      // publish an error against the user's newer account/provider choice.
      if (!isCurrent() || failure instanceof ActivationSuperseded
        || (failure instanceof Error && failure.message.includes('ACCOUNT_PRIMARY_ACTIVATION_STALE'))) return
      if (!claimed) {
        preparationFailed = true
        attempted.add(request.id)
        pushToast(t('setup.toast.loadFailed', {
          error: failure instanceof Error ? failure.message : String(failure),
        }), { tone: 'danger' })
        return
      }
      if (providerCommitted || bindingCommitted) {
        pushToast(t(bindingCommitted ? 'setup.modelStrategy.savedRefreshFailed'
          : 'setup.provider.primarySavedAdditionalFailed'), { tone: 'warn' })
      } else {
        const unknownResult = uncertain(failure)
        if (unknownResult || (failure instanceof SetupWorkflowError && failure.reason === 'already-active')) {
          // Reconcile observations only. A lost mutation response never permits replay.
          await readSnapshot().catch(() => {})
          if (!isCurrent()) return
          invalidateReadiness()
        }
        const message = unknownResult ? t('setup.provider.primarySaveUncertain')
          : `${setupSaveFailedMessage(failure)}${isSetupCapabilityUnsupported(failure)
            ? ` ${t('setup.provider.upgradeGatewayHint')}` : ''}`
        pushToast(message, { tone: 'danger' })
      }
    } finally {
      if (claimed || preparationFailed) {
        // Clearing is terminal even after a rejected confirmation or failure.
        // The store suppresses this ID synchronously before its host round trip.
        void account.cancelPrimaryActivation(request.id).catch(() => {})
        if (claimed) account.primaryMutationBusy = false
      }
    }
  }

  function schedule() {
    const request = eligible()
    if (!request || account.primaryMutationBusy) return
    if (running) { queued = true; return }
    running = true
    void consume(request).finally(() => {
      running = false
      if (queued) { queued = false; schedule() }
    })
  }
  const stop = watch([
    () => account.primaryActivation?.id,
    () => account.primaryActivation?.accountId,
    () => account.primaryActivation?.sessionId,
    () => account.ready,
    () => account.state?.status,
    () => account.state?.profile?.accountId,
    () => account.state?.profile?.sessionId,
    () => account.primaryMutationBusy,
    () => gateway.isAvailable,
    () => gateway.isLocalOwner,
    () => gateway.deliveryIdentity,
    () => gateway.subscriptionEpoch,
  ], schedule, { immediate: true })
  const unsubscribe = onReadinessInvalidated(schedule)
  void account.init()
  onScopeDispose(() => { alive = false; stop(); unsubscribe() })
}
