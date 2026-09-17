import type { Platform, WorkbenchPreviewMode } from '@/platform/types'

export const WEB_PREVIEW_PREFERENCES_KEY = 'opensquilla.workbench.preview.v1'

interface WebPreviewPreferences {
  version: 1
  mode: WorkbenchPreviewMode
  noticeShown: boolean
}

const DEFAULT_PREFERENCES: WebPreviewPreferences = {
  version: 1,
  mode: 'full',
  noticeShown: false,
}

function mode(value: unknown): WorkbenchPreviewMode {
  return value === 'offline' ? 'offline' : 'full'
}

export function readWebPreviewPreferences(
  storage: Pick<Storage, 'getItem'> | null = typeof localStorage === 'undefined'
    ? null
    : localStorage,
): WebPreviewPreferences {
  if (!storage) return { ...DEFAULT_PREFERENCES }
  try {
    const raw = JSON.parse(storage.getItem(WEB_PREVIEW_PREFERENCES_KEY) || 'null') as
      | Record<string, unknown>
      | null
    if (!raw || raw.version !== 1) return { ...DEFAULT_PREFERENCES }
    return {
      version: 1,
      mode: mode(raw.mode),
      noticeShown: raw.noticeShown === true,
    }
  } catch {
    return { ...DEFAULT_PREFERENCES }
  }
}

export function writeWebPreviewPreferences(
  preferences: Pick<WebPreviewPreferences, 'mode' | 'noticeShown'>,
  storage: Pick<Storage, 'setItem'> | null = typeof localStorage === 'undefined'
    ? null
    : localStorage,
): void {
  if (!storage) return
  storage.setItem(WEB_PREVIEW_PREFERENCES_KEY, JSON.stringify({
    version: 1,
    mode: mode(preferences.mode),
    noticeShown: preferences.noticeShown === true,
  }))
}

export async function readPreviewPreferences(platform: Platform): Promise<{
  mode: WorkbenchPreviewMode
  noticeShown: boolean
}> {
  if (platform.id !== 'desktop' || !platform.settings.getDesktopPreferences) {
    const preferences = readWebPreviewPreferences()
    return {
      mode: preferences.mode,
      noticeShown: preferences.noticeShown,
    }
  }
  try {
    const value = await platform.settings.getDesktopPreferences()
    return {
      mode: mode(value.effectiveWorkbenchPreviewMode ?? value.workbenchPreviewMode),
      noticeShown: value.workbenchPreviewNoticeShown === true,
    }
  } catch {
    return { mode: 'full', noticeShown: false }
  }
}

export async function savePreviewPreferences(
  platform: Platform,
  preferences: { mode: WorkbenchPreviewMode; noticeShown: boolean },
): Promise<void> {
  if (platform.id === 'desktop' && platform.settings.saveDesktopPreferences) {
    await platform.settings.saveDesktopPreferences({
      workbenchPreviewMode: preferences.mode,
      workbenchPreviewNoticeShown: preferences.noticeShown,
    })
    return
  }
  writeWebPreviewPreferences(preferences)
}
