// @vitest-environment happy-dom
import { createApp, h, nextTick, reactive } from 'vue'
import { createI18n } from 'vue-i18n'
import { afterEach, describe, expect, it } from 'vitest'
import ActivityCodeDetails from './ActivityCodeDetails.vue'
import type { ActivityToolCodeView } from '@/utils/chat/activityToolDetails'
import en from '@/locales/en.json'

const apps: ReturnType<typeof createApp>[] = []
function mount(view: ActivityToolCodeView, options: { full?: boolean; canOpenCodeView?: boolean; onCopy?: () => void; onDownload?: () => void; onReachEnd?: () => void } = {}) {
  const props = reactive({ view, ...options })
  const host = document.createElement('div')
  document.body.append(host)
  const app = createApp({ render: () => h(ActivityCodeDetails, props) })
  app.use(createI18n({ legacy: false, locale: 'en', messages: { en } }))
  app.mount(host)
  apps.push(app)
  return { host, props }
}
afterEach(() => { apps.splice(0).forEach(app => app.unmount()); document.body.innerHTML = '' })

describe('typed retained content', () => {
  it('labels copying a retained producer fragment as copying a preview', () => {
    const { host } = mount({ kind: 'command', command: 'build', output: 'saved fragment', partial: true })
    expect(host.querySelector('[data-testid="activity-tool-detail-copy"]')?.getAttribute('aria-label')).toBe(en.shared.runTrace.copyPreview)
    expect(host.querySelector('.activity-code-details__note')?.textContent).toBe(en.shared.runTrace.codePartial)
  })
  it('does not call an oversized partial producer record complete', () => {
    const { host } = mount({ kind: 'command', command: 'build', output: 'line\n'.repeat(2000), partial: true })
    expect(host.querySelector('[data-testid="activity-tool-detail-download"]')?.getAttribute('aria-label')).toBe(en.chat.download)
    const notes = host.querySelectorAll('.activity-code-details__note')
    expect(notes).toHaveLength(1)
    expect(notes[0]?.textContent).toBe(en.shared.runTrace.codePartial)
    expect(host.textContent).not.toContain(en.shared.runTrace.previewTruncated)
  })
  it('shares one byte and line budget across command input and output', () => {
    const part = `${'x'.repeat(600)}\n`.repeat(350)
    const { host } = mount({ kind: 'command', command: part, output: part })
    const code = Array.from(host.querySelectorAll('code'), element => element.textContent || '').join('')
    expect(new TextEncoder().encode(code).length).toBeLessThanOrEqual(256 * 1024 + 2)
    expect(host.querySelector('[data-testid="activity-tool-detail-download"]')).not.toBeNull()
  })
  it.each([false, true])('bounds huge diffs before creating lines even in full mode=%s', full => {
    const content = Array.from({ length: 10_000 }, (_, i) => `+line ${i}`).join('\n')
    const { host } = mount({ kind: 'edit', path: 'large.diff', content }, { full })
    expect(host.querySelectorAll('.activity-code-details__diff-line').length).toBeLessThanOrEqual(1001)
    expect(host.textContent).not.toContain('line 9999')
    expect(host.querySelector('[data-testid="tool-content-truncated"]')).not.toBeNull()
    expect(host.querySelector('[data-testid="activity-tool-detail-copy"]')).toBeNull()
  })

  it('bounds a huge single line and replaces copy with download without another disclosure', () => {
    let downloads = 0
    let copies = 0
    const { host } = mount({ kind: 'write', path: 'large.json', language: 'json', content: 'x'.repeat(2_000_000) },
      { onDownload: () => { downloads += 1 }, onCopy: () => { copies += 1 } })
    expect(host.querySelector('code')!.textContent!.length).toBe(8192)
    expect(host.querySelectorAll('button')).toHaveLength(1)
    host.querySelector<HTMLButtonElement>('[data-testid="activity-tool-detail-download"]')!.click()
    expect(downloads).toBe(1)
    expect(copies).toBe(0)
  })

  it('shows truncation from the server even when the retained preview is short', () => {
    const { host } = mount({ kind: 'write', path: 'a.txt', content: 'first line', truncated: true })
    expect(host.querySelector('[data-testid="activity-tool-detail-download"]')).not.toBeNull()
    expect(host.querySelector('[data-testid="tool-content-truncated"]')).not.toBeNull()
  })
  it('keeps the full long output in the scroll region without a second disclosure', () => {
    const output = Array.from({ length: 400 }, (_, i) => `line ${i} ${'x'.repeat(30)}`).join('\n')
    const { host } = mount({ kind: 'command', command: 'npm test', output, exitCode: 1 })
    expect(host.querySelector('.activity-code-details__output')?.textContent).toBe(output)
    expect(host.querySelector('.activity-code-details__exit')?.textContent).toBe('exit_code=1')
    expect(host.querySelectorAll('button')).toHaveLength(1)
    expect(host.querySelector('[data-testid="activity-code-open"]')).toBeNull()
  })

  it('does not render unavailable history markers as source or an empty card', () => {
    const { host } = mount({ kind: 'write', path: 'a.html', unavailable: true, content: '[historical_tool_argument_omitted]\nhead: <p>fragment' })
    expect(host.querySelector('section')).toBeNull()
  })

  it('shows a legitimate empty file without inventing diff lines', () => {
    const { host } = mount({ kind: 'write', path: 'empty.txt', content: '' })
    expect(host.textContent).toContain(en.shared.runTrace.codeEmptyFile)
    expect(host.querySelector('pre')).toBeNull()
  })

  it('keeps a no-output command as the command only', () => {
    const { host } = mount({ kind: 'command', command: 'pwd', output: '' })
    expect(host.querySelector('.activity-code-details__command')?.textContent).toBe('$ pwd')
    expect(host.querySelectorAll('p')).toHaveLength(0)
  })

  it('preserves diff text exactly and does not invent line numbers', () => {
    const content = '--- before\n+++ after\n@@\n-one\n+two\n'
    const { host } = mount({ kind: 'edit', path: 'a.ts', content })
    expect(host.querySelector('.activity-code-details__diff')?.textContent).toBe(content)
    expect(host.querySelectorAll('.activity-code-details__diff-line--added')).toHaveLength(1)
  })

  it('escapes executable-looking source even in the highlighted view', () => {
    const content = '<script>alert("x")</script><img src=x onerror="alert(1)">'
    const { host } = mount({ kind: 'write', path: 'a.html', language: 'html', content })
    expect(host.querySelector('code')?.textContent).toBe(content)
    expect(host.querySelector('script,img')).toBeNull()
  })

  it('keeps streaming changes reactive without adding another panel or truncation', async () => {
    const { host, props } = mount({ kind: 'command', command: 'npm run build', output: 'first\n' })
    props.view = { ...props.view, output: 'first\nlast\n' }
    await nextTick()
    expect(host.querySelector('.activity-code-details__output')?.textContent).toBe('first\nlast\n')
  })

  it('uses one inline file action and omits duplicate actions inside the workbench', () => {
    const file = { kind: 'read' as const, path: 'a.ts', content: 'const x = 1' }
    const { host } = mount(file, { canOpenCodeView: true })
    expect(host.querySelector('[data-testid="activity-code-open"]')?.textContent?.trim()).toBe(en.shared.runTrace.codeViewSource)
    const full = mount(file, { full: true, canOpenCodeView: true })
    expect(full.host.querySelector('button')).toBeNull()
    expect(full.host.querySelector('pre')?.textContent).toBe(file.content)
  })

  it('lets keyboard and wheel intent advance a short saved page without adding a button', () => {
    let requests = 0
    const { host } = mount({ kind: 'command', output: 'one line' }, { onReachEnd: () => { requests += 1 } })
    const viewport = host.querySelector<HTMLElement>('.activity-code-details__viewport')!
    Object.defineProperties(viewport, { clientHeight: { value: 24 }, scrollHeight: { value: 24 } })
    viewport.dispatchEvent(new WheelEvent('wheel', { deltaY: -1 }))
    expect(requests).toBe(0)
    viewport.dispatchEvent(new WheelEvent('wheel', { deltaY: 1 }))
    viewport.dispatchEvent(new KeyboardEvent('keydown', { key: 'PageDown' }))
    expect(requests).toBe(2)
    expect(host.querySelectorAll('button')).toHaveLength(1)
  })
})
