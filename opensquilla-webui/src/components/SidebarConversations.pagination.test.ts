// @vitest-environment happy-dom

import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, defineComponent, h, nextTick, ref, type App } from 'vue'

import i18n from '@/i18n'
import type { SidebarSectionRow } from '@/composables/useSessions'
import SidebarConversations from './SidebarConversations.vue'

const mounted: App[] = []

function taskRow(key: string, agentId: string): SidebarSectionRow {
  return {
    rowKind: 'session',
    key,
    title: key,
    effectiveAgentId: agentId,
    agentName: agentId,
    sessionKind: 'chat',
    depth: 0,
    runStatus: 'idle',
    runLabel: 'Idle',
    taskAttention: 'none',
    updatedAt: 1,
    hasContractGaps: false,
  }
}

afterEach(() => {
  mounted.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
})

describe('SidebarConversations pagination', () => {
  it('keeps loading when an active agent filter has no match on the current page', async () => {
    i18n.global.locale.value = 'en'
    const rows = ref([taskRow('agent:research:webchat:old', 'research')])
    const hasMore = ref(false)
    const loadMore = vi.fn()
    const root = document.createElement('div')
    document.body.appendChild(root)
    const Root = defineComponent(() => () => h(SidebarConversations, {
      sections: [{ family: 'chats', label: 'Tasks', rows: rows.value }],
      error: false,
      loading: false,
      loadingMore: false,
      loadMoreError: false,
      hasMore: hasMore.value,
      currentKey: '',
      contractDebugEnabled: false,
      searchHint: 'Ctrl+K',
      onLoadMore: loadMore,
    }))
    const app = createApp(Root)
    app.use(i18n)
    app.mount(root)
    mounted.push(app)
    await nextTick()

    root.querySelector<HTMLButtonElement>('.sidebar-agent-badge')?.click()
    await nextTick()
    rows.value = [taskRow('agent:main:webchat:new', 'main')]
    hasMore.value = true
    await nextTick()
    await nextTick()

    expect(root.querySelector('.sidebar-history-list')).not.toBeNull()
    expect(root.textContent).not.toContain('No matches')
    expect(loadMore).toHaveBeenCalled()
  })
})
