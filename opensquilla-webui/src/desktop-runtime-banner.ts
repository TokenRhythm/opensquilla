import type { DesktopGatewayConnection } from './platform/types'
import { getPlatform } from './platform'

function runtimeMessage(state: DesktopGatewayConnection): string {
  const chinese = (navigator.language || '').toLowerCase().startsWith('zh')
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

  const render = (state: DesktopGatewayConnection): void => {
    const text = runtimeMessage(state)
    banner.hidden = state.status === 'ready'
    banner.dataset.state = state.status
    message.textContent = text
    retry.hidden = state.status !== 'error'
    reveal.hidden = state.status !== 'error'
  }

  retry.addEventListener('click', () => {
    retry.disabled = true
    void gateway.retryStartup?.().finally(() => { retry.disabled = false })
  })
  reveal.addEventListener('click', () => { void gateway.revealLog?.() })

  gateway.onConnection(render)
  void gateway.getConnection().then(render)
}

installDesktopRuntimeBanner()
