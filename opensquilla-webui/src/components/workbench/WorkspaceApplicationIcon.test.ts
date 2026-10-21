// @vitest-environment happy-dom
import { createApp, h } from 'vue'
import { afterEach, describe, expect, it } from 'vitest'
import WorkspaceApplicationIcon from './WorkspaceApplicationIcon.vue'
import vscode from '@/assets/workspace-apps/vscode.svg'
import sublime from '@/assets/workspace-apps/sublime.svg'

const disposers: Array<() => void> = []
afterEach(() => { disposers.splice(0).forEach(dispose => dispose()) })
function mount(appId: string) {
  const host = document.createElement('div'); document.body.append(host)
  const app = createApp({ render: () => h(WorkspaceApplicationIcon, { appId }) })
  app.mount(host)
  disposers.push(() => { app.unmount(); host.remove() })
  return host
}

describe('workspace application icons', () => {
  it.each<{ appId: string; source?: string }>([
    { appId: 'vscode', source: vscode }, { appId: 'cursor' }, { appId: 'windsurf' },
    { appId: 'sublime', source: sublime }, { appId: 'zed' },
  ])('maps $appId to its own bundled brand artwork', ({ appId, source }) => {
    const host = mount(appId)
    const wrapper = host.querySelector<HTMLElement>('.workspace-application-icon')!
    expect(wrapper.dataset.applicationIcon).toBe(appId)
    expect(wrapper.getAttribute('aria-hidden')).toBe('true')
    const image = wrapper.querySelector('img')
    const mask = wrapper.querySelector<HTMLElement>('.workspace-application-icon__monochrome')
    expect(wrapper.querySelector('.icon')).toBeNull()
    if (source) {
      expect(image?.getAttribute('src')).toBe(source)
      expect(source).not.toMatch(/^https?:/)
    } else {
      expect(mask?.classList.contains(`workspace-application-icon__monochrome--${appId}`)).toBe(true)
      expect(mask?.getAttribute('style')).toBeNull()
    }
  })

  it.each([
    ['reveal', 'folder'], ['default', 'externalLink'], ['unrecognized-editor', 'externalLink'],
    ['__proto__', 'externalLink'], ['constructor', 'externalLink'], ['https://untrusted.test/icon.svg', 'externalLink'],
  ])('uses a neutral built-in icon for %s without accepting dynamic image sources', (appId, expected) => {
    const host = mount(appId)
    expect(host.querySelector<HTMLElement>('.workspace-application-icon')?.dataset.applicationIcon).toBe(expected)
    expect(host.querySelector('svg')).not.toBeNull()
    expect(host.querySelector('img')).toBeNull()
    expect(host.querySelector('[style]')).toBeNull()
  })
})
