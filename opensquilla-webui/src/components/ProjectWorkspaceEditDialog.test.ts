// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, defineComponent, h, nextTick, ref, type App } from 'vue'
import { createI18n } from 'vue-i18n'
import ProjectWorkspaceEditDialog from './ProjectWorkspaceEditDialog.vue'

let app: App<Element> | undefined
afterEach(() => { app?.unmount(); document.body.innerHTML = '' })

describe('ProjectWorkspaceEditDialog', () => {
  it('locks the submitted name and repeat saves while allowing Cancel', async () => {
    const saving = ref(false)
    const save = vi.fn()
    const close = vi.fn()
    const host = document.createElement('div')
    document.body.appendChild(host)
    app = createApp(defineComponent(() => () => h(ProjectWorkspaceEditDialog, {
      open: true, initialName: 'Project A', path: '/a', saving: saving.value,
      onSave: save, onClose: close,
    })))
    app.use(createI18n({
      legacy: false, locale: 'en', messages: { en: {
        common: { cancel: 'Cancel', save: 'Save' },
        workspaces: { editProject: 'Edit project', projectName: 'Name', projectPath: 'Path' },
      } },
    }))
    app.mount(host)
    await nextTick()
    const input = document.querySelector<HTMLInputElement>('input[maxlength="120"]')!
    const submit = document.querySelector<HTMLButtonElement>('.btn--primary')!
    const cancel = document.querySelector<HTMLButtonElement>('.btn--ghost')!
    input.value = 'Renamed'
    input.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()
    submit.click()
    expect(save).toHaveBeenCalledExactlyOnceWith('Renamed')
    saving.value = true
    await nextTick()
    expect(input.disabled).toBe(true)
    expect(submit.disabled).toBe(true)
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    expect(save).toHaveBeenCalledTimes(1)
    cancel.click()
    expect(close).toHaveBeenCalledOnce()
    saving.value = false
    await nextTick()
    expect(input.disabled).toBe(false)
    expect(submit.disabled).toBe(false)
  })
})
