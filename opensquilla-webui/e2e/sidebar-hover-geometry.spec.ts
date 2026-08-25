import { expect, test } from '@playwright/test'
import { readFileSync } from 'node:fs'

const sidebarCss = [
  new URL('../src/assets/base.css', import.meta.url),
  new URL('../src/styles/control-visual-system.css', import.meta.url),
  new URL('../src/styles/apple-modern.css', import.meta.url),
].map(url => readFileSync(url, 'utf8')).join('\n')

const longTitle = [
  '请执行一项 Deep Research 任务：分析 Fortinet 的长期经营表现、竞争格局与估值风险，',
  'including-an-intentionally-long-unbroken-segment-that-must-not-expand-the-sidebar-row',
].join('')

for (const sidebarWidth of [260, 420]) {
  for (const depth of [0, 2]) {
    test(`long title keeps a bounded hover anchor at ${sidebarWidth}px and depth ${depth}`, async ({
      page,
    }) => {
      await page.setViewportSize({ width: 1280, height: 900 })
      await page.setContent(`<!doctype html>
        <style>
          :root {
            --sp-1: 4px;
            --sp-2: 8px;
            --radius-md: 8px;
            --dur-fast: 0s;
            --sidebar-bg: #222;
            --sidebar-text: #ddd;
            --sidebar-text-strong: #fff;
            --sidebar-item-hover: #333;
            --font-sans: sans-serif;
          }
          ${sidebarCss}
          #app { --sidebar-width: ${sidebarWidth}px; }
        </style>
        <div id="app">
          <nav class="sidebar docked">
            <div class="sidebar-section sidebar-history">
              <div class="sidebar-history-list">
                <div class="sidebar-group">
                  <div class="sidebar-group__body">
                    <div class="sidebar-group__content">
                      <div
                        class="sidebar-history-row"
                        data-session-key="long-title"
                        style="--row-depth: ${depth}"
                      >
                        <button class="sidebar-history-item" type="button">
                          <span class="sidebar-history-title">${longTitle}</span>
                        </button>
                        <div class="sidebar-row-menu-wrap">
                          <button class="sidebar-row-menu-btn" type="button">⋯</button>
                        </div>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </nav>
        </div>
      `)

      const geometry = await page.evaluate(() => {
        const body = document.querySelector<HTMLElement>('.sidebar-group__body')!
        const content = document.querySelector<HTMLElement>('.sidebar-group__content')!
        const row = document.querySelector<HTMLElement>('.sidebar-history-row')!
        const title = document.querySelector<HTMLElement>('.sidebar-history-title')!
        const bodyRect = body.getBoundingClientRect()
        const contentRect = content.getBoundingClientRect()
        const rowRect = row.getBoundingClientRect()
        const preview = document.createElement('div')
        preview.className = 'sidebar-session-preview'
        preview.style.left = `${rowRect.right + 8}px`
        preview.style.top = `${rowRect.top}px`
        preview.textContent = 'Preview'
        document.body.appendChild(preview)
        const previewRect = preview.getBoundingClientRect()
        const titleStyle = getComputedStyle(title)

        return {
          bodyWidth: bodyRect.width,
          contentWidth: contentRect.width,
          bodyRight: bodyRect.right,
          rowRight: rowRect.right,
          previewGap: previewRect.left - rowRect.right,
          titleClientWidth: title.clientWidth,
          titleScrollWidth: title.scrollWidth,
          titleOverflow: titleStyle.overflow,
          titleTextOverflow: titleStyle.textOverflow,
          titleWhiteSpace: titleStyle.whiteSpace,
        }
      })

      expect(geometry.contentWidth).toBeLessThanOrEqual(geometry.bodyWidth + 1)
      expect(geometry.rowRight).toBeLessThanOrEqual(geometry.bodyRight + 1)
      expect(Math.abs(geometry.previewGap - 8)).toBeLessThanOrEqual(1)
      expect(geometry.titleScrollWidth).toBeGreaterThan(geometry.titleClientWidth)
      expect(geometry.titleOverflow).toBe('hidden')
      expect(geometry.titleTextOverflow).toBe('ellipsis')
      expect(geometry.titleWhiteSpace).toBe('nowrap')
    })
  }
}
