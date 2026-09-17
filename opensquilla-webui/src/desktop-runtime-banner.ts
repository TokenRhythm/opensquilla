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
  const chinese = (navigator.language || '').toLowerCase().startsWith('zh')
  if (sandboxUpgradeNeedsAttention(state.sandboxUpgrade)) {
    return chinese
      ? '沙箱设置升级未完成，当前运行仍可用；请查看日志并重启后重试。'
      : 'Sandbox settings were not fully upgraded. The runtime is usable; check the log and retry after restarting.'
  }
  if (state.status === 'error') {
    return state.error || (chinese ? '本地运行时启动失败。' : 'The local runtime failed to start.')
  }
  if (state.status === 'ready') return ''
  if (state.status === 'starting') {
    return chinese ? '正在启动本地运行时…' : 'Starting the local runtime…'
  }
  return chinese ? '正在准备本地运行时…' : 'Preparing the local runtime…'
}

function installDesktopRuntimeBanner(): void {
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
  ) return

  const render = (state: RuntimeBannerState): void => {
    const text = runtimeMessage(state)
    const migrationAttention = sandboxUpgradeNeedsAttention(state.sandboxUpgrade)
    banner.hidden = state.status === 'ready' && !migrationAttention
    banner.dataset.state = migrationAttention ? 'migration-warning' : state.status
    message.textContent = text
    retry.hidden = state.status !== 'error' || migrationAttention
    reveal.hidden = state.status !== 'error' && !migrationAttention
  }

  retry.addEventListener('click', () => {
    retry.disabled = true
    void gateway.retryStartup?.().finally(() => { retry.disabled = false })
  })
  reveal.addEventListener('click', () => { void gateway.revealLog?.() })

  gateway.onConnection(state => {
    render(state)
  })
  void gateway.getConnection().then(state => {
    render(state)
  })
}

installDesktopRuntimeBanner()
