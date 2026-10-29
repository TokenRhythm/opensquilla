import { watch } from 'vue'
import i18n from './i18n'
import type { DesktopGatewayConnection } from './platform/types'
import { getPlatform } from './platform'
import type { SandboxUpgradeReport } from './platform/types'

type RuntimeBannerState = DesktopGatewayConnection & {
  sandboxUpgrade?: SandboxUpgradeReport | null
}

const SANDBOX_ATTENTION_STATUSES = new Set([
  'partial_commit',
  'cleanup_pending',
  'retry_required',
  'legacy_artifacts_present',
])

function sandboxUpgradeNeedsAttention(report: SandboxUpgradeReport | null | undefined): boolean {
  return Boolean(report?.status && SANDBOX_ATTENTION_STATUSES.has(report.status))
}

function runtimeMessage(state: RuntimeBannerState): string {
  if (state.status === 'stopped') return ''
  if (sandboxUpgradeNeedsAttention(state.sandboxUpgrade)) {
    return i18n.global.t('desktopRuntime.sandboxUpgradeIncomplete')
  }
  if (state.status === 'error') {
    return state.error || i18n.global.t('desktopRuntime.startFailed')
  }
  if (state.status === 'starting') {
    return i18n.global.t('desktopRuntime.starting')
  }
  return ''
}

/** Install after the application's initial locale has been loaded. */
export function installDesktopRuntimeBanner(): () => void {
  const gateway = getPlatform().gateway
  const banner = document.getElementById('desktop-runtime-banner')
  const message = document.getElementById('desktop-runtime-message')
  const retry = document.getElementById('desktop-runtime-retry') as HTMLButtonElement | null
  const reveal = document.getElementById('desktop-runtime-log') as HTMLButtonElement | null
  if (
    !gateway.getConnection
    || !gateway.onConnection
    || !banner
    || !message
    || !retry
    || !reveal
  ) return () => {}

  let currentState: RuntimeBannerState | null = null
  let disposed = false

  const render = (state: RuntimeBannerState): void => {
    const text = runtimeMessage(state)
    const migrationAttention = state.status !== 'stopped'
      && sandboxUpgradeNeedsAttention(state.sandboxUpgrade)
    banner.hidden = !text
    banner.dataset.state = migrationAttention ? 'migration-warning' : state.status
    message.textContent = text
    retry.textContent = i18n.global.t('desktopRuntime.retry')
    reveal.textContent = i18n.global.t('desktopRuntime.showLog')
    retry.hidden = state.status !== 'error' || migrationAttention
    reveal.hidden = state.status !== 'error' && !migrationAttention
  }

  const retryStartup = (): void => {
    if (currentState?.status !== 'error') return
    retry.disabled = true
    void gateway.retryStartup?.().catch(() => {}).finally(() => { retry.disabled = false })
  }
  const revealLog = (): void => { void gateway.revealLog?.() }
  retry.addEventListener('click', retryStartup)
  reveal.addEventListener('click', revealLog)

  const receive = (state: RuntimeBannerState): void => {
    // The initial request may resolve after a newer shutdown notification.
    if (disposed || (currentState && state.revision < currentState.revision)) return
    currentState = state
    render(state)
  }
  const unsubscribe = gateway.onConnection(receive)
  const stopLocaleWatch = watch(i18n.global.locale, () => {
    if (currentState) render(currentState)
  }, { flush: 'sync' })
  void gateway.getConnection().then(receive).catch(() => {})
  return () => {
    disposed = true
    unsubscribe()
    stopLocaleWatch()
    retry.removeEventListener('click', retryStartup)
    reveal.removeEventListener('click', revealLog)
  }
}
