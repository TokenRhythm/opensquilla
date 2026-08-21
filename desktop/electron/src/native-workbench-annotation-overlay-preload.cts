import { ipcRenderer } from 'electron'

const OVERLAY_CHANNEL = 'opensquilla:workbench-annotation-overlay:init'
const BODY_MAX_BYTES = 16 * 1024

type OverlayPort = MessagePort

let port: OverlayPort | null = null
let pendingInitialBody = ''
let isComposing = false

interface OverlayCopy {
  targetLabel: string
  contextLabel: string
  bodyLabel: string
  placeholder: string
  newlineHint: string
  cancelLabel: string
  submitLabel: string
  emptyBodyMessage: string
}

let pendingCopy: OverlayCopy | null = null

function boundedBody(value: unknown): string | null {
  if (typeof value !== 'string') return null
  return new TextEncoder().encode(value).byteLength <= BODY_MAX_BYTES ? value : null
}

function send(message: Record<string, unknown>): void {
  try {
    port?.postMessage(message)
  } catch {}
}

const COPY_KEYS = [
  'targetLabel',
  'contextLabel',
  'bodyLabel',
  'placeholder',
  'newlineHint',
  'cancelLabel',
  'submitLabel',
  'emptyBodyMessage',
] as const

function boundedCopy(value: unknown): OverlayCopy | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const source = value as Record<string, unknown>
  if (Object.keys(source).some(key => !COPY_KEYS.includes(key as typeof COPY_KEYS[number]))) {
    return null
  }
  const entries = COPY_KEYS.map(key => {
    const raw = source[key]
    if (typeof raw !== 'string') return null
    const normalized = raw.replace(/[\u0000-\u001f\u007f]+/g, ' ').replace(/\s+/g, ' ').trim()
    return normalized && normalized.length <= 240 ? [key, normalized] as const : null
  })
  if (entries.some(entry => entry === null)) return null
  return Object.fromEntries(entries as Array<readonly [string, string]>) as unknown as OverlayCopy
}

function bindUi(initialBody: string, copy: OverlayCopy): void {
  const textarea = document.querySelector<HTMLTextAreaElement>('#annotation-body')
  const form = document.querySelector<HTMLFormElement>('#annotation-form')
  const cancel = document.querySelector<HTMLButtonElement>('#annotation-cancel')
  const submitButton = document.querySelector<HTMLButtonElement>('#annotation-submit')
  const target = document.querySelector<HTMLElement>('#annotation-target')
  const context = document.querySelector<HTMLElement>('#annotation-context')
  const newlineHint = document.querySelector<HTMLElement>('#annotation-newline-hint')
  if (!textarea || !form || !cancel || !submitButton || !target || !context || !newlineHint) {
    send({ version: 1, type: 'cancel' })
    return
  }
  // The trusted view is reused between annotations. A composition can be
  // interrupted by navigation, fencing, or an explicit close before Chromium
  // emits compositionend; never carry that IME state into the next editor.
  isComposing = false
  textarea.value = initialBody
  target.textContent = copy.targetLabel
  target.setAttribute('aria-label', copy.targetLabel)
  context.textContent = copy.contextLabel
  textarea.setAttribute('aria-label', copy.bodyLabel)
  textarea.placeholder = copy.placeholder
  newlineHint.textContent = copy.newlineHint
  newlineHint.title = copy.newlineHint
  cancel.textContent = copy.cancelLabel
  cancel.title = copy.cancelLabel
  submitButton.textContent = copy.submitLabel
  submitButton.title = copy.submitLabel
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
        textarea.setCustomValidity(
          pendingCopy?.emptyBodyMessage || copy.emptyBodyMessage,
        )
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
      } else if (event.key === 'Enter' && !event.shiftKey && !event.altKey) {
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
  const copy = boundedCopy(request?.copy)
  if (
    !request
    || request.version !== 1
    || initialBody === null
    || copy === null
    || Object.keys(request).some(key => !['version', 'initialBody', 'copy'].includes(key))
  ) return
  try {
    port?.close()
  } catch {}
  port = event.ports[0]!
  port.start()
  pendingInitialBody = initialBody
  pendingCopy = copy
  if (document.readyState === 'loading') {
    window.addEventListener(
      'DOMContentLoaded',
      () => {
        if (pendingCopy) bindUi(pendingInitialBody, pendingCopy)
      },
      { once: true },
    )
  } else {
    bindUi(pendingInitialBody, copy)
  }
})
