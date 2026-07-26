// @vitest-environment happy-dom
import { beforeEach, describe, expect, it } from 'vitest'
import { createApp, nextTick } from 'vue'
import i18n from '@/i18n'
import ApprovalCard from './ApprovalCard.vue'
import type { ChatApprovalItem, ChatApprovalResolution } from '@/composables/chat/useChatApprovals'

function approval(overrides: Partial<ChatApprovalItem> = {}): ChatApprovalItem {
  return {
    id: 'approval-1',
    namespace: 'exec',
    toolName: 'sandbox path',
    command: '',
    approvalKind: 'sandbox_path',
    args: { path: '/workspace/report.md', access: 'write', workspace: '/workspace' },
    warning: '',
    agent: 'main',
    sessionKey: 'agent:main:web',
    deadline: 0,
    ...overrides,
  }
}

async function mountCard(item: ChatApprovalItem, resolution: ChatApprovalResolution | null = null) {
  const root = document.createElement('div')
  document.body.appendChild(root)
  const app = createApp(ApprovalCard, { approval: item, resolution })
  app.use(i18n)
  app.mount(root)
  await nextTick()
  return { app, root }
}

beforeEach(() => {
  document.body.innerHTML = ''
  i18n.global.locale.value = 'en'
})

describe('ApprovalCard safe context', () => {
  it('renders dedicated sandbox target/access/workspace rows', async () => {
    const { app, root } = await mountCard(approval())
    const text = root.querySelector('.approval-card__context')?.textContent || ''
    expect(text).toContain('/workspace/report.md')
    expect(text).toContain('write')
    expect(text).toContain('/workspace')
    expect(root.querySelector('.approval-card__pre')).toBeNull()
    app.unmount()
  })

  it('keeps both the network host and bundle identity in the safe target row', async () => {
    const { app, root } = await mountCard(approval({
      approvalKind: 'sandbox_network',
      args: { host: 'packages.example.test', bundle_id: 'python-build', workspace: '/workspace' },
    }))
    const text = root.querySelector('.approval-card__context')?.textContent || ''
    expect(text).toContain('packages.example.test')
    expect(text).toContain('python-build')
    expect(text).toContain('/workspace')
    app.unmount()
  })

  it('folds a missing status into a neutral unavailable outcome', async () => {
    const { app, root } = await mountCard(approval(), 'unavailable')
    expect(root.querySelector('.approval-outcome--unavailable')).not.toBeNull()
    expect(root.querySelector('.approval-outcome')?.textContent).toContain('Approval no longer available')
    expect(root.querySelector('.approval-card')).toBeNull()
    app.unmount()
  })
})
