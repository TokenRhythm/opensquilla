import { describe, expect, it } from 'vitest'
import { normalizeWorkbenchResource } from '@/workbench/workbenchResourceProvider'
import { isPreviewPagePath, previewPagePathFromUrl } from './previewPagePath'

describe('preview page targets', () => {
  it.each(['editorial.html', 'pages/北京 页面.html', 'index.xhtml'])('accepts a logical page: %s', path => {
    expect(isPreviewPagePath(path)).toBe(true)
  })
  it.each(['', '/index.html', '../index.html', 'a/../index.html', './index.html', 'a//index.html',
    'https://site/index.html', 'a\\index.html', '%2e%2e/index.html', 'index.html#x',
    'index.html?q=1', ' index.html', 'secret.txt', 'bad\n.html'])('rejects unsafe targets: %s', path => {
    expect(isPreviewPagePath(path)).toBe(false)
  })
  it('preserves only valid document page metadata', () => {
    const resource = normalizeWorkbenchResource({ resource: { type: 'document', documentId: 'doc_a' },
      name: 'index.html', mime: 'text/html', previewPages: ['index.html', 'editorial.html',
        'editorial.html', '../secret.html', 'passwords.txt', null] })
    expect(resource?.previewPages).toEqual(['index.html', 'editorial.html'])
  })
  it('uses the actual navigated page and keeps remote authorization prefixes', () => {
    const lease = { launch_url: 'https://gateway.test/api/v1/artifact-preview/token/editorial.html',
      entrypoint: 'index.html', page_path: 'editorial.html' }
    expect(previewPagePathFromUrl('https://gateway.test/api/v1/artifact-preview/token/dashboard.html#title', lease))
      .toBe('dashboard.html')
    expect(previewPagePathFromUrl('https://gateway.test/api/v1/artifact-preview/other/dashboard.html', lease))
      .toBeUndefined()
    expect(previewPagePathFromUrl('https://foreign.test/api/v1/artifact-preview/token/dashboard.html', lease))
      .toBeUndefined()
  })
  it('retains the site root for a nested native launch and can navigate home', () => {
    const lease = { launch_url: 'http://p-token.localhost:1234/pages/editorial.html',
      entrypoint: 'index.html', page_path: 'pages/editorial.html' }
    expect(previewPagePathFromUrl('http://p-token.localhost:1234/dashboard.html', lease)).toBe('dashboard.html')
    expect(previewPagePathFromUrl('http://p-token.localhost:1234/', lease)).toBe('index.html')
  })
})
