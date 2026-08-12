import type { Ref } from 'vue'
import i18n from '@/i18n'
import { downloadBlob } from '@/utils/browser'

export type ShareExportTheme = 'light' | 'dark'

interface ChatShareExportOptions {
  threadRef: Ref<HTMLElement | null>
  /** Raw conversation title. The composable does all slugging and filename
      composition (CJK-safe) — callers must NOT pre-sanitize or pre-compose. */
  title: () => string
  /** Localized provenance label preserved in every generated share image. */
  aiGeneratedLabel: () => string
}

export interface ShareImageResult {
  blob: Blob
  filename: string
  width: number
  height: number
}

const EXPORT_WIDTH = 704
const MAX_EXPORT_HEIGHT = 24000
const SHARE_TEMPLATE_WIDTH = 760
const SHARE_TEMPLATE_MARGIN = 28
const SHARE_TEMPLATE_TOP = 24
const SHARE_TEMPLATE_BRAND_HEIGHT = 24
const SHARE_TEMPLATE_BRAND_GAP = 14
const SHARE_TEMPLATE_FOOTER_HEIGHT = 96
const SHARE_TEMPLATE_QR_SIZE = 64
const EXPORT_SCALE = 2
const SHARE_EXPORT_BOTTOM_SAFE_AREA = 32
const SHARE_EXPORT_USER_BUBBLE_RIGHT_OFFSET = 10

const SHARE_FILENAME_PREFIX = 'opensquilla'
const SHARE_FILENAME_FALLBACK = 'chat'
const SHARE_SLUG_MAX_LENGTH = 40
const SHARE_FOOTER_CAPTION = 'opensquilla.ai'

const SHARE_STAGE_ID = 'opensquilla-share-export-stage'

// Interactive UI remnants that must never appear in the static share image.
// Keep this list class-based and specific: generic selectors (e.g. bare
// input[type=checkbox]) would also strip rendered markdown task lists.
const SHARE_CLONE_STRIP_SELECTORS = [
  '.chat-share-picker',
  '.msg-user-actions',
  '.msg-ai-actions',
  '.share-select-check',
  '.share-select-checkbox',
  '.chat-share-checkbox',
  '[data-share-checkbox]',
  '[data-share-control]',
  '.turn-usage-details',
  '[data-turn-usage-details]',
  '.msg-meta__more',
  '.msg-meta__cost',
  '.step-view-btn',
  '.msg-artifact-actions',
  '.msg-artifact-download',
  '[data-tooltip]',
  '[role="tooltip"]',
]

export function useChatShareExport(options: ChatShareExportOptions) {
  // Build the share PNG without delivering it: callers get the blob (plus the
  // composed filename and pixel size) and decide whether to preview, copy, or
  // download. `theme` forces the export's token theme independently of the live
  // app theme, defaulting to light for legibility on social surfaces.
  async function buildShareImage(
    selectedIds: Set<string>,
    opts: { theme?: ShareExportTheme } = {},
  ): Promise<ShareImageResult | null> {
    const theme: ShareExportTheme = opts.theme ?? 'light'
    if (selectedIds.size === 0) {
      console.warn('Share export skipped: no messages selected')
      return null
    }
    const sourceElements = selectedShareElements(options.threadRef.value, selectedIds)
    if (sourceElements.length === 0) {
      console.warn('Share export skipped: selected messages not found in thread')
      return null
    }

    await document.fonts?.ready
    const aiGeneratedLabel = options.aiGeneratedLabel()
    const stage = buildShareDom(sourceElements, theme, aiGeneratedLabel)

    try {
      document.body.appendChild(stage)
      await waitForStablePaint()
      const contentCanvas = await captureStageWithDom(stage)
      const { blob, width, height } = await composeShareTemplate(
        contentCanvas,
        stage,
        aiGeneratedLabel,
      )
      const filename = shareExportFilename(options.title())
      return { blob, filename, width, height }
    } finally {
      stage.remove()
    }
  }

  // Thin build + immediate download, kept for non-preview callers.
  async function exportSelectedMessages(
    selectedIds: Set<string>,
    opts: { theme?: ShareExportTheme } = {},
  ): Promise<string | null> {
    const result = await buildShareImage(selectedIds, opts)
    if (!result) return null
    downloadBlob(result.blob, result.filename)
    return result.filename
  }

  return {
    buildShareImage,
    exportSelectedMessages,
  }
}

export function shareExportFilename(rawName: string, now: Date = new Date()): string {
  const date = now.toISOString().slice(0, 10)
  const slug = shareTitleSlug(rawName) || SHARE_FILENAME_FALLBACK
  return `${SHARE_FILENAME_PREFIX}-${slug}-${date}.png`
}

function shareTitleSlug(rawName: string): string {
  let value = (rawName || '').trim()
  // Unwrap values that are already composed filenames (the previous template
  // produced "opensquilla-chat-<title|chat>-<date>.png", which duplicated
  // "chat" whenever the title fell back).
  const looksComposed = /\.png$/i.test(value) || /^opensquilla-/i.test(value)
  if (looksComposed) {
    value = value
      .replace(/\.png$/i, '')
      .replace(/^opensquilla-/i, '')
      .replace(/-\d{4}-\d{2}-\d{2}$/, '')
      .replace(/^chat-(?=.)/i, '')
  }

  const slug = value
    .toLowerCase()
    // eslint-disable-next-line no-control-regex
    .replace(/[\u0000-\u001f<>:"/\\|?*]+/g, '-')
    .replace(/\s+/g, '-')
    .replace(/\.+$/g, '')
    .replace(/-{2,}/g, '-')
    .replace(/^-+|-+$/g, '')
  const capped = Array.from(slug).slice(0, SHARE_SLUG_MAX_LENGTH).join('').replace(/^-+|-+$/g, '')
  return dedupeAdjacentSegments(capped)
}

function dedupeAdjacentSegments(slug: string): string {
  const segments = slug.split('-').filter(Boolean)
  const result: string[] = []
  for (const segment of segments) {
    if (result[result.length - 1] !== segment) result.push(segment)
  }
  return result.join('-')
}

function selectedShareElements(thread: HTMLElement | null, selectedIds: Set<string>): HTMLElement[] {
  if (!thread) return []
  const elements = Array.from(thread.querySelectorAll<HTMLElement>('[data-share-message-id]'))
  return elements.filter(element => selectedIds.has(element.dataset.shareMessageId || ''))
}

export function buildShareDom(
  sourceElements: HTMLElement[],
  theme: ShareExportTheme = 'light',
  aiGeneratedLabel = '',
): HTMLElement {
  const stageWidth = EXPORT_WIDTH
  const stage = document.createElement('section')
  stage.id = SHARE_STAGE_ID
  // Forcing data-theme on the stage root re-resolves the token custom
  // properties for the whole cloned subtree, decoupling the export from the
  // live app theme. shareThemeTokens() reads getComputedStyle of THIS element,
  // and html-to-image inlines those computed styles into the raster.
  stage.dataset.theme = theme
  stage.setAttribute('aria-hidden', 'true')
  stage.className = 'chat-share-export-stage'
  stage.dataset.shareTemplateMetrics = JSON.stringify(shareTemplateMetrics(aiGeneratedLabel))
  const tokens = shareThemeTokens(stage)
  stage.style.cssText = [
    'position:fixed',
    'left:16px',
    'top:16px',
    `width:${stageWidth}px`,
    `max-width:${stageWidth}px`,
    'padding:0',
    'box-sizing:border-box',
    `background:${tokens.card}`,
    `color:${tokens.text}`,
    'z-index:-1',
    'pointer-events:none',
    'overflow:visible',
    'opacity:1',
  ].join(';')

  const style = document.createElement('style')
  style.textContent = shareExportCss()
  stage.appendChild(style)

  const stack = document.createElement('div')
  stack.className = 'chat-share-export-stack'
  sourceElements.forEach((element) => {
    const clone = cleanupShareClone(element.cloneNode(true) as HTMLElement)
    inlineBlobBackedImages(element, clone)
    stack.appendChild(clone)
  })
  stage.appendChild(stack)

  return stage
}

// Image artifacts render from blob: object URLs. html-to-image inlines images
// by fetch()-ing the src, but the gateway CSP allows blob: under img-src yet
// not connect-src, so that fetch is blocked and the picture drops out of the
// export. Bypass the fetch entirely: paint the live, already-decoded <img>
// (matched by its artifact key) onto a canvas and hand the clone a data: URL,
// which the rasteriser embeds directly without a network request.
function inlineBlobBackedImages(original: HTMLElement, clone: HTMLElement): void {
  clone.querySelectorAll<HTMLImageElement>('img').forEach((cloneImg) => {
    if (!cloneImg.src.startsWith('blob:')) return
    const key = cloneImg.dataset.artifactKey
    const source = key
      ? original.querySelector<HTMLImageElement>(`img[data-artifact-key="${CSS.escape(key)}"]`)
      : null
    // Only a fully-decoded same-origin image can be painted; otherwise leave the
    // blob src and let the capture's onImageErrorHandler degrade it gracefully.
    if (!source || !source.complete || !source.naturalWidth) return
    try {
      const canvas = document.createElement('canvas')
      canvas.width = source.naturalWidth
      canvas.height = source.naturalHeight
      const context = canvas.getContext('2d')
      if (!context) return
      context.drawImage(source, 0, 0)
      cloneImg.src = canvas.toDataURL('image/png')
      cloneImg.removeAttribute('srcset')
    } catch {
      // A tainted canvas (cross-origin artifact without CORS) throws here; keep
      // the blob src so the export still composes without the offending image.
    }
  })
}

function shareTemplateMetrics(aiGeneratedLabel: string) {
  return {
    width: SHARE_TEMPLATE_WIDTH,
    contentWidth: EXPORT_WIDTH,
    exportScale: captureScale(),
    bottomSafeArea: SHARE_EXPORT_BOTTOM_SAFE_AREA,
    userBubbleRightOffset: SHARE_EXPORT_USER_BUBBLE_RIGHT_OFFSET,
    top: SHARE_TEMPLATE_TOP,
    brandHeight: SHARE_TEMPLATE_BRAND_HEIGHT,
    brandGap: SHARE_TEMPLATE_BRAND_GAP,
    footerHeight: SHARE_TEMPLATE_FOOTER_HEIGHT,
    qrSize: SHARE_TEMPLATE_QR_SIZE,
    caption: SHARE_FOOTER_CAPTION,
    disclaimer: aiGeneratedLabel,
  }
}

function shareThemeTokens(themeEl: HTMLElement) {
  // Read the forced-theme custom properties off the export stage (not
  // document.documentElement), so the rasterized template follows the stage's
  // data-theme rather than the live app theme.
  const styles = getComputedStyle(themeEl)
  const token = (name: string, fallback: string) => styles.getPropertyValue(name).trim() || fallback
  return {
    page: token('--bg', '#f4f4f3'),
    card: token('--bg-surface', '#ffffff'),
    border: token('--border', 'rgba(32, 39, 34, 0.08)'),
    text: token('--text', '#18181b'),
    muted: token('--text-muted', '#4f5550'),
    fontSans: token('--font-sans', '-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif'),
  }
}

function cleanupShareClone(clone: HTMLElement): HTMLElement {
  clone.classList.remove(
    'msg-user--share-mode',
    'msg-user--share-selected',
    'msg-ai--share-mode',
    'msg-ai--share-selected',
  )
  clone.removeAttribute('data-share-selected')

  // Activity follows the existing reasoning-share contract: collapsed process
  // is omitted, while process the user explicitly expanded is included as
  // static content. The data attributes are the stable renderer/export
  // boundary; the class + <details open> fallback keeps exports from older
  // activity DOM compatible while the chat renderer migrates.
  clone.querySelectorAll<HTMLElement>('[data-share-activity], .assistant-activity')
    .forEach((activity) => {
      const usesShareContract = activity.hasAttribute('data-share-activity')
      const expanded = usesShareContract
        ? activity.dataset.shareExpanded === 'true'
        : activity.hasAttribute('open')
      const summary = activity.querySelector<HTMLElement>(
        '[data-share-activity-label], .assistant-activity__summary',
      )
      const declaredBody = activity.querySelector<HTMLElement>(
        '[data-share-activity-body], .assistant-activity__body',
      )

      if (!expanded) {
        activity.remove()
        return
      }

      // A renderer may keep the activity rows directly under the activity root
      // instead of wrapping them in the legacy body element. Move those rows
      // into the static body as well, excluding the interactive summary chrome.
      const body = declaredBody || document.createElement('div')
      if (!declaredBody) {
        Array.from(activity.childNodes).forEach((node) => {
          if (node !== summary) body.appendChild(node)
        })
      }
      if (!body.childNodes.length) {
        activity.remove()
        return
      }

      const block = document.createElement('div')
      block.className = 'chat-share-export-activity'
      const label = document.createElement('div')
      label.className = 'chat-share-export-activity__label'
      label.textContent = summary?.textContent?.replace(/\s+/g, ' ').trim()
        || i18n.global.t('chat.activityLabel')
      body.className = 'chat-share-export-activity__body'
      body.removeAttribute('data-share-activity-body')
      block.append(label, body)
      activity.replaceWith(block)
    })

  // Thinking fold: an expanded fold means the user deliberately opened the
  // reasoning and is sharing it — keep the text, but swap the interactive
  // <details> chrome for a quiet static label. A collapsed fold is just a
  // dead button in a static image and is dropped entirely.
  clone.querySelectorAll<HTMLElement>('.thinking-fold').forEach((fold) => {
    const body = fold.querySelector<HTMLElement>('.thinking-fold__body')
    const text = body?.textContent?.trim() || ''
    if (!fold.hasAttribute('open') || !text) {
      fold.remove()
      return
    }
    const block = document.createElement('div')
    block.className = 'chat-share-export-thinking'
    const label = document.createElement('div')
    label.className = 'chat-share-export-thinking__label'
    label.textContent = i18n.global.t('chat.thinking')
    const bodyText = document.createElement('div')
    bodyText.className = 'chat-share-export-thinking__body'
    bodyText.textContent = text
    block.append(label, bodyText)
    fold.replaceWith(block)
  })

  clone.querySelectorAll<HTMLElement>('[data-share-selected]').forEach((element) => {
    element.removeAttribute('data-share-selected')
  })
  clone.querySelectorAll<HTMLElement>(SHARE_CLONE_STRIP_SELECTORS.join(','))
    .forEach(element => element.remove())

  clone.querySelectorAll<HTMLElement>('*').forEach((element) => {
    element.classList.remove(
      'msg-user--share-mode',
      'msg-user--share-selected',
      'msg-ai--share-mode',
      'msg-ai--share-selected',
    )
  })

  clone.style.transform = 'none'
  return clone
}

function shareExportCss(): string {
  return `
    #${SHARE_STAGE_ID},
    #${SHARE_STAGE_ID} * {
      animation: none !important;
      transition: none !important;
      caret-color: transparent !important;
    }

    #${SHARE_STAGE_ID} .chat-share-export-stack {
      display: flex;
      flex-direction: column;
      gap: 0;
      width: 100%;
      padding-bottom: ${SHARE_EXPORT_BOTTOM_SAFE_AREA}px;
      box-sizing: border-box;
    }

    #${SHARE_STAGE_ID} .msg-user {
      padding-right: ${SHARE_EXPORT_USER_BUBBLE_RIGHT_OFFSET}px;
      box-sizing: border-box;
    }

    #${SHARE_STAGE_ID} button,
    #${SHARE_STAGE_ID} input,
    #${SHARE_STAGE_ID} textarea,
    #${SHARE_STAGE_ID} select {
      pointer-events: none !important;
    }

    #${SHARE_STAGE_ID} .chat-share-export-thinking {
      margin: 0 0 10px;
      padding: 6px 10px;
      border-left: 2px solid color-mix(in srgb, currentColor 22%, transparent);
    }

    #${SHARE_STAGE_ID} .chat-share-export-thinking__label {
      font-size: 11px;
      letter-spacing: 0.04em;
      opacity: 0.55;
      margin-bottom: 3px;
    }

    #${SHARE_STAGE_ID} .chat-share-export-thinking__body {
      font-size: 12px;
      line-height: 1.55;
      opacity: 0.75;
      white-space: pre-wrap;
    }

    #${SHARE_STAGE_ID} .chat-share-export-activity {
      margin: 0 0 10px;
      padding: 6px 0;
      border: 0;
      background: transparent;
    }

    #${SHARE_STAGE_ID} .chat-share-export-activity__label {
      font-size: 11px;
      letter-spacing: 0.02em;
      opacity: 0.6;
      margin-bottom: 5px;
    }

    #${SHARE_STAGE_ID} .chat-share-export-activity__body {
      display: grid;
      gap: 6px;
      min-width: 0;
    }

    /* The live reasoning body is a capped scroll container; a static image has
       no scrollbar, so the cap would silently truncate long reasoning. Let the
       export lay out the full text instead. */
    #${SHARE_STAGE_ID} .thinking-block__body {
      max-height: none !important;
      overflow-y: visible !important;
    }

    /* The live meta line is hover-dimmed; the static image has no hover. */
    #${SHARE_STAGE_ID} .msg-ai-meta > span {
      opacity: 1 !important;
    }

    /* Markdown blocks the scoped chat CSS does not reach in a cloned stage.
       Mirrors styles/chat-markdown.css so exported images render tables,
       headings, quotes, links and rules the same as the live chat. */
    #${SHARE_STAGE_ID} .msg-ai-text table {
      display: block;
      width: max-content;
      max-width: 100%;
      overflow-x: auto;
      border-collapse: collapse;
      border: 1px solid var(--border);
      margin: var(--sp-2) 0;
      font-size: 0.9375em;
    }
    #${SHARE_STAGE_ID} .msg-ai-text th,
    #${SHARE_STAGE_ID} .msg-ai-text td {
      padding: var(--sp-1) var(--sp-3);
      border: 1px solid var(--border);
      text-align: left;
      vertical-align: top;
    }
    #${SHARE_STAGE_ID} .msg-ai-text th {
      background: var(--bg-elevated);
      font-weight: 600;
    }
    #${SHARE_STAGE_ID} .msg-ai-text h1,
    #${SHARE_STAGE_ID} .msg-ai-text h2,
    #${SHARE_STAGE_ID} .msg-ai-text h3,
    #${SHARE_STAGE_ID} .msg-ai-text h4,
    #${SHARE_STAGE_ID} .msg-ai-text h5,
    #${SHARE_STAGE_ID} .msg-ai-text h6 {
      font-family: var(--font-display);
      font-weight: 600;
      line-height: 1.3;
      margin: var(--sp-3) 0 var(--sp-2);
      color: var(--text);
    }
    #${SHARE_STAGE_ID} .msg-ai-text h1 { font-size: 1.4em; }
    #${SHARE_STAGE_ID} .msg-ai-text h2 { font-size: 1.25em; }
    #${SHARE_STAGE_ID} .msg-ai-text h3 { font-size: 1.1em; }
    #${SHARE_STAGE_ID} .msg-ai-text h4 { font-size: 1em; }
    #${SHARE_STAGE_ID} .msg-ai-text h5,
    #${SHARE_STAGE_ID} .msg-ai-text h6 { font-size: 0.9em; color: var(--text-muted); }
    #${SHARE_STAGE_ID} .msg-ai-text blockquote {
      border-left: 2px solid var(--border-strong);
      color: var(--text-muted);
      margin: var(--sp-2) 0;
      padding-left: var(--sp-3);
    }
    #${SHARE_STAGE_ID} .msg-ai-text a { color: var(--accent); text-decoration: none; }
    #${SHARE_STAGE_ID} .msg-ai-text hr {
      border: 0;
      border-top: 1px solid var(--border);
      margin: var(--sp-3) 0;
    }
    #${SHARE_STAGE_ID} .msg-ai-text li:has(> input[type='checkbox']) {
      list-style: none;
      margin-left: -1.1em;
    }
    #${SHARE_STAGE_ID} .msg-ai-text li > input[type='checkbox'] {
      margin: 0 0.4em 0 0;
      vertical-align: middle;
      accent-color: var(--accent);
    }
  `
}

async function captureStageWithDom(stage: HTMLElement): Promise<HTMLCanvasElement> {
  const rect = stage.getBoundingClientRect()
  const height = assertShareStageHeight(stage, rect)
  // Load the rasteriser on demand so it stays out of the chat critical path.
  const { toCanvas } = await import('html-to-image')
  const canvas = await toCanvas(stage, {
    backgroundColor: shareThemeTokens(stage).card,
    // Cache-busting must stay OFF: chat image artifacts render from blob:
    // object URLs, and html-to-image's cache-bust glues a "?<ts>" query onto
    // every image src. A blob: URL with a query string is not a registered
    // object URL, so the fetch fails, the cloned <img> src is cleared, and the
    // resulting error event rejects the whole capture. These same-origin blob
    // and static resources never need cache-busting anyway.
    cacheBust: false,
    pixelRatio: captureScale(),
    width: Math.ceil(rect.width),
    height,
    // Defence in depth: a single image that still can't be embedded (e.g. its
    // blob URL was revoked mid-export) must not abort the PNG. Swallow the
    // error so that one image comes through blank instead of failing the share.
    onImageErrorHandler: () => {},
    style: {
      transform: 'none',
      margin: '0',
    },
  })
  canvas.style.width = `${EXPORT_WIDTH}px`
  canvas.style.height = `${Math.round((canvas.height * EXPORT_WIDTH) / canvas.width)}px`
  return canvas
}

async function composeShareTemplate(
  contentCanvas: HTMLCanvasElement,
  stage: HTMLElement,
  aiGeneratedLabel: string,
): Promise<{ blob: Blob; width: number; height: number }> {
  const tokens = shareThemeTokens(stage)
  const contentHeight = Math.ceil((contentCanvas.height * EXPORT_WIDTH) / contentCanvas.width)
  const height = SHARE_TEMPLATE_TOP
    + SHARE_TEMPLATE_BRAND_HEIGHT
    + SHARE_TEMPLATE_BRAND_GAP
    + contentHeight
    + SHARE_TEMPLATE_FOOTER_HEIGHT

  if (height > MAX_EXPORT_HEIGHT) {
    throw new Error(`Share image is too tall (${height}px). Select fewer bubbles.`)
  }

  const scale = captureScale()
  const canvas = document.createElement('canvas')
  canvas.width = Math.ceil(SHARE_TEMPLATE_WIDTH * scale)
  canvas.height = Math.ceil(height * scale)
  canvas.style.width = `${SHARE_TEMPLATE_WIDTH}px`
  canvas.style.height = `${height}px`

  const context = canvas.getContext('2d')
  if (!context) throw new Error('Canvas is unavailable')
  context.scale(scale, scale)

  context.fillStyle = tokens.page
  context.fillRect(0, 0, SHARE_TEMPLATE_WIDTH, height)

  await drawTemplateBrand(context, SHARE_TEMPLATE_TOP, tokens)

  const cardX = SHARE_TEMPLATE_MARGIN
  const cardY = SHARE_TEMPLATE_TOP + SHARE_TEMPLATE_BRAND_HEIGHT + SHARE_TEMPLATE_BRAND_GAP
  roundRect(context, cardX, cardY, EXPORT_WIDTH, contentHeight, 8)
  context.fillStyle = tokens.card
  context.fill()
  context.save()
  roundRect(context, cardX, cardY, EXPORT_WIDTH, contentHeight, 8)
  context.clip()
  context.drawImage(contentCanvas, cardX, cardY, EXPORT_WIDTH, contentHeight)
  context.restore()
  roundRect(context, cardX, cardY, EXPORT_WIDTH, contentHeight, 8)
  context.strokeStyle = tokens.border
  context.stroke()

  await drawTemplateFooter(context, cardY + contentHeight, tokens, aiGeneratedLabel)

  const blob = await blobFromCanvas(canvas)
  return { blob, width: SHARE_TEMPLATE_WIDTH, height }
}

async function drawTemplateBrand(
  context: CanvasRenderingContext2D,
  y: number,
  tokens: ReturnType<typeof shareThemeTokens>,
) {
  const markSize = 22
  const markGap = 8
  const mark = await loadOptionalImage(staticAssetUrl('img/opensquilla-mark.png'))

  // Measure with the wordmark font already set so the centered group is exact.
  context.font = `600 18px ${tokens.fontSans}`
  const wordWidth = context.measureText('OpenSquilla').width
  const groupWidth = (mark ? markSize + markGap : 0) + wordWidth
  let cursorX = Math.round((SHARE_TEMPLATE_WIDTH - groupWidth) / 2)

  if (mark) {
    const markY = y + Math.round((SHARE_TEMPLATE_BRAND_HEIGHT - markSize) / 2)
    context.drawImage(mark, cursorX, markY, markSize, markSize)
    cursorX += markSize + markGap
  }

  context.fillStyle = tokens.text
  context.textAlign = 'left'
  context.textBaseline = 'middle'
  context.fillText('OpenSquilla', cursorX, y + SHARE_TEMPLATE_BRAND_HEIGHT / 2)
  context.textBaseline = 'alphabetic'
}

async function drawTemplateFooter(
  context: CanvasRenderingContext2D,
  startY: number,
  tokens: ReturnType<typeof shareThemeTokens>,
  aiGeneratedLabel: string,
) {
  const qr = await loadOptionalImage(staticAssetUrl('img/QRcode.png'))

  // One cohesive, centered footer band: [QR][gap][site + provenance], the
  // whole group centered on the template width rather than pinned to opposite
  // corners. The provenance survives when the image leaves the chat UI.
  const captionGap = 12
  context.font = `500 12px ${tokens.fontSans}`
  const captionWidth = context.measureText(SHARE_FOOTER_CAPTION).width
  context.font = `400 11px ${tokens.fontSans}`
  const disclaimerWidth = context.measureText(aiGeneratedLabel).width
  const textWidth = Math.max(captionWidth, disclaimerWidth)
  const groupWidth = (qr ? SHARE_TEMPLATE_QR_SIZE + captionGap : 0) + textWidth
  const groupX = Math.round((SHARE_TEMPLATE_WIDTH - groupWidth) / 2)
  const qrY = startY + 16
  const centerY = qrY + SHARE_TEMPLATE_QR_SIZE / 2

  let captionX = groupX
  if (qr) {
    const qrX = groupX
    // White backing keeps the QR scannable on the dark theme page fill.
    roundRect(context, qrX, qrY, SHARE_TEMPLATE_QR_SIZE, SHARE_TEMPLATE_QR_SIZE, 6)
    context.fillStyle = '#ffffff'
    context.fill()
    context.drawImage(qr, qrX, qrY, SHARE_TEMPLATE_QR_SIZE, SHARE_TEMPLATE_QR_SIZE)
    captionX = qrX + SHARE_TEMPLATE_QR_SIZE + captionGap
  }

  context.fillStyle = tokens.muted
  context.textAlign = 'left'
  context.textBaseline = 'middle'
  context.font = `500 12px ${tokens.fontSans}`
  context.fillText(SHARE_FOOTER_CAPTION, captionX, centerY - 9)
  context.font = `400 11px ${tokens.fontSans}`
  context.fillText(aiGeneratedLabel, captionX, centerY + 10)
  context.textBaseline = 'alphabetic'
}

function assertShareStageHeight(stage: HTMLElement, rect: DOMRect): number {
  const height = Math.ceil(Math.max(rect.height, stage.offsetHeight, stage.scrollHeight))
  if (height > MAX_EXPORT_HEIGHT) {
    throw new Error(`Share image is too tall (${height}px). Select fewer bubbles.`)
  }
  return height
}

async function waitForStablePaint(): Promise<void> {
  await new Promise<void>(resolve => requestAnimationFrame(() => resolve()))
  await new Promise<void>(resolve => requestAnimationFrame(() => resolve()))
}

function captureScale(): number {
  return EXPORT_SCALE
}

function blobFromCanvas(canvas: HTMLCanvasElement): Promise<Blob> {
  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => {
      if (blob) resolve(blob)
      else reject(new Error('Failed to create PNG'))
    }, 'image/png')
  })
}

function loadOptionalImage(src: string): Promise<HTMLImageElement | null> {
  if (!src) return Promise.resolve(null)
  return new Promise((resolve) => {
    const image = new Image()
    image.decoding = 'async'
    image.onload = () => resolve(image)
    image.onerror = () => resolve(null)
    image.src = src
  })
}

function staticAssetUrl(path: string): string {
  const base = document.getElementById('opensquilla-data')?.dataset.basePath || '/control'
  return `${base}/static/${path.replace(/^\/+/, '')}`
}

function roundRect(
  context: CanvasRenderingContext2D,
  x: number,
  y: number,
  width: number,
  height: number,
  radius: number,
) {
  const r = Math.min(radius, width / 2, height / 2)
  context.beginPath()
  context.moveTo(x + r, y)
  context.lineTo(x + width - r, y)
  context.quadraticCurveTo(x + width, y, x + width, y + r)
  context.lineTo(x + width, y + height - r)
  context.quadraticCurveTo(x + width, y + height, x + width - r, y + height)
  context.lineTo(x + r, y + height)
  context.quadraticCurveTo(x, y + height, x, y + height - r)
  context.lineTo(x, y + r)
  context.quadraticCurveTo(x, y, x + r, y)
  context.closePath()
}
