import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

import desktopUpdateSource from '@/components/DesktopUpdateIndicator.vue?raw'
import chatHeaderSource from '@/components/chat/ChatHeaderActions.vue?raw'
import systemStatusSource from '@/components/chat/ChatSystemStatus.vue?raw'

const baseCssSource = readFileSync(new URL('../assets/base.css', import.meta.url), 'utf8')
const worldIds = ['ember', 'miami', 'vapor', 'synthwave', 'terminal'] as const
const worldSources = Object.fromEntries(worldIds.map(id => [
  id,
  readFileSync(new URL(`./${id}/world.css`, import.meta.url), 'utf8'),
])) as Record<(typeof worldIds)[number], string>

describe('chat topbar semantic state contract', () => {
  it('maps the only four severity values to shared presentation channels', () => {
    expect(baseCssSource).toContain('.topbar .topbar-state {')
    for (const state of ['normal', 'info', 'warning', 'danger']) {
      expect(baseCssSource).toContain(`.topbar .topbar-state[data-state="${state}"]`)
    }
    expect(baseCssSource).toContain('--topbar-state-channel')
    expect(baseCssSource).toContain('--topbar-state-fill')
    expect(baseCssSource).toContain('--topbar-state-border')
    expect(baseCssSource).toContain('.topbar-state--connection[data-state="normal"]')
    expect(baseCssSource).toContain('.topbar-state--system[data-state="normal"]')
  })

  it('keeps domain names in components while exposing severity through data-state', () => {
    expect(chatHeaderSource).toContain('topbar-state--deliverables')
    expect(chatHeaderSource).toContain('data-state="normal"')

    for (const domain of ['connection', 'approval', 'update', 'system']) {
      expect(systemStatusSource).toContain(`topbar-state--${domain}`)
    }
    expect(systemStatusSource).toContain(':data-state="connectionSeverity"')
    expect(systemStatusSource).toContain('data-state="danger"')
    expect(systemStatusSource).toContain(':data-state="updateSeverity"')
    expect(systemStatusSource).toContain(':data-state="severity"')
    expect(systemStatusSource).toContain('highestSystemSeverity([')

    expect(desktopUpdateSource).toContain('topbar-state--update')
    expect(desktopUpdateSource).toContain(':data-state="severity"')
  })

  it('limits world-theme emphasis to informational, warning, and danger states', () => {
    for (const id of worldIds) {
      const source = worldSources[id]
      const urgentSelector = source.match(
        /\.topbar \.topbar-state:is\([\s\S]*?\)\s*\{/,
      )?.[0]

      expect(urgentSelector, id).toBeDefined()
      expect(urgentSelector, id).toContain('[data-state="info"]')
      expect(urgentSelector, id).toContain('[data-state="warning"]')
      expect(urgentSelector, id).toContain('[data-state="danger"]')
      expect(urgentSelector, id).not.toContain('[data-state="normal"]')
      expect(source, id).toContain('.conn-pill:not(.topbar-state)')
      expect(source, id).toContain('var(--topbar-state-channel)')
    }
  })

  it('preserves state distinctions in forced colors and freezes themed state motion', () => {
    const reducedMotion = baseCssSource.slice(
      baseCssSource.indexOf('@media (prefers-reduced-motion: reduce)'),
      baseCssSource.indexOf('/* ── Forced colors'),
    )
    const forcedColors = baseCssSource.slice(
      baseCssSource.indexOf('@media (forced-colors: active)'),
    )

    expect(reducedMotion).toContain('.topbar .topbar-state')
    expect(reducedMotion).toContain('animation: none !important')
    expect(forcedColors).toContain('.topbar .topbar-state')
    expect(forcedColors).toContain('[data-state="info"] { border-style: dotted !important; }')
    expect(forcedColors).toContain('[data-state="warning"] { border-style: dashed !important; }')
    expect(forcedColors).toContain('[data-state="danger"] { border-width: 2px !important; }')
  })
})
