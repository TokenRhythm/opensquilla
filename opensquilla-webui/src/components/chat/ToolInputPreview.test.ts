// @vitest-environment happy-dom
import { afterEach, expect, it } from 'vitest'
import { createApp, h, nextTick, reactive, type App } from 'vue'
import i18n from '@/i18n'
import ToolInputPreview from './ToolInputPreview.vue'
import type { LiveToolInputPreview } from '@/types/toolInputPreview'

const apps: App[] = []
afterEach(() => { while (apps.length) apps.pop()?.unmount(); document.body.innerHTML = '' })
it.each([
  ['C:\\Users\\example\\project\\workspace\\live-preview.html', 'live-preview.html'],
  ['C:\\Users\\example\\project\\live-preview.html', '…/live-preview.html'],
  ['/home/example/project/workspace/src/page.html', 'src/page.html'],
  ['/home/example/project/page.html', '…/page.html'],
  ['src/pages/page.html', 'src/pages/page.html'],
])('shows the existing compact path for %s and retains its full tooltip', (path, label) => {
  const preview: LiveToolInputPreview = {
    previewId: 'p1', attemptId: 'a1', toolUseId: 't1', generationEpoch: 0, activityOrder: 3,
    revision: 1, state: 'generating', toolName: 'write_file', path, content: '<html>', truncated: false,
  }
  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp({ render: () => h(ToolInputPreview, { preview }) })
  apps.push(app); app.use(i18n); app.mount(host)
  const target = host.querySelector('.tool-input-preview__path')
  expect(target?.textContent).toBe(label)
  expect(target?.getAttribute('title')).toBe(path)
})
it('keeps draft HTML inert, mounts code only when expanded, and uses the eventual tool disclosure key', async () => {
  i18n.global.locale.value = 'en'
  const preview: LiveToolInputPreview = {
    previewId: 'p1', attemptId: 'a1', toolUseId: 't1', generationEpoch: 0, activityOrder: 3,
    revision: 1, state: 'generating', toolName: 'write_file', path: 'page.html',
    content: '<script>window.draftExecuted=true</script><button>unsafe</button>', truncated: false,
  }
  const state = reactive({ preview, open: false, executing: true })
  const choices: unknown[] = []
  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp({ render: () => h(ToolInputPreview, { ...state, stateScope: 'turn-1',
    onToggleItem: (id: string, open: boolean) => { choices.push([id, open]); state.open = open },
  }) })
  apps.push(app); app.use(i18n); app.mount(host)
  expect(host.querySelector('code')).toBeNull()
  expect(host.querySelector('.activity-motion--active')).not.toBeNull()
  host.querySelector<HTMLButtonElement>('button')!.click()
  await nextTick()
  expect(choices).toEqual([['turn-1:t1', true]])
  expect(host.querySelector('code')?.textContent).toBe(preview.content)
  expect(host.querySelector('script')).toBeNull()
  expect(host.querySelectorAll('button')).toHaveLength(1)
  expect(host.textContent).toContain('File has not been written yet')
  state.executing = false
  await nextTick()
  expect(host.querySelector('.activity-motion--active')).toBeNull()
  state.preview = { ...preview, revision: 2, content: '<html>real next content' }
  await nextTick()
  expect(host.querySelector('code')?.textContent).toBe('<html>real next content')
  host.querySelector<HTMLButtonElement>('button')!.click()
  await nextTick()
  expect(host.querySelector('code')).toBeNull()
})
