// @vitest-environment happy-dom

import { createApp, h, nextTick, ref } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import i18n from '@/i18n'
import { useSkillsCatalog } from '@/composables/skills/useSkillsCatalog'
import type { useRpcStore } from '@/stores/rpc'
import SkillGroup from './SkillGroup.vue'

const apps: ReturnType<typeof createApp>[] = []

afterEach(() => {
  while (apps.length) apps.pop()?.unmount()
  document.body.innerHTML = ''
})

describe('SkillGroup lifecycle compatibility', () => {
  it('loads and renders an old Gateway skills.list response without lifecycle fields', async () => {
    const rpc = {
      waitForConnection: vi.fn(async () => {}),
      call: vi.fn(async () => ({
        skills: [{
          name: 'legacy-community-skill',
          description: 'Loaded from an older Gateway response',
          layer: 'managed',
          status: 'ready',
          eligible: true,
        }],
      })),
    } as unknown as ReturnType<typeof useRpcStore>
    const loadProposals = vi.fn(async () => {})
    const catalog = useSkillsCatalog(rpc, {
      proposals: ref([]),
      autoEnabledSkills: ref([]),
      proposalsSettings: ref({
        available: false,
        enabled: false,
        on_dream_complete: false,
        auto_enable: false,
        auto_enable_max_risk: 'low',
      }),
      loadProposals,
    })

    await expect(catalog.loadData()).resolves.toBe(true)
    expect(rpc.call).toHaveBeenCalledWith('skills.list', { includeLifecycle: true })
    expect(loadProposals).toHaveBeenCalledOnce()
    expect(catalog.allSkills.value).toHaveLength(1)
    expect(catalog.allSkills.value[0]?.lifecycle).toBeUndefined()

    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp({
      setup: () => () => h(SkillGroup, {
        title: 'Managed',
        description: 'Community skills',
        skills: catalog.allSkills.value,
      }),
    })
    app.use(i18n)
    app.mount(host)
    apps.push(app)
    await nextTick()

    expect(host.textContent).toContain('legacy-community-skill')
    expect(host.textContent).toContain('Loaded from an older Gateway response')
    expect(host.querySelector('.sk-tile__dot.is-ready')).not.toBeNull()
    expect(host.querySelector('.sk-tile__lifecycle')).toBeNull()
  })

  it('keeps healthy installed cards quiet and highlights only exceptional lifecycle states', async () => {
    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp({
      setup: () => () => h(SkillGroup, {
        title: 'Skills',
        description: 'Installed catalog',
        skills: [
          {
            name: 'bundled-ready',
            status: 'ready',
            lifecycle: {
              install_state: 'untracked',
              load_state: 'loaded',
              selection_state: 'active',
              compatibility_state: 'native',
              readiness_state: 'ready',
            },
          },
          {
            name: 'managed-ready',
            status: 'ready',
            lifecycle: {
              install_state: 'tracked',
              load_state: 'loaded',
              selection_state: 'active',
              compatibility_state: 'instruction_only',
              readiness_state: 'ready',
            },
          },
          {
            name: 'managed-setup',
            status: 'needs_setup',
            lifecycle: {
              install_state: 'tracked',
              load_state: 'loaded',
              selection_state: 'active',
              compatibility_state: 'instruction_only',
              readiness_state: 'needs_setup',
            },
          },
        ],
      }),
    })
    app.use(i18n)
    app.mount(host)
    apps.push(app)
    await nextTick()

    const tiles = [...host.querySelectorAll<HTMLElement>('.sk-tile')]
    const bundled = tiles.find(tile => tile.title.startsWith('bundled-ready'))
    const managed = tiles.find(tile => tile.title.startsWith('managed-ready'))
    const setup = tiles.find(tile => tile.title.startsWith('managed-setup'))

    expect(bundled?.querySelector('.sk-tile__lifecycle')).toBeNull()
    expect(managed?.querySelector('.sk-tile__lifecycle')).toBeNull()
    expect(setup?.querySelector('.sk-tile__lifecycle')?.textContent).toBe('Setup required')
    expect(setup?.querySelector('.sk-tile__lifecycle')?.getAttribute('data-tone')).toBe('warning')
  })
})
