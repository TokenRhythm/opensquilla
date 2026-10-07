// Click-to-zoom lightbox for markdown images rendered inside sanitized
// assistant text / artifact previews. A deliberately small DOM overlay (browsa
// pattern) instead of extending the artifact lightbox: markdown <img> sources
// are plain URLs, while ArtifactImageLightbox is coupled to session-scoped
// artifact/attachment blob transport — merging them would drag session
// plumbing into every chat surface for no benefit.
//
// The overlay is created on demand, appended to <body>, and removed on close.
// All styling lives in chat-markdown.css; colors come from theme tokens.

let activeLightbox: HTMLElement | null = null
let invokerToRestore: HTMLElement | null = null

export function closeImageLightbox(): void {
  activeLightbox?.remove()
  activeLightbox = null
  if (invokerToRestore && document.contains(invokerToRestore)) invokerToRestore.focus()
  invokerToRestore = null
}

function buildLightbox(src: string, alt: string, closeLabel: string): HTMLElement {
  const overlay = document.createElement('div')
  overlay.className = 'md-img-lightbox'
  overlay.setAttribute('role', 'dialog')
  overlay.setAttribute('aria-modal', 'true')
  overlay.setAttribute('aria-label', alt || src)

  const img = document.createElement('img')
  img.className = 'md-img-lightbox__image'
  img.src = src
  img.alt = alt
  overlay.appendChild(img)

  const close = document.createElement('button')
  close.type = 'button'
  close.className = 'md-img-lightbox__close'
  close.setAttribute('aria-label', closeLabel)
  close.title = closeLabel
  close.textContent = '✕'
  close.addEventListener('click', event => {
    event.stopPropagation()
    closeImageLightbox()
  })
  overlay.appendChild(close)

  overlay.addEventListener('click', event => {
    if (event.target === overlay) closeImageLightbox()
  })
  overlay.addEventListener('keydown', event => {
    if (event.key === 'Escape') {
      event.stopPropagation()
      closeImageLightbox()
    }
  })
  return overlay
}

export function openImageLightbox(src: string, alt: string, closeLabel: string): void {
  closeImageLightbox()
  invokerToRestore = document.activeElement instanceof HTMLElement ? document.activeElement : null
  const overlay = buildLightbox(src, alt, closeLabel)
  document.body.appendChild(overlay)
  activeLightbox = overlay
  overlay.querySelector<HTMLButtonElement>('.md-img-lightbox__close')?.focus()
}

// Binds click-to-zoom on every sanitized <img> under `root`. Idempotent (a
// `data-md-img-decorated` marker survives re-runs), and only adds listeners to
// images the sanitizer already vetted — http(s)/raster-data sources only.
export function decorateMarkdownImages(root: HTMLElement | null, closeLabel: string): void {
  if (!root) return
  for (const img of root.querySelectorAll<HTMLImageElement>('img')) {
    if (img.dataset.mdImgDecorated) continue
    img.dataset.mdImgDecorated = '1'
    img.classList.add('md-img')
    img.addEventListener('click', event => {
      event.preventDefault()
      event.stopPropagation()
      openImageLightbox(img.currentSrc || img.src, img.alt || '', closeLabel)
    })
  }
}
