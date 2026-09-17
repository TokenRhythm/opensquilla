import { describe, expect, it } from 'vitest'
import {
  browserUrlFromWorkbenchItem,
  createBrowserWorkbenchItem,
  normalizeBrowserUrl,
} from './browserItems'

describe('browser Workbench items', () => {
  it('accepts only credential-free HTTP(S) entry URLs', () => {
    expect(normalizeBrowserUrl('https://example.com/path')).toBe('https://example.com/path')
    expect(normalizeBrowserUrl('file:///etc/passwd')).toBe('')
    expect(normalizeBrowserUrl('javascript:alert(1)')).toBe('')
    expect(normalizeBrowserUrl('https://example.com/\u0000')).toBe('')
    expect(normalizeBrowserUrl('https://user:secret@example.com/')).toBe(
      'https://example.com/',
    )
  })

  it('keeps raw URLs out of item identity', () => {
    const item = createBrowserWorkbenchItem({
      scopeId: 'agent:main:webchat:1',
      url: 'https://example.com/private?q=secret',
    })
    expect(item?.id).not.toContain('example.com')
    expect(item?.hostKind).toBe('native-webcontents')
    expect(item && browserUrlFromWorkbenchItem(item)).toBe(
      'https://example.com/private?q=secret',
    )
  })
})
