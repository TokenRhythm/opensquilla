// @vitest-environment happy-dom
import { beforeEach, describe, expect, it } from 'vitest'
import type { RouteLocationNormalized } from 'vue-router'
import { captureContentScroll, contentScrollBehavior } from './scrollMemory'

function routeAt(fullPath: string): RouteLocationNormalized {
  const path = fullPath.split('?')[0]
  return { path, fullPath } as RouteLocationNormalized
}

function mountContent(scrollTop: number): HTMLElement {
  const el = document.createElement('main')
  el.id = 'content'
  document.body.appendChild(el)
  el.scrollTop = scrollTop
  return el
}

async function frame(): Promise<void> {
  await new Promise(resolve => requestAnimationFrame(() => resolve(undefined)))
  await new Promise(resolve => setTimeout(resolve, 0))
}

beforeEach(() => {
  document.body.innerHTML = ''
})

describe('contentScrollBehavior', () => {
  it('leaves the scroll alone on a same-path query-only navigation', async () => {
    const el = mountContent(420)
    // A drill-in tab change is a query-only replace on /channels: the view
    // owns its section scrolling, so nothing may be scheduled here.
    const result = contentScrollBehavior(
      routeAt('/channels?channel=ops&tab=configuration'),
      routeAt('/channels?channel=ops&tab=pairings'),
      null,
    )
    expect(result).toBe(false)
    await frame()
    expect(el.scrollTop).toBe(420)
  })

  it('scrolls to top on a fresh cross-path navigation', async () => {
    const el = mountContent(420)
    const result = contentScrollBehavior(routeAt('/sessions'), routeAt('/channels'), null)
    expect(result).toBe(false)
    await frame()
    expect(el.scrollTop).toBe(0)
  })

  it('restores the captured offset on back/forward, including same-path pops', async () => {
    const el = mountContent(300)
    captureContentScroll(routeAt('/channels?channel=ops&tab=diagnostics'))
    el.scrollTop = 0
    contentScrollBehavior(
      routeAt('/channels?channel=ops&tab=diagnostics'),
      routeAt('/channels'),
      { left: 0, top: 0 },
    )
    await frame()
    expect(el.scrollTop).toBe(300)
  })
})
