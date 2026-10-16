import type { Ref } from 'vue'
import { createClientRequestId } from '@/utils/chat/messageIdentity'
import { parseSideChatCommand, type SideChatLauncher } from '@/modules/sideChatLauncher'

export function useSideChatCommand(options: {
  launcher: SideChatLauncher | null
  inputText: Ref<string>
  sessionKey: Ref<string>
  revision: Ref<number>
  identity(): string | null | undefined
  allowed(): boolean
  hasAttachments(): boolean
  active(): boolean
  notify(message: string): void
  translate(key: string): string
  resized(): void
}) {
  let attempt: { signature: string; id: string; pending?: Promise<boolean> } | null = null
  async function run(command: string): Promise<boolean> {
    const parsed = parseSideChatCommand(command)
    if (!parsed) return false
    const reject = (key: string) => { options.notify(options.translate(`chat.slashCommands.side.${key}`)); return true }
    if (!options.allowed()) return reject('nested')
    if (options.hasAttachments()) return reject('attachments')
    if (!options.launcher || !options.active()) return reject('unavailable')
    const key = options.sessionKey.value
    const identity = options.identity()
    const revision = options.revision.value
    const original = options.inputText.value
    const signature = JSON.stringify([key, identity, revision, original, parsed.text])
    if (attempt?.signature === signature && attempt.pending) return attempt.pending
    const current = attempt?.signature === signature ? attempt : { signature, id: createClientRequestId() }
    attempt = current
    const isCurrent = () => options.active() && options.allowed() && !options.hasAttachments() && options.sessionKey.value === key
      && options.identity() === identity && options.revision.value === revision && options.inputText.value === original
    current.pending = (async () => {
      try {
        const accepted = await options.launcher!.open({ parentKey: key, requestId: current.id, text: parsed.text, isCurrent })
        if (accepted && isCurrent()) {
          options.inputText.value = ''
          options.resized()
          if (attempt === current) attempt = null
        } else if (!accepted && isCurrent()) reject('failed')
      } catch (error) {
        if (isCurrent()) options.notify(error instanceof Error ? error.message : options.translate('chat.slashCommands.side.failed'))
      } finally { current.pending = undefined }
      return true
    })()
    return current.pending
  }
  return { run }
}
