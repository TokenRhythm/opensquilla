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
    displayKind: 'path_access',
    displayTarget: '/workspace/report.md',
    destructive: false,
    irreversible: false,
    backupState: 'not_applicable',
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
  it('renders only the approved semantic target rather than raw sandbox args', async () => {
    const { app, root } = await mountCard(approval())
    const card = root.querySelector<HTMLElement>('.approval-card')
    const text = root.querySelector('.approval-card__context')?.textContent || ''
    expect(card?.dataset.approvalId).toBe('approval-1')
    expect(card?.tabIndex).toBe(-1)
    expect(text).toContain('/workspace/report.md')
    expect(root.textContent).not.toContain('sandbox path')
    expect(root.textContent).not.toContain('write')
    expect(root.querySelector('.approval-card__pre')).toBeNull()
    app.unmount()
  })

  it('presents destructive target and backup status as compact semantic rows', async () => {
    const { app, root } = await mountCard(approval({
      toolName: 'sandbox_elevation',
      args: { action_kind: 'fs.recursive_delete', internal_policy: true },
      displayKind: 'delete',
      displayTarget: '/workspace/archive',
      destructive: true,
      irreversible: false,
      backupState: 'enabled',
    }))

    expect(root.querySelector('.approval-card__target')?.textContent).toBe('/workspace/archive')
    expect(root.querySelector('.approval-card__risk-icon')).not.toBeNull()
    expect(root.querySelector('.approval-card__risk-copy')?.textContent).toContain('recoverable backup')
    expect(root.textContent).not.toContain('sandbox_elevation')
    expect(root.textContent).not.toContain('fs.recursive_delete')
    expect(root.textContent).not.toContain('internal_policy')
    app.unmount()
  })

  it('renders the public network target without dumping its argument object', async () => {
    const { app, root } = await mountCard(approval({
      approvalKind: 'sandbox_network',
      args: { host: 'packages.example.test', bundle_id: 'python-build', workspace: '/workspace' },
      displayKind: 'network_access',
      displayTarget: 'packages.example.test',
    }))
    const text = root.querySelector('.approval-card__context')?.textContent || ''
    expect(text).toContain('packages.example.test')
    expect(root.textContent).not.toContain('python-build')
    expect(root.textContent).not.toContain('/workspace')
    app.unmount()
  })

  it('renders the fingerprint-bound command when the legacy command field is empty', async () => {
    const exactCommand = "rm '/workspace/old report.txt'"
    const { app, root } = await mountCard(approval({
      toolName: 'exec_command',
      command: '',
      approvalKind: 'sandbox_elevation',
      args: { sandbox_permissions: 'require_escalated', internal_tool: 'legacy_escalate' },
      displayKind: 'run_command',
      displayTarget: exactCommand,
    }))

    expect(root.querySelector('.approval-card__pre--cmd')?.textContent).toBe(exactCommand)
    expect(root.textContent).not.toContain('legacy_escalate')
    app.unmount()
  })

  it('prefers the fingerprint-bound command over a stale legacy command', async () => {
    const exactCommand = "rm '/workspace/exact.txt'"
    const { app, root } = await mountCard(approval({
      command: 'legacy_escalation_tool --opaque-id 123',
      displayKind: 'run_command',
      displayTarget: exactCommand,
    }))

    expect(root.querySelector('.approval-card__pre--cmd')?.textContent).toBe(exactCommand)
    expect(root.textContent).not.toContain('legacy_escalation_tool')
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
