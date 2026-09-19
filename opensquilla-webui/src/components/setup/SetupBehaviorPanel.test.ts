// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick, type App } from 'vue'
import i18n from '@/i18n'
import SetupBehaviorPanel from './SetupBehaviorPanel.vue'

const mounted: App[] = []

function panel(overrides: Record<string, unknown> = {}) {
  return {
    autoSessionTitles: true,
    commitMessageEnabled: true,
    commitMessageInstructions: '',
    statusText: 'New sessions receive a short generated title.',
    ...overrides,
  }
}

async function mountPanel(panelValue = panel()) {
  const updateAutoSessionTitles = vi.fn()
  const updateCommitMessageEnabled = vi.fn()
  const updateCommitMessageInstructions = vi.fn()
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(SetupBehaviorPanel, {
    panel: panelValue,
    onUpdateAutoSessionTitles: updateAutoSessionTitles,
    onUpdateCommitMessageEnabled: updateCommitMessageEnabled,
    onUpdateCommitMessageInstructions: updateCommitMessageInstructions,
  })
  app.use(i18n)
  app.mount(el)
  mounted.push(app)
  await nextTick()
  return {
    el,
    updateAutoSessionTitles,
    updateCommitMessageEnabled,
    updateCommitMessageInstructions,
  }
}

afterEach(() => {
  while (mounted.length) mounted.pop()!.unmount()
  document.body.innerHTML = ''
})

describe('SetupBehaviorPanel', () => {
  it('shows the configured commit message rule and emits the edited value', async () => {
    i18n.global.locale.value = 'en'
    const { el, updateCommitMessageInstructions } = await mountPanel(
      panel({ commitMessageInstructions: 'Use Conventional Commits prefixes.' }),
    )
    const field = el.querySelector<HTMLTextAreaElement>(
      '[data-testid="setup-commit-message-rule"]',
    )

    expect(field?.value).toBe('Use Conventional Commits prefixes.')
    expect(field?.getAttribute('aria-label')).toBe('Commit message rule')
    // The rule is a paragraph, so the field has to be a textarea.
    expect(field?.tagName).toBe('TEXTAREA')

    field!.value = 'Say why, not what.'
    field!.dispatchEvent(new Event('input'))

    expect(updateCommitMessageInstructions).toHaveBeenCalledWith('Say why, not what.')
  })

  it('emits an emptied rule rather than treating it as no change', async () => {
    i18n.global.locale.value = 'en'
    const { el, updateCommitMessageInstructions } = await mountPanel(
      panel({ commitMessageInstructions: 'Old rule.' }),
    )
    const field = el.querySelector<HTMLTextAreaElement>(
      '[data-testid="setup-commit-message-rule"]',
    )

    field!.value = ''
    field!.dispatchEvent(new Event('input'))

    // Empty means "use the built-in guidance", which is a patch, not a no-op.
    expect(updateCommitMessageInstructions).toHaveBeenCalledWith('')
  })

  it('offers the drafting switch, and it emits on its own event', async () => {
    i18n.global.locale.value = 'en'
    const { el, updateCommitMessageEnabled, updateCommitMessageInstructions } = await mountPanel(
      panel({ commitMessageEnabled: true }),
    )
    const toggle = el.querySelector<HTMLInputElement>('[name="setup_commit_message_enabled"]')

    expect(toggle?.checked).toBe(true)
    expect(el.textContent).toContain('Draft commit messages')

    toggle!.checked = false
    toggle!.dispatchEvent(new Event('change'))

    expect(updateCommitMessageEnabled).toHaveBeenCalledWith(false)
    expect(updateCommitMessageInstructions).not.toHaveBeenCalled()
  })

  it('keeps the auto-title switch on its own event', async () => {
    i18n.global.locale.value = 'en'
    const { el, updateAutoSessionTitles, updateCommitMessageInstructions } = await mountPanel()
    const toggle = el.querySelector<HTMLInputElement>('[name="setup_auto_session_titles"]')

    toggle!.checked = false
    toggle!.dispatchEvent(new Event('change'))

    expect(updateAutoSessionTitles).toHaveBeenCalledWith(false)
    expect(updateCommitMessageInstructions).not.toHaveBeenCalled()
  })
})
