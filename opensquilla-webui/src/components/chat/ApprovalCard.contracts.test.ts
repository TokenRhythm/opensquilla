// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest'
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

async function mountCard(
  item: ChatApprovalItem,
  resolution: ChatApprovalResolution | null = null,
  timeline = false,
  onExtend = vi.fn(),
) {
  const root = document.createElement('div')
  document.body.appendChild(root)
  const app = createApp(ApprovalCard, {
    approval: item,
    resolution,
    timeline,
    onExtend,
  })
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
    const card = root.querySelector<HTMLElement>('.approval-card')
    const text = root.querySelector('.approval-card__context')?.textContent || ''
    expect(card?.dataset.approvalId).toBe('approval-1')
    expect(card?.tabIndex).toBe(-1)
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

  it('keeps untimed human approvals free of countdown controls', async () => {
    const { app, root } = await mountCard(approval({ deadline: 0 }))
    expect(root.querySelector('.approval-card__timer')).toBeNull()
    expect(root.textContent).not.toContain('Expires in')
    app.unmount()
  })

  it('shows and extends an explicitly timed human approval', async () => {
    const extend = vi.fn()
    const { app, root } = await mountCard(approval({
      deadline: Date.now() / 1000 + 30,
    }), null, false, extend)
    expect(root.querySelector('.approval-card__timer')).not.toBeNull()
    expect(root.textContent).toContain('Expires in')
    root.querySelector<HTMLButtonElement>('.approval-card__extend')?.click()
    expect(extend).toHaveBeenCalledOnce()
    app.unmount()
  })

  it('folds a missing status into a neutral unavailable outcome', async () => {
    const { app, root } = await mountCard(approval(), 'unavailable')
    expect(root.querySelector('.approval-outcome--unavailable')).not.toBeNull()
    expect(root.querySelector('.approval-outcome')?.textContent).toContain('Approval no longer available')
    expect(root.querySelector('.approval-card')).toBeNull()
    app.unmount()
  })

  it('uses the compact in-flow treatment inside the work timeline', async () => {
    const { app, root } = await mountCard(approval(), 'approved', true)
    expect(root.querySelector('.approval-outcome--timeline')).not.toBeNull()
    app.unmount()
  })
})
