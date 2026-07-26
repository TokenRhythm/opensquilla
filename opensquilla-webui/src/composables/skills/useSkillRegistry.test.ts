import { afterEach, describe, expect, it, vi } from 'vitest'
import { useSkillRegistry } from './useSkillRegistry'

const pushToast = vi.hoisted(() => vi.fn())

vi.mock('@/composables/useToasts', () => ({
  useToasts: () => ({ pushToast }),
}))

afterEach(() => {
  vi.restoreAllMocks()
  pushToast.mockClear()
})

describe('useSkillRegistry install state', () => {
  it('marks the matching community result installed after a successful install', async () => {
    const call = vi.fn(async (method: string) => {
      if (method === 'skills.install') {
        return { success: true, name: 'Development Coding Agent', message: 'installed' }
      }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const loadData = vi.fn(async () => true)
    const registry = useSkillRegistry({ call } as never, loadData)

    registry.registryResults.value = [
      {
        name: 'Development Coding Agent',
        description: 'Enhanced coding agent',
        identifier: 'development-coding-agent',
        source: 'clawhub',
        installed: false,
      },
      {
        name: 'Other Skill',
        identifier: 'other-skill',
        source: 'clawhub',
        installed: false,
      },
    ]

    await registry.installSkill('development-coding-agent', 'clawhub')

    expect(call).toHaveBeenCalledWith('skills.install', {
      identifier: 'development-coding-agent',
      source: 'clawhub',
    })
    expect(loadData).toHaveBeenCalledOnce()
    expect(registry.registryResults.value.map(result => result.installed)).toEqual([true, false])
    expect(registry.installingId.value).toBeNull()
  })

  it('warns when installation succeeds but the catalog list cannot refresh', async () => {
    const call = vi.fn(async () => ({
      success: true,
      name: 'Development Coding Agent',
      message: 'installed',
    }))
    const loadData = vi.fn(async () => false)
    const registry = useSkillRegistry({ call } as never, loadData)

    await registry.installSkill('development-coding-agent', 'clawhub')

    expect(pushToast).toHaveBeenCalledWith(expect.any(String), { tone: 'warn' })
  })

  it('treats an unresolved envAny group as an incomplete dependency install', async () => {
    const call = vi.fn(async () => ({
      success: true,
      message: 'binary installed',
      missing_still: {
        bins: [],
        env: [],
        env_any: [['OPENROUTER_API_KEY', 'ARK_API_KEY']],
      },
    }))
    const registry = useSkillRegistry({ call } as never, vi.fn(async () => true))

    const outcome = await registry.installDeps('audio-cog', 'ffmpeg')

    expect(outcome.success).toBe(true)
    expect(outcome.complete).toBe(false)
    expect(outcome.missingStill.env_any).toEqual([
      ['OPENROUTER_API_KEY', 'ARK_API_KEY'],
    ])
  })
})
