import type { MetaSkillCatalog } from '@/modules/metaSkillCatalog'
import type { Skill } from '@/types/skills'

interface MetaSkillCatalogTransport {
  request<T = unknown>(method: string, params?: Record<string, unknown>): Promise<T>
}

interface MetaListResponse {
  skills?: Skill[]
}

/** Project MetaSkill list/detail operations behind the Gateway Adapter boundary. */
export function createV4MetaSkillCatalog(
  transport: MetaSkillCatalogTransport,
): MetaSkillCatalog {
  return {
    async list() {
      const response = await transport.request<MetaListResponse>('meta.list')
      return response.skills || []
    },
    inspect(name) {
      return transport.request<Skill>('meta.inspect', { name })
    },
  }
}
