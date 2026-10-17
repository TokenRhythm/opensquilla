import { describe, expect, it, vi } from 'vitest'
import type { SkillCatalog } from '@/modules/skillCatalog'
import { useSkillsCatalog } from './useSkillsCatalog'

describe('skills catalog loading states', () => {
  it('distinguishes an initial error from a successfully loaded empty catalog', async () => {
    let finish!: (value: unknown[]) => void
    const list = vi.fn().mockRejectedValueOnce(new Error('offline')).mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    const state = useSkillsCatalog({ list } as unknown as SkillCatalog)
    await state.loadData()
    expect(state.error.value).toBe('offline')
    expect(state.hasLoaded.value).toBe(false)
    expect(state.loading.value).toBe(false)
    const retry = state.loadData()
    expect(state.loading.value).toBe(true)
    expect(state.error.value).toBeNull()
    finish([])
    expect(await retry).toBe(true)
    expect(state.hasLoaded.value).toBe(true)
    expect(state.installedEmpty.value).toBe(true)
  })

  it('retains the last successful catalog when refresh fails', async () => {
    const list = vi.fn().mockResolvedValueOnce([{ name: 'saved', layer: 'personal' }]).mockRejectedValueOnce(new Error('offline'))
    const state = useSkillsCatalog({ list } as unknown as SkillCatalog)
    await state.loadData()
    expect(await state.loadData()).toBe(false)
    expect(state.hasLoaded.value).toBe(true)
    expect(state.allSkills.value.map(skill => skill.name)).toEqual(['saved'])
    expect(state.error.value).toBe('offline')
  })

  it('does not let an older request replace a newer result', async () => {
    let finish!: (value: unknown[]) => void
    const list = vi.fn().mockImplementationOnce(() => new Promise(resolve => { finish = resolve })).mockResolvedValueOnce([{ name: 'latest' }])
    const state = useSkillsCatalog({ list } as unknown as SkillCatalog)
    const older = state.loadData()
    await state.loadData()
    finish([{ name: 'stale' }])
    await older
    expect(state.allSkills.value.map(skill => skill.name)).toEqual(['latest'])
    expect(state.loading.value).toBe(false)
  })
})
