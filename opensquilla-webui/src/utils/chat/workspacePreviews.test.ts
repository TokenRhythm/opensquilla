// @vitest-environment happy-dom
import { describe, expect, it, vi } from 'vitest'
import {
  decorateWorkspacePreviewLinks,
  workspacePreviewForPath,
  workspacePreviewFromToolCall,
  workspacePreviewLabel,
  workspacePreviewIdentity,
  workspacePreviewPages,
  type WorkspacePreviewLink,
} from './workspacePreviews'
import type { WorkbenchResource } from '@/types/workbenchResources'

const site: WorkspacePreviewLink = {
  callId: 'call-site', documentId: 'doc_site', name: 'index.html',
  entrypoint: '/tasks/a/site/index.html', relativePath: 'site/index.html',
}
const other: WorkspacePreviewLink = {
  callId: 'call-other', documentId: 'doc_other', name: 'index.html',
  entrypoint: '/tasks/a/other/index.html', relativePath: 'other/index.html',
}

describe('registered workspace preview path aliases', () => {
  it.each(['index.html', 'site/index.html', './site/index.html', '/tasks/a/site/index.html'])
  ('matches the complete registered path %s', path => {
    expect(workspacePreviewForPath(path, [site])).toBe(site)
  })

  it.each(['open index.html', 'prefixsite/index.html', '../site/index.html', 'file:///tasks/a/site/index.html',
    'https://example.com/index.html', '~/site/index.html', 'index.html:12', 'index.html extra'])
  ('does not infer a resource from %s', path => {
    expect(workspacePreviewForPath(path, [site])).toBeUndefined()
  })

  it('requires unique basenames while retaining exact relative and absolute matches', () => {
    expect(workspacePreviewForPath('index.html', [site, other])).toBeUndefined()
    expect(workspacePreviewForPath('other/index.html', [site, other])).toBe(other)
    expect(workspacePreviewForPath(site.entrypoint, [site, other])).toBe(site)
    expect(workspacePreviewLabel(site, [site, other])).toBe('site/index.html')
    expect(workspacePreviewLabel(site, [site])).toBe('index.html')
  })

  it('allows an explicit workspace-root relative path without guessing a shared basename', () => {
    const root = { ...site, entrypoint: '/tasks/a/index.html', relativePath: 'index.html' }
    expect(workspacePreviewForPath('index.html', [root, other])).toBeUndefined()
    expect(workspacePreviewForPath('./index.html', [root, other])).toBe(root)
  })

  it('does not choose between conflicting documents with the same complete path', () => {
    const conflict = { ...site, documentId: 'doc_conflict' }
    expect(workspacePreviewForPath(site.entrypoint, [site, conflict])).toBeUndefined()
  })

  it('retains full source paths when no trustworthy workspace-relative alias exists', () => {
    const link = workspacePreviewFromToolCall({
      toolId: 'call-1', name: 'open_workspace_preview', isRunning: false, status: 'success', isError: false,
      result: JSON.stringify({
        documentId: 'doc_site', resourceId: 'document:doc_site', previewStatus: 'ready',
        entrypoint: '/tasks/ab/index.html', workspace: '/tasks/a',
      }),
    })!
    expect(link.relativePath).toBeUndefined()
    expect(workspacePreviewLabel(link, [link, other])).toBe('/tasks/ab/index.html')
  })

  it('supports canonical Windows separators without case folding or basename ambiguity', () => {
    const link = workspacePreviewFromToolCall({
      toolId: 'call-windows', name: 'open_workspace_preview', isRunning: false, status: 'success', isError: false,
      result: JSON.stringify({
        documentId: 'doc_windows', resourceId: 'document:doc_windows', previewStatus: 'ready',
        entrypoint: 'C:\\tasks\\site\\index.html', workspace: 'C:\\tasks\\',
      }),
    })!
    expect(link.relativePath).toBe('site/index.html')
    expect(workspacePreviewForPath('site\\index.html', [link])).toBe(link)
    expect(workspacePreviewForPath('C:/tasks/site/index.html', [link])).toBe(link)
    expect(workspacePreviewForPath('C:/tasks/SITE/index.html', [link])).toBeUndefined()
  })
})

describe('sanitized answer link decoration', () => {
  it('attaches right-click and keyboard menus only to trusted file identities', () => {
    const root = document.createElement('div')
    root.innerHTML = '<code>index.html</code><a href="https://example.com">external</a><code>unknown.html</code>'
    const menu = vi.fn()
    decorateWorkspacePreviewLinks(root, [site], vi.fn(), () => 'Open file', menu)
    const link = root.querySelector('.workspace-file-link')!
    link.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true }))
    link.dispatchEvent(new KeyboardEvent('keydown', { key: 'F10', shiftKey: true, bubbles: true }))
    expect(menu).toHaveBeenCalledTimes(2)
    expect(menu.mock.calls[0]![1]).toBe(site)
    const external = new MouseEvent('contextmenu', { cancelable: true, bubbles: true })
    root.querySelector('a')!.dispatchEvent(external)
    expect(external.defaultPrevented).toBe(false)
    expect(menu).toHaveBeenCalledTimes(2)
  })
  function fixture(html: string) {
    const root = document.createElement('div')
    root.innerHTML = html
    const onOpen = vi.fn()
    const decorate = (previews = [site]) => decorateWorkspacePreviewLinks(
      root, previews, onOpen, preview => `Open ${preview.name}`,
    )
    return { root, onOpen, decorate }
  }

  it('is idempotent and reconciles removed or newly ambiguous registrations', () => {
    const h = fixture('<p>Open <code>index.html</code>.</p>')
    expect(h.decorate()).toEqual([workspacePreviewIdentity(site)])
    expect(h.decorate()).toEqual([workspacePreviewIdentity(site)])
    expect(h.root.querySelectorAll('.workspace-file-link')).toHaveLength(1)
    h.root.querySelector<HTMLButtonElement>('.workspace-file-link')!.click()
    expect(h.onOpen).toHaveBeenCalledOnce()
    expect(h.decorate([site, other])).toEqual([])
    expect(h.root.querySelector('.workspace-file-link')).toBeNull()
    expect(h.root.querySelector('code')?.textContent).toBe('index.html')
    expect(h.decorate()).toEqual([workspacePreviewIdentity(site)])
    expect(h.decorate([])).toEqual([])
    expect(h.root.innerHTML).toBe('<p>Open <code>index.html</code>.</p>')
  })

  it('skips fenced code, existing actions, unknown aliases, and ordinary text', () => {
    const h = fixture('<pre><code>index.html</code></pre><a href="https://example.com"><code>index.html</code></a>'
      + '<button><code>index.html</code></button><code class="hljs">index.html</code>'
      + '<code>unknown.html</code><p>index.html</p>')
    expect(h.decorate()).toEqual([])
    expect(h.root.querySelector('.workspace-file-link')).toBeNull()
  })

  it('does not treat a forged decoration class or data attribute as an authorization', () => {
    const h = fixture('<button class="workspace-file-link" data-document-id="doc_forged"><code>unknown.html</code></button>')
    const before = h.root.innerHTML
    expect(h.decorate()).toEqual([])
    h.root.querySelector<HTMLButtonElement>('button')!.click()
    expect(h.onOpen).not.toHaveBeenCalled()
    expect(h.root.innerHTML).toBe(before)
  })
})

describe('server-enumerated site pages', () => {
  const registered = { ...site, bundleRoot: 'site' }
  const resource = {
    resource: { type: 'document', documentId: 'doc_site' },
    capabilities: { preview: true },
    previewPages: ['index.html', 'culture.html', 'food.html', 'places.html'],
  } as WorkbenchResource

  it('maps known pages to the original site without duplicating its entrypoint', () => {
    const links = workspacePreviewPages(registered, resource)
    expect(links).toHaveLength(4)
    expect(links[0]).toEqual(registered)
    expect(links[1]).toMatchObject({
      documentId: 'doc_site', previewPagePath: 'culture.html',
      relativePath: 'site/culture.html', entrypoint: '/tasks/a/site/culture.html',
    })
    expect(new Set(links.map(workspacePreviewIdentity)).size).toBe(4)
    expect(workspacePreviewForPath('site/food.html', links)?.previewPagePath).toBe('food.html')
    expect(workspacePreviewForPath('site/missing.html', links)).toBeUndefined()
  })

  it('rejects invalid pages and only allows unambiguous complete aliases', () => {
    const links = workspacePreviewPages(registered, { ...resource, previewPages: [
      'index.html', 'district/index.html', 'district/index.html', 'style.css', '../secret.html',
      '/escape.html', 'nested//bad.html', 'https://example.com/page.html', 'page.html?q=1',
      'bad\\name.html', '%2e%2e/secret.html', 'page.html#fragment',
    ] })
    expect(links).toHaveLength(2)
    expect(workspacePreviewForPath('index.html', links)).toBeUndefined()
    expect(workspacePreviewForPath('site/index.html', links)).toBe(links[0])
    expect(workspacePreviewForPath('site/district/index.html', links)).toBe(links[1])
  })

  it('requires the existing directory scope and the same authorized Document', () => {
    expect(workspacePreviewPages(site, resource)).toEqual([site])
    expect(workspacePreviewPages(registered, null)).toEqual([registered])
    expect(workspacePreviewPages(registered, { ...resource,
      resource: { type: 'document', documentId: 'doc_other' },
    })).toEqual([registered])
    expect(workspacePreviewPages(registered, { ...resource,
      capabilities: { ...resource.capabilities, preview: false },
    })).toEqual([registered])
  })

  it('does not trust a tool-authored page list or unsafe bundle root', () => {
    const parsed = workspacePreviewFromToolCall({ toolId: 'call-site', name: 'open_workspace_preview',
      isRunning: false, status: 'success', isError: false,
      result: JSON.stringify({ documentId: 'doc_site', resourceId: 'document:doc_site',
        previewStatus: 'ready', entrypoint: '/tasks/a/site/index.html', workspace: '/tasks/a',
        bundleMode: 'directory', bundleRoot: '../site', previewPages: ['secret.html'],
      }),
    })!
    expect(parsed.bundleRoot).toBeUndefined()
    expect(workspacePreviewPages(parsed, resource)).toEqual([parsed])
  })
})
