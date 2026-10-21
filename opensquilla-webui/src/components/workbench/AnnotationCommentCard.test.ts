// @vitest-environment happy-dom
import { createApp, h, nextTick, reactive } from 'vue'
import { createI18n } from 'vue-i18n'
import { afterEach, describe, expect, it, vi } from 'vitest'
import en from '@/locales/en.json'
import AnnotationCommentCard from './AnnotationCommentCard.vue'

afterEach(() => { document.body.innerHTML = '' })

function mount() {
  const node = document.createElement('div')
  document.body.append(node)
  const record = vi.fn(), submit = vi.fn(), cancel = vi.fn()
  const state = reactive({ body: '', label: 'Heading', count: 0, busy: false, stale: false, error: '' })
  const app = createApp({ render: () => h(AnnotationCommentCard, {
    ...state, onUpdate: (body: string) => { state.body = body },
    onRecord: record, onSubmit: submit, onCancel: cancel,
  }) })
  app.use(createI18n({ legacy: false, locale: 'en', messages: { en } }))
  app.mount(node)
  const input = node.querySelector('textarea')!
  return { node, input, state, record, submit, cancel, close: () => app.unmount() }
}

describe('AnnotationCommentCard', () => {
  it('records the current unsaved text even when draft count is zero and never sends from the right card', async () => {
    const view = mount()
    expect(view.node.querySelector('.annotation-comment__record')).toBeNull()
    view.input.value = 'Make the heading shorter'
    view.input.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()
    const arrow = view.node.querySelector<HTMLButtonElement>('.annotation-comment__record')!
    expect(arrow.disabled).toBe(false)
    arrow.click()
    expect(view.record).toHaveBeenCalledOnce()
    expect(view.submit).not.toHaveBeenCalled()
    view.input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }))
    expect(view.record).toHaveBeenCalledTimes(2)
    expect(view.submit).not.toHaveBeenCalled()
    view.close()
  })

  it('guards IME, multiline, stale state, busy state and UTF-8 body limits', async () => {
    const view = mount()
    view.state.body = '保留批注'
    await nextTick()
    view.input.dispatchEvent(new CompositionEvent('compositionstart'))
    view.input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }))
    expect(view.record).not.toHaveBeenCalled()
    view.input.dispatchEvent(new CompositionEvent('compositionend'))
    const newline = new KeyboardEvent('keydown', { key: 'Enter', shiftKey: true, bubbles: true, cancelable: true })
    view.input.dispatchEvent(newline)
    expect(newline.defaultPrevented).toBe(false)
    for (const change of [{ stale: true }, { stale: false, busy: true }, { busy: false, body: '中'.repeat(6000) }]) {
      Object.assign(view.state, change)
      await nextTick()
      expect(view.node.querySelector<HTMLButtonElement>('.annotation-comment__record')!.disabled).toBe(true)
      view.input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }))
    }
    expect(view.record).not.toHaveBeenCalled()
    expect(view.submit).not.toHaveBeenCalled()
    view.close()
  })
})
