import { ipcRenderer } from 'electron'

const OVERLAY_CHANNEL = 'opensquilla:workbench-annotation-overlay:init'
const BODY_MAX_BYTES = 16 * 1024

type OverlayPort = MessagePort

let port: OverlayPort | null = null
let pendingInitialBody = ''
let isComposing = false
let editorVersion: 1 | 2 = 1
let actionPending = false
let sizeFrame = 0
let lastHeight = 0
interface EditorState { batchCount: number; busy: boolean; error?: string; theme?: 'light' | 'dark' | 'system'; action?: 'record' | 'submit' }
let editorState: EditorState = { batchCount: 0, busy: false }
let refreshUi: (() => void) | null = null

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

function boundedState(value: unknown): EditorState | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const state = value as Record<string, unknown>
  if (Object.keys(state).some(key => !['batchCount', 'busy', 'error', 'theme', 'action'].includes(key))
    || !Number.isInteger(state.batchCount) || Number(state.batchCount) < 0 || Number(state.batchCount) > 16
    || typeof state.busy !== 'boolean'
    || (state.error !== undefined && (typeof state.error !== 'string' || state.error.length > 1000))
    || (state.theme !== undefined && !['light', 'dark', 'system'].includes(String(state.theme)))
    || (state.action !== undefined && state.action !== 'record' && state.action !== 'submit')) return null
  return state as unknown as EditorState
}

function reportSize(): void {
  if (editorVersion !== 2 || sizeFrame) return
  sizeFrame = requestAnimationFrame(() => {
    sizeFrame = 0
    const form = document.querySelector<HTMLElement>('#annotation-form')
    if (!form || editorVersion !== 2) return
    const height = Math.max(48, Math.min(320, Math.ceil(form.scrollHeight + 2)))
    if (height === lastHeight) return
    lastHeight = height
    send({ version: 2, type: 'content-size', height })
  })
}

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
  const error = document.querySelector<HTMLElement>('#annotation-error')
  if (!textarea || !form || !cancel || !submitButton || !target || !context || !newlineHint) {
    send({ version: 1, type: 'cancel' })
    return
  }
  // The trusted view is reused between annotations. A composition can be
  // interrupted by navigation, fencing, or an explicit close before Chromium
  // emits compositionend; never carry that IME state into the next editor.
  isComposing = false
  actionPending = false
  lastHeight = 0
  document.documentElement.dataset.editorVersion = String(editorVersion)
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
  submitButton.textContent = editorVersion === 2 ? '↑' : copy.submitLabel
  submitButton.title = copy.submitLabel
  const updateSubmitState = () => {
    const currentCopy = pendingCopy || copy
    const busy = editorVersion === 2 && (editorState.busy || actionPending)
    submitButton.disabled = !textarea.value.trim() || boundedBody(textarea.value) === null || busy || isComposing
    cancel.disabled = busy || isComposing
    textarea.readOnly = busy
    form.setAttribute('aria-busy', String(busy))
    if (editorVersion === 2) {
      document.documentElement.dataset.theme = editorState.theme || 'system'
      const expanded = Boolean(textarea.value || editorState.error || busy)
      form.dataset.expanded = String(expanded)
      textarea.style.height = '0px'
      textarea.style.height = `${Math.max(20, Math.min(180, textarea.scrollHeight))}px`
      submitButton.title = editorState.action === 'submit'
        ? `${currentCopy.submitLabel} (${editorState.batchCount})` : currentCopy.submitLabel
      submitButton.setAttribute('aria-label', submitButton.title)
      if (error) { error.textContent = editorState.error || ''; error.hidden = !editorState.error }
      reportSize()
    } else {
      delete document.documentElement.dataset.theme
      textarea.style.removeProperty('height')
      if (error) { error.textContent = ''; error.hidden = true }
    }
  }
  refreshUi = updateSubmitState
  updateSubmitState()
  if (textarea.dataset.annotationBound !== 'true') {
    textarea.dataset.annotationBound = 'true'
    textarea.addEventListener('compositionstart', () => {
      isComposing = true
      refreshUi?.()
    })
    textarea.addEventListener('compositionend', () => {
      isComposing = false
      textarea.setCustomValidity('')
      refreshUi?.()
      const body = boundedBody(textarea.value)
      if (body !== null) send({ version: editorVersion, type: 'draft-changed', body })
    })
    textarea.addEventListener('input', (event) => {
      textarea.setCustomValidity('')
      refreshUi?.()
      if (isComposing || (event as InputEvent).isComposing) return
      const body = boundedBody(textarea.value)
      if (body !== null) send({ version: editorVersion, type: 'draft-changed', body })
    })
    const submit = (action: 'record-current' | 'request-batch-submit') => {
      if (isComposing || (editorVersion === 2 && (editorState.busy || actionPending))) return
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
      send(editorVersion === 2 ? { version: 2, type: action, body } : { version: 1, type: 'submit', body })
      if (editorVersion === 2) { actionPending = true; refreshUi?.() }
    }
    const cancelCurrent = () => {
      if (isComposing || (editorVersion === 2 && (editorState.busy || actionPending))) return
      if (editorVersion === 2) {
        const body = boundedBody(textarea.value)
        if (body === null) return
        send({ version: 2, type: 'cancel-current', body })
        actionPending = true
        refreshUi?.()
      } else send({ version: 1, type: 'cancel' })
    }
    textarea.addEventListener('keydown', event => {
      if (event.isComposing || isComposing || event.keyCode === 229) return
      if (event.key === 'Escape') {
        event.preventDefault()
        cancelCurrent()
      } else if (event.key === 'Enter' && !event.shiftKey && !event.altKey
        && (editorVersion === 1 || (!event.ctrlKey && !event.metaKey))) {
        event.preventDefault()
        submit('record-current')
      }
    })
    form.addEventListener('submit', event => {
      event.preventDefault()
      submit(editorState.action === 'submit' ? 'request-batch-submit' : 'record-current')
    })
    cancel.addEventListener('click', cancelCurrent)
    window.addEventListener('resize', () => refreshUi?.())
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
  const state = request?.version === 2 ? boundedState(request.editorState) : null
  if (
    !request
    || (request.version !== 1 && request.version !== 2)
    || initialBody === null
    || copy === null
    || (request.version === 2 && !state)
    || Object.keys(request).some(key => !(request.version === 2
      ? ['version', 'initialBody', 'copy', 'editorState'] : ['version', 'initialBody', 'copy']).includes(key))
  ) return
  try {
    port?.close()
  } catch {}
  port = event.ports[0]!
  const boundPort = port
  editorVersion = request.version
  editorState = state || { batchCount: 0, busy: false }
  port.onmessage = event => {
    if (port !== boundPort || editorVersion !== 2) return
    const message = event.data
    if (!message || message.version !== 2 || message.type !== 'editor-state'
      || Object.keys(message).some(key => !['version', 'type', 'editorState'].includes(key))) return
    const next = boundedState(message.editorState)
    if (!next) return
    editorState = next
    actionPending = false
    refreshUi?.()
  }
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
