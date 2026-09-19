import { describe, expect, it } from 'vitest'
import { useSetupBehaviorForm } from './useSetupBehaviorForm'

describe('useSetupBehaviorForm', () => {
  it('defaults auto session titles on when config omits naming', () => {
    const form = useSetupBehaviorForm()

    form.initFromConfig({})

    expect(form.autoSessionTitles.value).toBe(true)
    expect(form.isDirty.value).toBe(false)
    expect(form.patches()).toEqual({})
  })

  it('creates a safe naming.enabled patch when the title toggle changes', () => {
    const form = useSetupBehaviorForm()

    form.initFromConfig({ naming: { enabled: true } })
    form.setAutoSessionTitles(false)

    expect(form.autoSessionTitles.value).toBe(false)
    expect(form.isDirty.value).toBe(true)
    expect(form.patches()).toEqual({ 'naming.enabled': false })
  })

  it('defaults commit message drafting on when config omits it', () => {
    const form = useSetupBehaviorForm()

    form.initFromConfig({})

    expect(form.commitMessageEnabled.value).toBe(true)
    expect(form.isDirty.value).toBe(false)
    expect(form.patches()).toEqual({})
  })

  it('patches the commit-message switch on its own', () => {
    const form = useSetupBehaviorForm()

    form.initFromConfig({ commit_message: { enabled: true } })
    form.setCommitMessageEnabled(false)

    expect(form.isDirty.value).toBe(true)
    expect(form.patches()).toEqual({ 'commit_message.enabled': false })
  })

  it('defaults the commit message rule to empty when config omits it', () => {
    const form = useSetupBehaviorForm()

    form.initFromConfig({})

    expect(form.commitMessageInstructions.value).toBe('')
    expect(form.isDirty.value).toBe(false)
  })

  it('patches only the field that changed', () => {
    const form = useSetupBehaviorForm()

    form.initFromConfig({ naming: { enabled: true } })
    form.setCommitMessageInstructions('Use Conventional Commits prefixes.')

    expect(form.isDirty.value).toBe(true)
    expect(form.patches()).toEqual({
      'commit_message.instructions': 'Use Conventional Commits prefixes.',
    })
  })

  it('keeps every edit when a save follows more than one change', () => {
    const form = useSetupBehaviorForm()

    form.initFromConfig({ naming: { enabled: true } })
    form.setAutoSessionTitles(false)
    form.setCommitMessageEnabled(false)
    form.setCommitMessageInstructions('Say why, not what.')

    expect(form.patches()).toEqual({
      'naming.enabled': false,
      'commit_message.enabled': false,
      'commit_message.instructions': 'Say why, not what.',
    })
  })

  it('patches an emptied rule back to the built-in guidance', () => {
    const form = useSetupBehaviorForm()

    form.initFromConfig({ commit_message: { instructions: 'Old rule.' } })
    form.setCommitMessageInstructions('')

    expect(form.isDirty.value).toBe(true)
    expect(form.patches()).toEqual({ 'commit_message.instructions': '' })
  })

  it('resets dirtiness when reloaded from saved config', () => {
    const form = useSetupBehaviorForm()

    form.initFromConfig({ naming: { enabled: true } })
    form.setAutoSessionTitles(false)
    form.initFromConfig({ naming: { enabled: false } })

    expect(form.autoSessionTitles.value).toBe(false)
    expect(form.isDirty.value).toBe(false)
    expect(form.patches()).toEqual({})
  })

  it('resets the switch dirtiness when reloaded from saved config', () => {
    const form = useSetupBehaviorForm()

    form.initFromConfig({ commit_message: { enabled: true } })
    form.setCommitMessageEnabled(false)
    form.initFromConfig({ commit_message: { enabled: false } })

    expect(form.commitMessageEnabled.value).toBe(false)
    expect(form.isDirty.value).toBe(false)
    expect(form.patches()).toEqual({})
  })

  it('resets the rule dirtiness when reloaded from saved config', () => {
    const form = useSetupBehaviorForm()

    form.initFromConfig({ commit_message: { instructions: 'Draft.' } })
    form.setCommitMessageInstructions('Edited.')
    form.initFromConfig({ commit_message: { instructions: 'Edited.' } })

    expect(form.isDirty.value).toBe(false)
    expect(form.patches()).toEqual({})
  })
})
