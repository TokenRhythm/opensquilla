// @vitest-environment happy-dom

import { describe, expect, it, vi } from 'vitest'
import type { ArtifactPayload } from '@/types/rpc'
import {
  ARTIFACT_HTML_OFFLINE_CSP,
  ARTIFACT_HTML_REFERENCE_SCAN_LIMIT,
  ARTIFACT_HTML_RELATIVE_RESOURCE_LIMIT,
  ARTIFACT_TEXT_PREVIEW_LIMIT,
  artifactPreviewLimit,
  artifactUsesWorkbenchPreview,
  artifactWorkbenchPreviewKind,
  buildOfflineArtifactHtml,
  ARTIFACT_PREVIEW_ESCAPE_MESSAGE,
  detectArtifactHtmlRelativeResources,
  renderArtifactMarkdown,
  responseMatchesArtifactPreviewKind,
} from './artifactPreview'

function artifact(overrides: Partial<ArtifactPayload>): ArtifactPayload {
  return {
    id: 'artifact-1',
    name: 'artifact.bin',
    mime: 'application/octet-stream',
    ...overrides,
  }
}

describe('artifactWorkbenchPreviewKind', () => {
  it.each([
    ['page.html', 'text/html', 'html'],
    ['page.HTML', 'application/octet-stream', 'html'],
    ['report.pdf', 'application/pdf', 'pdf'],
    ['notes.md', 'text/markdown', 'markdown'],
    ['server.log', 'application/octet-stream', 'text'],
    ['photo.png', 'image/png', 'image'],
    ['data.json', 'application/json', 'unsupported'],
  ])('classifies %s (%s) as %s', (name, mime, expected) => {
    expect(artifactWorkbenchPreviewKind(artifact({ name, mime }))).toBe(expected)
  })

  it('uses the smaller bounded limit for active text documents', () => {
    expect(artifactPreviewLimit('html')).toBe(ARTIFACT_TEXT_PREVIEW_LIMIT)
    expect(artifactPreviewLimit('markdown')).toBe(ARTIFACT_TEXT_PREVIEW_LIMIT)
    expect(artifactPreviewLimit('image')).toBeGreaterThan(ARTIFACT_TEXT_PREVIEW_LIMIT)
  })

  it('reserves Workbench navigation for document previews', () => {
    expect(artifactUsesWorkbenchPreview(artifact({
      name: 'page.html',
      mime: 'text/html',
    }))).toBe(true)
    expect(artifactUsesWorkbenchPreview(artifact({
      name: 'report.pdf',
      mime: 'application/pdf',
    }))).toBe(true)
    expect(artifactUsesWorkbenchPreview(artifact({
      name: 'poster.png',
      mime: 'image/png',
    }))).toBe(false)
    expect(artifactUsesWorkbenchPreview(artifact({
      name: 'slides.pptx',
      mime: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    }))).toBe(false)
  })

  it('rejects a response MIME that does not match the selected renderer', () => {
    expect(responseMatchesArtifactPreviewKind('image', 'text/html')).toBe(false)
    expect(responseMatchesArtifactPreviewKind('pdf', 'application/pdf; charset=binary')).toBe(true)
    expect(responseMatchesArtifactPreviewKind('html', 'application/octet-stream')).toBe(true)
  })
})

describe('artifact HTML isolation', () => {
  it('detects local document, stylesheet, image and CSS references', () => {
    const source = `
      <link rel="stylesheet" href="./style.css">
      <img src="assets/cover.png" srcset="assets/cover@2x.png 2x, https://cdn.invalid/x.png 3x">
      <a href="#section">Jump</a>
      <style>@import "theme.css"; .hero { background: url('/images/hero.png') }</style>
    `

    expect(detectArtifactHtmlRelativeResources(source)).toEqual([
      './style.css',
      '/images/hero.png',
      'assets/cover.png',
      'assets/cover@2x.png',
      'theme.css',
    ])
  })

  it('does not treat commas inside data URIs as missing resources', () => {
    const source = `
      <img src="data:image/png;base64,AA==">
      <img srcset="data:image/png;base64,AA== 1x, ./fallback.png 2x">
      <style>.icon { background: url(data:image/svg+xml,%3Csvg%3E%3C/svg%3E) }</style>
    `

    expect(detectArtifactHtmlRelativeResources(source)).toEqual(['./fallback.png'])
  })

  it('detects local module, fetch, worker and URL references', () => {
    const source = `
      <script type="module">
        import './app.js'
        import helper from "./helper.js"
        import("./lazy.js")
        export { value } from './shared.js'
        fetch("./data.json")
        new Worker('./worker.js')
        new SharedWorker("./shared-worker.js")
        new URL('./logo.png', import.meta.url)
        fetch("https://example.invalid/allowed-by-detector")
      </script>
    `

    expect(detectArtifactHtmlRelativeResources(source)).toEqual([
      './app.js',
      './data.json',
      './helper.js',
      './lazy.js',
      './logo.png',
      './shared-worker.js',
      './shared.js',
      './worker.js',
    ])
  })

  it('scans adversarial near-limit JavaScript in bounded linear time', { timeout: 15_000 }, () => {
    const inside = '\nfetch("./inside-budget.json")'
    const adversarialUnit = 'import unresolved '
    const fillerLength = ARTIFACT_HTML_REFERENCE_SCAN_LIMIT - inside.length
    const repeated = adversarialUnit.repeat(Math.floor(fillerLength / adversarialUnit.length))
    const filler = repeated + 'x'.repeat(fillerLength - repeated.length)
    const source = `${filler}${inside}fetch("./outside-budget.json")`

    const startedAt = performance.now()
    const resources = detectArtifactHtmlRelativeResources(source)
    const elapsed = performance.now() - startedAt

    expect(resources).toContain('./inside-budget.json')
    expect(resources).not.toContain('./outside-budget.json')
    // This is deliberately generous for shared/virtualized CI. It catches the
    // previous pathological scan without asserting normal sub-second timing.
    expect(elapsed).toBeLessThan(5_000)
  })

  it('bounds diagnostics for a near-limit document with unique references', { timeout: 15_000 }, () => {
    const makeTag = (index: number) =>
      `<img src="./resource-${index.toString(36).padStart(6, '0')}.png">`
    const tagLength = makeTag(0).length
    const tagCount = Math.ceil(ARTIFACT_HTML_REFERENCE_SCAN_LIMIT / tagLength)
    const source = Array.from({ length: tagCount }, (_, index) => makeTag(index))
      .join('')
      .slice(0, ARTIFACT_HTML_REFERENCE_SCAN_LIMIT)

    const startedAt = performance.now()
    const resources = detectArtifactHtmlRelativeResources(source)
    const elapsed = performance.now() - startedAt

    expect(source).toHaveLength(ARTIFACT_HTML_REFERENCE_SCAN_LIMIT)
    expect(resources).toHaveLength(ARTIFACT_HTML_RELATIVE_RESOURCE_LIMIT)
    expect(resources).toContain('./resource-000000.png')
    expect(resources).toContain(
      `./resource-${(ARTIFACT_HTML_RELATIVE_RESOURCE_LIMIT - 1)
        .toString(36)
        .padStart(6, '0')}.png`,
    )
    expect(elapsed).toBeLessThan(5_000)
  })

  it('places the offline CSP before untrusted markup without parsing it first', () => {
    const parse = vi.spyOn(DOMParser.prototype, 'parseFromString')
    const output = buildOfflineArtifactHtml(`
      <!doctype html>
      <html><head>
        <base href="https://example.invalid/">
        <meta http-equiv=" refresh " content="0; url=https://example.invalid/">
      </head><body>
        <img src="https://example.invalid/tracker.png">
        <script>document.body.dataset.ran = 'yes'</script>
      </body></html>
    `)

    expect(parse).not.toHaveBeenCalled()
    parse.mockRestore()
    expect(output.match(/<!doctype html>/gi)).toHaveLength(1)
    expect(output).toContain('http-equiv="Content-Security-Policy"')
    expect(output).toContain(ARTIFACT_HTML_OFFLINE_CSP)
    expect(output).toContain('<meta name="referrer" content="no-referrer">')
    expect(output.indexOf('Content-Security-Policy')).toBeLessThan(output.indexOf('<html>'))
    expect(ARTIFACT_HTML_OFFLINE_CSP).toContain("default-src 'none'")
    expect(ARTIFACT_HTML_OFFLINE_CSP).toContain("connect-src 'none'")
    expect(ARTIFACT_HTML_OFFLINE_CSP).toContain("frame-src 'none'")
    expect(ARTIFACT_HTML_OFFLINE_CSP).toContain("form-action 'none'")
    expect(output).not.toMatch(/<base\b/i)
    expect(output).not.toMatch(/http-equiv=" refresh "/i)
    expect(output).toContain('<script>document.body.dataset.ran')
    expect(output).toContain(ARTIFACT_PREVIEW_ESCAPE_MESSAGE)
  })
})

describe('artifact Markdown rendering', () => {
  it('keeps Markdown structure while stripping active content', () => {
    const output = renderArtifactMarkdown(`
# Notes

<script>window.pwned = true</script>

[unsafe](javascript:alert(1))
    `)

    expect(output).toContain('<h1>Notes</h1>')
    expect(output).not.toContain('<script')
    expect(output).not.toContain('javascript:')
  })
})
