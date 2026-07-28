import { describe, expect, it } from 'vitest'

import appSource from './App.vue?raw'

describe('App sidebar chrome contract', () => {
  it('renders the OpenSquilla brand as a non-interactive lockup', () => {
    const brandStart = appSource.indexOf('<!-- Brand -->')
    const brandEnd = appSource.indexOf('<button', brandStart)
    const brandMarkup = appSource.slice(brandStart, brandEnd)

    expect(brandMarkup).toContain('<div class="sidebar-brand-lockup">')
    expect(brandMarkup).not.toContain('<router-link')
    expect(brandMarkup).not.toContain('@click')
    expect(brandMarkup).not.toContain('to="/overview"')
  })

  it('keeps project selection out of the primary sidebar controls', () => {
    const newTaskStart = appSource.indexOf('class="sidebar-new-session"')
    const workNavStart = appSource.indexOf('<router-link', newTaskStart)
    const primaryControls = appSource.slice(newTaskStart, workNavStart)

    expect(primaryControls).not.toContain('@click="openProjectPicker"')
    expect(primaryControls).not.toContain("t('workspaces.chooseProject')")
  })
})
