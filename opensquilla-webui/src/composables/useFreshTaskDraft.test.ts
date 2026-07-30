import { describe, expect, it } from 'vitest'

import { useFreshTaskDraft } from './useFreshTaskDraft'

describe('useFreshTaskDraft', () => {
  it('emits a distinct request every time the same project asks for a new task', () => {
    const drafts = useFreshTaskDraft()

    drafts.requestFreshTask('main', 'project-a')
    const first = drafts.request.value
    drafts.requestFreshTask('main', 'project-a')
    const second = drafts.request.value

    expect(first).toMatchObject({ agentId: 'main', workspaceId: 'project-a' })
    expect(second).toMatchObject({ agentId: 'main', workspaceId: 'project-a' })
    expect(second?.id).toBeGreaterThan(first?.id || 0)
  })

  it('represents the default workspace explicitly as null', () => {
    const drafts = useFreshTaskDraft()

    drafts.requestFreshTask('main')

    expect(drafts.request.value?.workspaceId).toBeNull()
  })

  it('keeps a materialized project task bound until canonical metadata confirms it', () => {
    const drafts = useFreshTaskDraft()
    const sessionKey = 'agent:main:webchat:project-transition'

    drafts.bindMaterializedProjectTask(sessionKey, 'project-a')
    expect(drafts.materializedWorkspaceBySession.value[sessionKey]).toBe('project-a')

    drafts.confirmMaterializedProjectTask(sessionKey, null)
    expect(drafts.materializedWorkspaceBySession.value[sessionKey]).toBe('project-a')

    drafts.confirmMaterializedProjectTask(sessionKey, 'project-a')
    expect(drafts.materializedWorkspaceBySession.value[sessionKey]).toBeUndefined()
  })
})
