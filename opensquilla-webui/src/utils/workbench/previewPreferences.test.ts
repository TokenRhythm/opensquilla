// @vitest-environment happy-dom
import { beforeEach, describe, expect, it } from 'vitest'
import { createWebPlatform } from '@/platform/web'
import {
  readPreviewPreferences,
  readWebPreviewPreferences,
  savePreviewPreferences,
  WEB_PREVIEW_PREFERENCES_KEY,
} from './previewPreferences'

beforeEach(() => localStorage.clear())

describe('Workbench preview preferences', () => {
  it('defaults new and existing web users to full preview', () => {
    expect(readWebPreviewPreferences()).toEqual({
      version: 1,
      mode: 'full',
      noticeShown: false,
    })
  })

  it('round-trips the versioned web preference', async () => {
    const platform = createWebPlatform()
    await savePreviewPreferences(platform, { mode: 'offline', noticeShown: true })
    expect(await readPreviewPreferences(platform)).toEqual({
      mode: 'offline',
      noticeShown: true,
    })
    expect(JSON.parse(localStorage.getItem(WEB_PREVIEW_PREFERENCES_KEY) || '{}'))
      .toMatchObject({ version: 1, mode: 'offline', noticeShown: true })
  })

  it('does not trust malformed or future stored records', () => {
    localStorage.setItem(WEB_PREVIEW_PREFERENCES_KEY, '{"version":2,"mode":"offline"}')
    expect(readWebPreviewPreferences().mode).toBe('full')
  })
})
