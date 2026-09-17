// @vitest-environment happy-dom
import { afterEach, describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { createApp, h, type App } from 'vue'
import SidebarSessionHoverCard, {
  fitSessionPreviewPosition,
  sessionPreviewPosition,
} from './SidebarSessionHoverCard.vue'

const mountedApps: App<Element>[] = []
const baseCss = readFileSync('src/assets/base.css', 'utf8')

function mountCard(props: {
  title: string
  updatedAt: number
  projectName?: string
}) {
  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp({
    render: () => h(SidebarSessionHoverCard, {
      ...props,
      position: { left: '12px', top: '12px' },
    }),
  })
  app.mount(host)
  mountedApps.push(app)
  return host
}

afterEach(() => {
  mountedApps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
})

describe('SidebarSessionHoverCard', () => {
  it('shows the full title, relative time, and resolved project name', () => {
    const host = mountCard({
      title: 'Investigate interrupted task',
      updatedAt: Date.now() - 2 * 60 * 60 * 1000,
      projectName: 'opensquilla',
    })

    expect(host.querySelector('.sidebar-session-preview__title')?.textContent)
      .toBe('Investigate interrupted task')
    expect(host.querySelector('.sidebar-session-preview__time')?.textContent).not.toBe('')
    expect(host.querySelector('[data-testid="sidebar-session-project"]')?.textContent)
      .toContain('opensquilla')
  })

  it('uses ordinary text with the same typography as a session title', () => {
    const host = mountCard({
      title: 'Typography check',
      updatedAt: Date.now(),
    })

    expect(host.querySelector('.sidebar-session-preview__title')?.tagName).toBe('SPAN')
    const sharedTypographyRule = baseCss.match(
      /\.sidebar-history-title,\s*\.sidebar-session-preview__title\s*\{([^}]*)\}/,
    )?.[1]
    expect(sharedTypographyRule).toBeDefined()
    expect(sharedTypographyRule).toContain('font-family: var(--font-sans)')
    expect(sharedTypographyRule).toContain('font-size: 0.828125rem')
    expect(sharedTypographyRule).toContain('font-weight: 500')
    expect(sharedTypographyRule).toContain('line-height: 1.35')
  })

  it('omits only the project row for an unbound session', () => {
    const host = mountCard({
      title: 'General task',
      updatedAt: Date.now() - 60_000,
      projectName: '',
    })

    expect(host.querySelector('.sidebar-session-preview__title')?.textContent)
      .toBe('General task')
    expect(host.querySelector('.sidebar-session-preview__time')?.textContent).not.toBe('')
    expect(host.querySelector('[data-testid="sidebar-session-project"]')).toBeNull()
  })

  it('flips left and clamps vertically inside a tight viewport', () => {
    expect(sessionPreviewPosition(
      { left: 900, right: 980, top: 740 },
      { width: 1024, height: 768 },
    )).toEqual({ left: '620px', top: '652px' })
  })

  it('places the card to the right when space permits', () => {
    expect(sessionPreviewPosition(
      { left: 20, right: 300, top: 40 },
      { width: 1024, height: 768 },
    )).toEqual({ left: '308px', top: '40px' })
  })

  it('repositions a measured tall card inside the viewport', () => {
    expect(fitSessionPreviewPosition(
      { left: '620px', top: '652px' },
      { width: 272, height: 220 },
      { width: 1024, height: 768 },
    )).toEqual({ left: '620px', top: '536px' })
  })

  it('defines peer-zone and preview styles without the old pseudo heading', () => {
    expect(baseCss).toContain('.sidebar-zone-heading {')
    expect(baseCss).toContain('.sidebar-session-preview {')
    expect(baseCss).not.toContain('.sidebar-history-row--recent-start::before')
  })

  it('does not reserve project-row height when no project is rendered', () => {
    const previewRule = baseCss.match(/\.sidebar-session-preview\s*\{([^}]*)\}/)?.[1]
    expect(previewRule).toBeDefined()
    expect(previewRule).not.toContain('min-height')
  })
})
