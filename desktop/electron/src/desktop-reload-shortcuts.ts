import type { Input, WebContents } from 'electron'

export type DesktopReloadCommand = 'reload'

export type DesktopReloadInput = Pick<
  Input,
  'type' | 'key' | 'code' | 'control' | 'alt' | 'meta' | 'shift'
>

export function desktopReloadCommandForInput(
  input: DesktopReloadInput,
  platform: NodeJS.Platform = process.platform,
): DesktopReloadCommand | null {
  if (
    platform === 'darwin'
    || input.type !== 'keyDown'
    || !input.control
    || input.alt
    || input.meta
    || input.shift
  ) return null

  const key = input.key.toLowerCase()
  return key === 'r' || input.code === 'KeyR' ? 'reload' : null
}

export function installDesktopReloadShortcuts(
  inputContents: WebContents,
  reloadContents: WebContents = inputContents,
  canReload: () => boolean = () => true,
  onReload: () => void = () => reloadContents.reload(),
): () => void {
  const listener = (event: Electron.Event, input: Input) => {
    if (desktopReloadCommandForInput(input) !== 'reload' || !canReload()) return
    event.preventDefault()
    onReload()
  }
  inputContents.on('before-input-event', listener)
  return () => inputContents.removeListener('before-input-event', listener)
}
