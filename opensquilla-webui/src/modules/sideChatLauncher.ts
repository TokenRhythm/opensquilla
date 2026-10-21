import { inject, provide, type InjectionKey } from 'vue'

/** A local UI hand-off. Execution and acceptance remain owned by the child ChatView. */
export interface SideChatLaunchRequest {
  parentKey: string
  requestId: string
  text: string
  isCurrent(): boolean
}

export interface SideChatLaunchHandle {
  launchSideChat(request: SideChatLaunchRequest): Promise<boolean>
}

export interface SideChatLauncher {
  register(handler: (request: SideChatLaunchRequest) => Promise<boolean>): () => void
  open(request: SideChatLaunchRequest): Promise<boolean>
}

export const SIDE_CHAT_LAUNCHER_KEY: InjectionKey<SideChatLauncher> = Symbol('side-chat-launcher')

export function createSideChatLauncher(): SideChatLauncher {
  let owner: ((request: SideChatLaunchRequest) => Promise<boolean>) | null = null
  return {
    register(handler) {
      owner = handler
      return () => { if (owner === handler) owner = null }
    },
    async open(request) {
      return request.isCurrent() && owner ? owner(request) : false
    },
  }
}

export function provideSideChatLauncher() {
  const launcher = createSideChatLauncher()
  provide(SIDE_CHAT_LAUNCHER_KEY, launcher)
  return launcher
}

export function useSideChatLauncher() { return inject(SIDE_CHAT_LAUNCHER_KEY, null) }

export function parseSideChatCommand(value: string): { text: string } | null {
  const match = /^\/(?:side|btw)(?:\s+([\s\S]*))?$/i.exec(value.trim())
  return match ? { text: (match[1] || '').trim() } : null
}
