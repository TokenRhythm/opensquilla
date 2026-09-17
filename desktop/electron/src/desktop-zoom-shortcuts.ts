import type { Input, WebContents } from 'electron'

export const DESKTOP_ZOOM_MIN_FACTOR = 0.5
export const DESKTOP_ZOOM_MAX_FACTOR = 3
export const DESKTOP_ZOOM_STEP_FACTOR = 1.2

export type DesktopZoomCommand = 'in' | 'out' | 'reset'

type DesktopZoomInput = Pick<
  Input,
  'type' | 'key' | 'code' | 'control' | 'alt' | 'meta'
>

export function desktopZoomCommandForInput(
  input: DesktopZoomInput,
  platform: NodeJS.Platform = process.platform,
): DesktopZoomCommand | null {
  if (input.type !== 'keyDown' || input.alt) return null
  const primaryModifier = platform === 'darwin' ? input.meta : input.control
  if (!primaryModifier) return null
  if (input.key === '0' || input.code === 'Digit0' || input.code === 'Numpad0') return 'reset'
  if (input.key === '+' || input.key === '=' || input.code === 'Equal' || input.code === 'NumpadAdd') return 'in'
  if (input.key === '-' || input.key === '_' || input.code === 'Minus' || input.code === 'NumpadSubtract') return 'out'
  return null
}

export function desktopZoomFactor(
  currentFactor: number,
  command: DesktopZoomCommand,
): number {
  if (command === 'reset') return 1
  const candidate = command === 'in'
    ? currentFactor * DESKTOP_ZOOM_STEP_FACTOR
    : currentFactor / DESKTOP_ZOOM_STEP_FACTOR
  return Math.min(DESKTOP_ZOOM_MAX_FACTOR, Math.max(DESKTOP_ZOOM_MIN_FACTOR, candidate))
}

export function installDesktopZoomShortcuts(
  inputContents: WebContents,
  zoomContents: WebContents = inputContents,
  onZoomApplied: () => void = () => {},
): () => void {
  const listener = (event: Electron.Event, input: Input) => {
    const command = desktopZoomCommandForInput(input)
    if (!command) return
    event.preventDefault()
    zoomContents.setZoomFactor(desktopZoomFactor(zoomContents.getZoomFactor(), command))
    onZoomApplied()
  }
  inputContents.on('before-input-event', listener)
  return () => inputContents.removeListener('before-input-event', listener)
}
