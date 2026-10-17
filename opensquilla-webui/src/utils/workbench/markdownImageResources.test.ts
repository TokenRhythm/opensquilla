// @vitest-environment happy-dom
import { describe, expect, it, vi } from 'vitest'
import type { WorkbenchResource } from '@/types/workbenchResources'
import { createMarkdownImageResolver } from './markdownImageResources'

function resource(name = 'docs/chart.svg', id = 'chart'): WorkbenchResource {
  return {
    resource: { type: 'deliverable', artifactId: id }, name, mime: 'image/svg+xml',
    downloadUrl: `/api/v1/artifacts/${id}`,
    capabilities: { preview: true, download: true, selectionContext: false, manualEdit: false, agentEdit: false, edit: false, publish: false },
    relations: {},
  }
}

describe('registered Markdown relative images', () => {
  it('loads one bounded session page lazily and matches exact document-relative names', async () => {
    const list = vi.fn(async () => ({ resources: [resource(), resource('logo.svg', 'logo')], totalCount: 2 }))
    const resolve = createMarkdownImageResolver({ list }, () => ({ name: 'docs/notes.md' }), () => 'session-a')
    const signal = new AbortController().signal
    expect(list).not.toHaveBeenCalled()
    expect(await resolve('chart.svg', signal)).toMatchObject({ id: 'chart', download_url: '/api/v1/artifacts/chart' })
    expect(await resolve('../logo.svg', signal)).toMatchObject({ id: 'logo' })
    expect(await resolve('missing.svg', signal)).toBeNull()
    expect(list).toHaveBeenCalledExactlyOnceWith('session-a', { types: ['deliverable'], limit: 200, signal, singlePage: true })
  })

  it('rejects ambiguous revisions instead of guessing by filename', async () => {
    const list = vi.fn(async () => ({ resources: [resource(), resource('docs/chart.svg', 'other')], totalCount: 2 }))
    const resolve = createMarkdownImageResolver({ list }, () => ({ name: 'docs/notes.md' }), () => 'session-a')
    expect(await resolve('chart.svg', new AbortController().signal)).toBeNull()
  })

  it.each([true, undefined])('retains alt for a partial catalog even with legacy hasMore=%s', async hasMore => {
    const list = vi.fn(async () => ({ resources: [resource()], totalCount: 500, hasMore, nextCursor: 'older' }))
    const resolve = createMarkdownImageResolver({ list }, () => ({ name: 'docs/notes.md' }), () => 'session-a')
    const signal = new AbortController().signal
    expect(await resolve('chart.svg', signal)).toBeNull()
    expect(await resolve('other.svg', signal)).toBeNull()
    expect(list).toHaveBeenCalledTimes(1)
  })

  it('does not access catalogs or local files for network/absolute/traversal paths', async () => {
    const list = vi.fn()
    const resolve = createMarkdownImageResolver({ list }, () => ({ name: 'notes.md' }), () => 'session-a')
    for (const source of ['file:///C:/secret.png', 'C:/secret.png', '/secret.png', '//evil/a.png', 'https://evil/a.png', '../secret.png', '%2e%2e/secret.png', '..\\secret.png']) {
      expect(await resolve(source, new AbortController().signal)).toBeNull()
    }
    expect(list).not.toHaveBeenCalled()
  })

  it('rejects stale resolution after cancellation and reloads on a new resource request', async () => {
    let finish!: (value: { resources: WorkbenchResource[]; totalCount: number }) => void
    const list = vi.fn(() => new Promise<{ resources: WorkbenchResource[]; totalCount: number }>(resolve => { finish = resolve }))
    const resolve = createMarkdownImageResolver({ list }, () => ({ name: 'docs/notes.md' }), () => 'session-a')
    const controller = new AbortController()
    const result = resolve('chart.svg', controller.signal)
    controller.abort()
    finish({ resources: [resource()], totalCount: 1 })
    expect(await result).toBeNull()
    const second = resolve('chart.svg', new AbortController().signal)
    finish({ resources: [resource()], totalCount: 1 })
    expect(await second).toMatchObject({ id: 'chart' })
    expect(list).toHaveBeenCalledTimes(2)
  })
})
