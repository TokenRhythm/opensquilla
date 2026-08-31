import { describe, expect, it, vi } from 'vitest'

import { createV4MetaSkillCatalog } from './metaSkillCatalogV4'

describe('MetaSkill catalog Gateway Adapter', () => {
  it('owns MetaSkill wire method names and projects list/detail reads', async () => {
    const request = vi.fn(async (method: string) => (
      method === 'meta.list'
        ? { skills: [{ name: 'meta-paper-write', kind: 'meta' }] }
        : { name: 'meta-paper-write', kind: 'meta', dependencies: [] }
    )) as <T = unknown>(
      method: string,
      params?: Record<string, unknown>,
    ) => Promise<T>
    const catalog = createV4MetaSkillCatalog({ request })

    await expect(catalog.list()).resolves.toEqual([
      { name: 'meta-paper-write', kind: 'meta' },
    ])
    await expect(catalog.inspect('meta-paper-write')).resolves.toEqual({
      name: 'meta-paper-write',
      kind: 'meta',
      dependencies: [],
    })
    expect(request).toHaveBeenNthCalledWith(1, 'meta.list')
    expect(request).toHaveBeenNthCalledWith(2, 'meta.inspect', {
      name: 'meta-paper-write',
    })
  })
})
