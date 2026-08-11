import { ipcRenderer } from 'electron'

const OVERLAY_CHANNEL = 'opensquilla:workbench-annotation-overlay:init'
const BODY_MAX_BYTES = 16 * 1024

type OverlayPort = MessagePort

let port: OverlayPort | null = null
let pendingInitialBody = ''
let pendingTagName = 'element'
let isComposing = false

function boundedBody(value: unknown): string | null {
  if (typeof value !== 'string') return null
  return new TextEncoder().encode(value).byteLength <= BODY_MAX_BYTES ? value : null
}

function send(message: Record<string, unknown>): void {
  try {
    port?.postMessage(message)
  } catch {}
}

function boundedTagName(value: unknown): string | null {
  return typeof value === 'string' && /^[a-z][a-z0-9._:-]{0,63}$/.test(value)
    ? value
    : null
}

function bindUi(initialBody: string, tagName: string): void {
  const textarea = document.querySelector<HTMLTextAreaElement>('#annotation-body')
  const form = document.querySelector<HTMLFormElement>('#annotation-form')
  const cancel = document.querySelector<HTMLButtonElement>('#annotation-cancel')
  const submitButton = document.querySelector<HTMLButtonElement>('#annotation-submit')
  const target = document.querySelector<HTMLElement>('#annotation-target')
  if (!textarea || !form || !cancel || !submitButton || !target) {
    send({ version: 1, type: 'cancel' })
    return
  }
  // The trusted view is reused between annotations. A composition can be
  // interrupted by navigation, fencing, or an explicit close before Chromium
  // emits compositionend; never carry that IME state into the next editor.
  isComposing = false
  textarea.value = initialBody
  target.textContent = `<${tagName}>`
  const updateSubmitState = () => {
    submitButton.disabled = !textarea.value.trim() || boundedBody(textarea.value) === null
  }
  updateSubmitState()
  if (textarea.dataset.annotationBound !== 'true') {
    textarea.dataset.annotationBound = 'true'
    textarea.addEventListener('compositionstart', () => {
      isComposing = true
    })
    textarea.addEventListener('compositionend', () => {
      isComposing = false
      textarea.setCustomValidity('')
      updateSubmitState()
      const body = boundedBody(textarea.value)
      if (body !== null) send({ version: 1, type: 'draft-changed', body })
    })
    textarea.addEventListener('input', (event) => {
      textarea.setCustomValidity('')
      updateSubmitState()
      if (isComposing || (event as InputEvent).isComposing) return
      const body = boundedBody(textarea.value)
      if (body !== null) send({ version: 1, type: 'draft-changed', body })
    })
    const submit = () => {
      const body = boundedBody(textarea.value)
      if (body === null) return
      if (!body.trim()) {
        textarea.setCustomValidity('请输入批注修改要求。')
        textarea.reportValidity()
        textarea.focus()
        return
      }
      textarea.setCustomValidity('')
      send({ version: 1, type: 'submit', body })
    }
    textarea.addEventListener('keydown', event => {
      if (event.isComposing || isComposing || event.keyCode === 229) return
      if (event.key === 'Escape') {
        event.preventDefault()
        send({ version: 1, type: 'cancel' })
      } else if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
        event.preventDefault()
        submit()
      }
    })
    form.addEventListener('submit', event => {
      event.preventDefault()
      submit()
    })
    cancel.addEventListener('click', () => send({ version: 1, type: 'cancel' }))
  }
  requestAnimationFrame(() => textarea.focus())
}

ipcRenderer.on(OVERLAY_CHANNEL, (event, payload: unknown) => {
  if (event.ports.length !== 1) return
  const request = payload && typeof payload === 'object'
    ? payload as Record<string, unknown>
    : null
  const initialBody = boundedBody(request?.initialBody)
  const tagName = boundedTagName(request?.tagName)
  if (
    !request
    || request.version !== 1
    || initialBody === null
    || tagName === null
    || Object.keys(request).some(key => !['version', 'initialBody', 'tagName'].includes(key))
  ) return
  try {
    port?.close()
  } catch {}
  port = event.ports[0]!
  port.start()
  pendingInitialBody = initialBody
  pendingTagName = tagName
  if (document.readyState === 'loading') {
    window.addEventListener(
      'DOMContentLoaded',
      () => bindUi(pendingInitialBody, pendingTagName),
      { once: true },
    )
  } else {
    bindUi(pendingInitialBody, pendingTagName)
  }
})
