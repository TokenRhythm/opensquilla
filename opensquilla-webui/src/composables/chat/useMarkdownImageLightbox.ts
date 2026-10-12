import { nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useDialogA11y } from '@/composables/useDialogA11y'
import { isSafeMarkdownImageSource } from '@/utils/markdown/markdownUris'

// Public Markdown images already have a vetted URL. Their preview uses the
// app's existing dialog stack, without acquiring session or artifact access.
export function useMarkdownImageLightbox() {
  const { t, locale } = useI18n()
  const overlay = ref<HTMLElement | null>(null)
  const closeButton = ref<HTMLElement | null>(null)
  const isOpen = ref(false)
  const controls = new Map<HTMLImageElement, { refresh: () => void; dispose: () => void }>()
  let activeImage: HTMLImageElement | null = null
  let openingRequest = 0
  let disposed = false

  function imageSource(image: HTMLImageElement): string {
    const source = (image.getAttribute('src') || '').trim()
    return isSafeMarkdownImageSource(source) ? source : ''
  }

  function previewLabel(image: HTMLImageElement): string {
    return t('chat.previewOf', { title: image.alt || imageSource(image) })
  }

  function removePreview() {
    isOpen.value = false
    overlay.value?.remove()
    overlay.value = null
    closeButton.value = null
    activeImage = null
  }

  function close() {
    openingRequest += 1
    removePreview()
  }

  useDialogA11y(overlay, isOpen, close, { initialFocus: closeButton })

  function refreshPreviewLabels() {
    if (!overlay.value || !activeImage) return
    overlay.value.setAttribute('aria-label', previewLabel(activeImage))
    const label = t('chat.closePreview')
    closeButton.value?.setAttribute('aria-label', label)
    closeButton.value?.setAttribute('title', label)
  }

  async function activate(image: HTMLImageElement) {
    const source = imageSource(image)
    if (disposed || !source || !image.isConnected || !controls.has(image)) return
    const request = ++openingRequest
    removePreview()
    // Let the shared dialog watcher release a prior preview before reopening.
    // Reset/unmount can invalidate this pending activation without leaking DOM.
    await nextTick()
    if (disposed || request !== openingRequest || !image.isConnected
      || !controls.has(image) || imageSource(image) !== source) return

    const dialog = document.createElement('div')
    dialog.className = 'md-img-lightbox'
    dialog.setAttribute('role', 'dialog')
    dialog.setAttribute('aria-modal', 'true')

    const preview = document.createElement('img')
    preview.className = 'md-img-lightbox__image'
    preview.referrerPolicy = 'no-referrer'
    preview.decoding = 'async'
    preview.src = source
    preview.alt = image.alt
    dialog.appendChild(preview)

    const button = document.createElement('button')
    button.type = 'button'
    button.className = 'md-img-lightbox__close'
    button.textContent = '✕'
    button.addEventListener('click', event => {
      event.stopPropagation()
      close()
    })
    dialog.appendChild(button)
    dialog.addEventListener('click', event => {
      if (event.target === dialog) close()
    })

    // An image click does not naturally focus its trigger. Make the shared
    // dialog's invoker capture work for pointer and keyboard activation alike.
    image.focus({ preventScroll: true })
    document.body.appendChild(dialog)
    overlay.value = dialog
    closeButton.value = button
    activeImage = image
    refreshPreviewLabels()
    isOpen.value = true
  }

  function decorate(root: HTMLElement | null) {
    if (disposed) return
    if (!root?.isConnected) {
      reset()
      return
    }
    // Incremental streams pass only their newly committed roots. Existing
    // controls are released together on reset/dispose, not swept per append.
    if (activeImage && (!activeImage.isConnected || !imageSource(activeImage))) close()
    for (const image of root.querySelectorAll<HTMLImageElement>('img')) {
      if (controls.has(image) || !imageSource(image)) continue
      const original = new Map(['role', 'tabindex', 'aria-label', 'aria-haspopup', 'data-md-img-decorated']
        .map(name => [name, image.getAttribute(name)] as const))
      const hadClass = image.classList.contains('md-img')
      const onClick = (event: MouseEvent) => {
        event.preventDefault()
        event.stopPropagation()
        void activate(image)
      }
      const onKeydown = (event: KeyboardEvent) => {
        if (event.key !== 'Enter' && event.key !== ' ') return
        event.preventDefault()
        event.stopPropagation()
        void activate(image)
      }
      const control = {
        refresh() { image.setAttribute('aria-label', previewLabel(image)) },
        dispose() {
          image.removeEventListener('click', onClick)
          image.removeEventListener('keydown', onKeydown)
          for (const [name, value] of original) {
            if (value === null) image.removeAttribute(name)
            else image.setAttribute(name, value)
          }
          if (!hadClass) image.classList.remove('md-img')
        },
      }
      image.classList.add('md-img')
      image.dataset.mdImgDecorated = '1'
      image.setAttribute('role', 'button')
      image.setAttribute('tabindex', '0')
      image.setAttribute('aria-haspopup', 'dialog')
      image.addEventListener('click', onClick)
      image.addEventListener('keydown', onKeydown)
      control.refresh()
      controls.set(image, control)
    }
  }

  function reset() {
    close()
    for (const control of controls.values()) control.dispose()
    controls.clear()
  }

  function dispose() {
    reset()
    disposed = true
  }

  watch(locale, () => {
    for (const control of controls.values()) control.refresh()
    refreshPreviewLabels()
  })
  onBeforeUnmount(dispose)

  return { decorate, reset, dispose }
}
