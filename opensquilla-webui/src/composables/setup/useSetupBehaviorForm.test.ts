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

  it('resets dirtiness when reloaded from saved config', () => {
    const form = useSetupBehaviorForm()

    form.initFromConfig({ naming: { enabled: true } })
    form.setAutoSessionTitles(false)
    form.initFromConfig({ naming: { enabled: false } })

    expect(form.autoSessionTitles.value).toBe(false)
    expect(form.isDirty.value).toBe(false)
    expect(form.patches()).toEqual({})
  })

  it('accepts a saved value without overwriting a newer draft', () => {
    const form = useSetupBehaviorForm()
    form.initFromConfig({ naming: { enabled: false } })
    form.setAutoSessionTitles(true)
    form.setAutoSessionTitles(false)

    form.acceptSaved(true)

    expect(form.autoSessionTitles.value).toBe(false)
    expect(form.isDirty.value).toBe(true)
    expect(form.patches()).toEqual({ 'naming.enabled': false })

    form.acceptSaved(false)
    expect(form.isDirty.value).toBe(false)
    expect(form.patches()).toEqual({})
  })
})
