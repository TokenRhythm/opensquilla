// @vitest-environment happy-dom
import { afterEach, describe, expect, it } from 'vitest'
import { createApp, h, nextTick, reactive, type App } from 'vue'
import { compileStyle, parse } from 'vue/compiler-sfc'

import ActivityMotion from './ActivityMotion.vue'
import source from './ActivityMotion.vue?raw'

const mounted: App[] = []
const { descriptor } = parse(source)
const compiled = compileStyle({
  source: descriptor.styles[0]!.content,
  filename: 'ActivityMotion.vue',
  id: 'data-v-motion-test',
  scoped: true,
})

afterEach(() => {
  while (mounted.length) mounted.pop()?.unmount()
  document.body.innerHTML = ''
})

describe('ActivityMotion', () => {
  it('keeps one readable row and stops feedback as soon as execution ends', async () => {
    const state = reactive({ active: false })
    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp({
      render: () => h(ActivityMotion, { active: state.active }, {
        default: () => [h('span', 'Run command'), h('span', 'npm run build')],
      }),
    })
    mounted.push(app)
    app.mount(host)
    await nextTick()

    const row = host.querySelector('.activity-motion')
    expect(row?.classList.contains('activity-motion--active')).toBe(false)
    expect(row?.children).toHaveLength(2)
    expect(row?.textContent).toBe('Run commandnpm run build')

    state.active = true
    await nextTick()
    expect(row?.classList.contains('activity-motion--active')).toBe(true)
    expect(row?.children).toHaveLength(2)

    state.active = false
    await nextTick()
    expect(row?.classList.contains('activity-motion--active')).toBe(false)
    expect(row?.textContent).toBe('Run commandnpm run build')
  })

  it('preserves the hidden descendant selector after Vue scoped-style compilation', () => {
    expect(compiled.errors).toEqual([])
    // Vue drops following selector nodes if only the ancestor is wrapped in
    // :global(). Check emitted CSS so hidden child loops cannot regress.
    expect(compiled.code).toMatch(
      /\[data-share-expanded="false"\]\s+\[data-share-activity-body\]\s+\.activity-motion--active\s*\{\s*animation: none;\s*mask-image: none;/,
    )
  })

  it('removes both the animation and the dimming mask for reduced motion', () => {
    const mediaIndex = compiled.code.indexOf('@media (prefers-reduced-motion: reduce)')
    expect(mediaIndex).toBeGreaterThanOrEqual(0)
    expect(compiled.code.slice(mediaIndex)).toMatch(
      /\.activity-motion--active\[data-v-motion-test\]\s*\{\s*animation: none;\s*mask-image: none;/,
    )
  })
})
