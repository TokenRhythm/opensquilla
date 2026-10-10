// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, reactive } from 'vue'
import { createI18n } from 'vue-i18n'
import en from '@/locales/en.json'
import type { WorkbenchComponentEvent } from '@/workbench/types'
import BrowserPreviewPanel from './BrowserPreviewPanel.vue'

const cleanups: Array<() => void> = []

afterEach(() => {
  for (const cleanup of cleanups.splice(0)) cleanup()
  vi.useRealTimers()
})

function mountPanel() {
  const props = reactive({
    currentUrl: 'https://example.test/',
    findOpen: false,
    findQuery: '',
    findMatches: null as number | null,
    findActiveMatch: 0,
    zoomFactor: 1,
    downloadId: '',
    downloadName: '',
    downloadState: '' as '' | 'progressing' | 'completed' | 'cancelled' | 'interrupted',
    downloadReceivedBytes: 0,
    downloadTotalBytes: 0,
    navigationCancelSequence: 0,
  })
  const onEvent = vi.fn((event: WorkbenchComponentEvent) => {
    if (event.type !== 'browser-action') return
    const payload = event.payload as Record<string, unknown>
    if (payload.action === 'find-open') props.findOpen = true
    if (payload.action === 'find-close') props.findOpen = false
    if (payload.action === 'find') props.findQuery = String(payload.query)
    if (payload.action === 'zoom') props.zoomFactor = Number(payload.zoomFactor)
  })
  const element = document.createElement('div')
  document.body.append(element)
  const app = createApp({ render: () => h(BrowserPreviewPanel, {
    ...props,
    'onWorkbench-event': onEvent,
  }) })
  app.use(createI18n({ legacy: false, locale: 'en', messages: { en } })).mount(element)
  cleanups.push(() => { app.unmount(); element.remove() })
  const button = (label: string) => element.querySelector<HTMLButtonElement>(`button[aria-label="${label}"]`)!
  return { element, props, onEvent, button }
}

it('opens page find from the keyboard, reports matches, navigates and closes', async () => {
  vi.useFakeTimers()
  const { element, props, onEvent, button } = mountPanel()
  const panel = element.querySelector<HTMLElement>('.browser-preview')!
  const shortcut = new KeyboardEvent('keydown', { key: 'f', ctrlKey: true,
    bubbles: true, cancelable: true })
  panel.dispatchEvent(shortcut)
  expect(shortcut.defaultPrevented).toBe(true)
  await nextTick()
  const input = element.querySelector<HTMLInputElement>('.browser-preview__find-input')!
  expect(document.activeElement).toBe(input)
  input.value = 'sample'
  input.dispatchEvent(new Event('input', { bubbles: true }))
  await nextTick()
  await vi.advanceTimersByTimeAsync(120)
  expect(onEvent).toHaveBeenCalledWith({ type: 'browser-action',
    payload: { action: 'find', query: 'sample' } })
  props.findMatches = 3
  props.findActiveMatch = 1
  await nextTick()
  expect(element.querySelector('.browser-preview__find-count')?.textContent).toBe('1/3')
  button('Next match').click()
  button('Previous match').click()
  expect(onEvent).toHaveBeenCalledWith({ type: 'browser-action',
    payload: { action: 'find-next', forward: true } })
  expect(onEvent).toHaveBeenCalledWith({ type: 'browser-action',
    payload: { action: 'find-next', forward: false } })
  input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }))
  await nextTick()
  expect(element.querySelector('.browser-preview__find-input')).toBeNull()
  expect(onEvent).toHaveBeenLastCalledWith({ type: 'browser-action',
    payload: { action: 'find-close' } })
})

it('changes only page zoom and resets to 100 percent', async () => {
  const { element, onEvent, button } = mountPanel()
  button('Zoom in').click()
  expect(onEvent).toHaveBeenLastCalledWith({ type: 'browser-action',
    payload: { action: 'zoom', zoomFactor: 1.1 } })
  await nextTick()
  expect(element.querySelector('.browser-preview__zoom-value')?.textContent).toBe('110%')
  button('Reset page zoom').click()
  expect(onEvent).toHaveBeenLastCalledWith({ type: 'browser-action',
    payload: { action: 'zoom', zoomFactor: 1 } })
  await nextTick()
  expect(element.querySelector('.browser-preview__zoom-value')?.textContent).toBe('100%')
})

it('restores the retained page address after a cancelled navigation', async () => {
  const { element, props, onEvent } = mountPanel()
  const address = element.querySelector<HTMLInputElement>('.browser-preview__address')!
  address.value = 'https://example.test/other'
  address.dispatchEvent(new Event('input', { bubbles: true }))
  element.querySelector<HTMLFormElement>('.browser-preview__toolbar')!.dispatchEvent(
    new Event('submit', { bubbles: true, cancelable: true }),
  )
  expect(onEvent).toHaveBeenLastCalledWith({ type: 'browser-action', payload: {
    action: 'navigate', url: 'https://example.test/other',
  } })
  props.navigationCancelSequence++
  await nextTick()
  expect(address.value).toBe(props.currentUrl)
})

it('shows the latest download progress and opens a completed download', async () => {
  const { element, props, onEvent, button } = mountPanel()
  props.downloadId = 'download-12345678-1234-1234-1234-123456789abc'
  props.downloadName = 'synthetic-note.txt'
  props.downloadState = 'progressing'
  props.downloadReceivedBytes = 25
  props.downloadTotalBytes = 100
  await nextTick()
  expect(element.querySelector('.browser-preview__download')?.textContent).toContain('25%')
  expect(element.querySelector('.browser-preview__download')?.textContent).toContain('synthetic-note.txt')
  expect(button('Open')).toBeNull()
  props.downloadState = 'completed'
  await nextTick()
  button('Open').click()
  expect(onEvent).toHaveBeenLastCalledWith({ type: 'browser-action', payload: {
    action: 'download-open', downloadId: props.downloadId,
  } })
  props.downloadState = 'cancelled'
  await nextTick()
  expect(button('Open')).toBeNull()
})
