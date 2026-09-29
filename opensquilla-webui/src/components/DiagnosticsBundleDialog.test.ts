// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { App, Ref } from 'vue'
import { setAgentTraceEnabled } from '@/modules/agentTracePreference'

const messages = vi.hoisted(() => ({
  'monitorSupport.bundleTitle': 'Download redacted support bundle',
  'monitorSupport.bundleSubtitle': 'Create a ZIP to share with OpenSquilla support.',
  'monitorSupport.bundleDefaultIncludes': 'Included by default',
  'monitorSupport.bundleReadiness': 'Readiness and diagnostics snapshot',
  'monitorSupport.bundleConfig': 'Redacted configuration summary',
  'monitorSupport.bundleLogs': 'Errors, logs, and trace information',
  'monitorSupport.bundleLogsNoTrace': 'Errors and runtime logs',
  'monitorSupport.bundlePlatform': 'Version and platform information',
  'monitorSupport.bundleScopeTitle': 'Recent diagnostic records (up to 1 day)',
  'monitorSupport.bundleScopeBody': 'Error and trace records cover no more than one day; runtime logs follow local rotation, so actual coverage may differ.',
  'monitorSupport.bundleScopeBodyNoTrace': 'Error records cover no more than one day; runtime logs follow local rotation, so actual coverage may differ.',
  'monitorSupport.bundleIncludeContentTitle': 'Include conversation content',
  'monitorSupport.bundleIncludeContentBody': 'Enable only when support explicitly asks; this may contain sensitive business information.',
  'monitorSupport.bundleCredentialsTitle': 'Known credential fields are redacted',
  'monitorSupport.bundleCredentialsBody': 'Recognized API key, token, and secret values are redacted before packaging.',
  'monitorSupport.bundleCancel': 'Cancel',
  'monitorSupport.bundleConfirm': 'Generate and download',
  'common.close': 'Close',
} as Record<string, string>))

vi.mock('vue-i18n', () => ({
  useI18n: () => ({ t: (key: string) => messages[key] ?? key }),
}))

vi.mock('@/components/Icon.vue', async () => {
  const { defineComponent, h } = await import('vue')
  return {
    default: defineComponent({
      name: 'IconStub',
      props: { name: { type: String, default: '' } },
      setup(props) {
        return () => h('span', { 'data-icon': props.name })
      },
    }),
  }
})

const mountedApps: Array<{ app: App; el: HTMLElement }> = []

async function flush() {
  const { nextTick } = await import('vue')
  await nextTick()
  await nextTick()
}

async function mountDialog() {
  const { createApp, defineComponent, h, ref } = await import('vue')
  const Dialog = (await import('./DiagnosticsBundleDialog.vue')).default
  let open!: Ref<boolean>
  const confirms: Array<{ includeContent: boolean }> = []
  const Host = defineComponent({
    setup() {
      open = ref(true)
      return () => h(Dialog, {
        open: open.value,
        onClose: () => { open.value = false },
        onConfirm: (payload: { includeContent: boolean }) => { confirms.push(payload) },
      })
    },
  })
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(Host)
  app.mount(el)
  mountedApps.push({ app, el })
  await flush()
  return { open, confirms }
}

afterEach(() => {
  setAgentTraceEnabled(false)
  while (mountedApps.length) {
    const { app, el } = mountedApps.pop()!
    app.unmount()
    el.remove()
  }
  document.body.innerHTML = ''
  vi.restoreAllMocks()
})

describe('DiagnosticsBundleDialog', () => {
  it('shows the one-day scope as fixed guidance without a range selector', async () => {
    await mountDialog()
    const dialog = document.querySelector<HTMLElement>('[role="dialog"]')
    expect(dialog).toBeTruthy()
    expect(dialog?.textContent).toContain('Recent diagnostic records (up to 1 day)')
    expect(dialog?.textContent).toContain('Errors and runtime logs')
    expect(dialog?.textContent).toContain('Error records cover no more than one day; runtime logs follow local rotation, so actual coverage may differ.')
    expect(dialog?.textContent).not.toContain('trace information')
    expect(dialog?.textContent).not.toContain('Include conversation content')
    expect(dialog?.querySelector('select')).toBeNull()
    expect(dialog?.textContent).not.toContain('3 days')
    expect(document.activeElement?.textContent?.trim()).toBe('Cancel')
  })

  it('mentions traces only while the agent trace setting is enabled', async () => {
    await mountDialog()
    const dialog = document.querySelector<HTMLElement>('[role="dialog"]')
    setAgentTraceEnabled(true)
    await flush()
    expect(dialog?.textContent).toContain('Errors, logs, and trace information')
    expect(dialog?.textContent).toContain('Error and trace records cover no more than one day')
    expect(dialog?.textContent).toContain('Include conversation content')

    setAgentTraceEnabled(false)
    await flush()
    expect(dialog?.textContent).toContain('Errors and runtime logs')
    expect(dialog?.textContent).not.toContain('trace information')
    expect(dialog?.textContent).not.toContain('Include conversation content')
  })

  it('resets the conversation-content opt-in every time it opens', async () => {
    setAgentTraceEnabled(true)
    const { open } = await mountDialog()
    let checkbox = document.querySelector<HTMLInputElement>('[role="dialog"] input[type="checkbox"]')
    expect(checkbox?.checked).toBe(false)

    checkbox!.click()
    await flush()
    expect(checkbox?.checked).toBe(true)

    open.value = false
    await flush()
    open.value = true
    await flush()

    checkbox = document.querySelector<HTMLInputElement>('[role="dialog"] input[type="checkbox"]')
    expect(checkbox?.checked).toBe(false)
  })

  it('does not request conversation content when Trace is turned off after opt-in', async () => {
    setAgentTraceEnabled(true)
    const { confirms } = await mountDialog()
    document.querySelector<HTMLInputElement>('[role="dialog"] input[type="checkbox"]')!.click()
    await flush()

    setAgentTraceEnabled(false)
    await flush()
    expect(document.querySelector('[role="dialog"] input[type="checkbox"]')).toBeNull()
    document.querySelector<HTMLButtonElement>('[role="dialog"] .btn--primary')!.click()
    expect(confirms).toEqual([{ includeContent: false }])
  })
})
