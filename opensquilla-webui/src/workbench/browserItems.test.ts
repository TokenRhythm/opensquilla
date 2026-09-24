import { describe, expect, it } from 'vitest'
import {
  browserUrlFromWorkbenchItem,
  createBrowserWorkbenchItem,
  normalizeBrowserAddress,
  normalizeBrowserUrl,
} from './browserItems'

describe('browser Workbench items', () => {
  it('accepts address-bar domains and loopback URLs without widening tool URL protocols', () => {
    expect(normalizeBrowserAddress('example.test/path')).toBe('https://example.test/path')
    expect(normalizeBrowserAddress('example.test:8080/path')).toBe('https://example.test:8080/path')
    expect(normalizeBrowserAddress('localhost:18807/case')).toBe('http://localhost:18807/case')
    expect(normalizeBrowserAddress('127.0.0.1:18807')).toBe('http://127.0.0.1:18807/')
    for (const value of ['javascript:alert(1)', 'file:///tmp/test', 'data:text/html,test', '', 'not a host']) {
      expect(normalizeBrowserAddress(value)).toBe('')
    }
    expect(normalizeBrowserUrl('example.test')).toBe('')
  })

  it('supports independent manually opened tabs for the same URL', () => {
    const options = { scopeId: 'session-a', url: 'https://example.test/' }
    const first = createBrowserWorkbenchItem({ ...options, instanceId: 'one' })!
    const second = createBrowserWorkbenchItem({ ...options, instanceId: 'two' })!
    expect(first.id).not.toBe(second.id)
    expect(first.scope).toEqual(second.scope)
  })

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
