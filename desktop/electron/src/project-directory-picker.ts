import type { OpenDialogOptions } from 'electron'

function requestedInitialPath(payload: unknown): string | undefined {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return undefined
  const initialPath = (payload as Record<string, unknown>).initialPath
  if (typeof initialPath !== 'string') return undefined
  return initialPath.trim() || undefined
}

export function projectDirectoryDialogOptions(
  platform: NodeJS.Platform,
  payload: unknown,
): OpenDialogOptions {
  const initialPath = requestedInitialPath(payload)
  return {
    ...(initialPath ? { defaultPath: initialPath } : {}),
    properties: platform === 'darwin'
      ? ['openDirectory', 'createDirectory']
      : ['openDirectory'],
  }
}
