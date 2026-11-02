export type DesktopQuitDialogResponse = 'cancel' | 'confirm' | 'background'

export interface DesktopQuitDialogDecision {
  response: DesktopQuitDialogResponse
  remember: boolean
}

export interface DesktopQuitDialogCopy {
  lang?: string
  title: string
  message: string
  detail: string
  keepRunning: string
  confirm: string
  background?: string
  remember?: string
}

const QUIT_DIALOG_COLOR_TOKENS = [
  '--bg-surface', '--bg-hover', '--text', '--text-muted', '--border',
  '--accent', '--accent-hover', '--accent-foreground',
] as const

/** Accept only a complete palette of literal colors, never renderer-supplied CSS. */
export function normalizeDesktopQuitDialogPalette(value: unknown): Record<string, string> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {}
  const palette: Record<string, string> = {}
  for (const token of QUIT_DIALOG_COLOR_TOKENS) {
    const color = (value as Record<string, unknown>)[token]
    if (typeof color !== 'string' || !/^#[\da-f]{6}$/i.test(color)) return {}
    palette[token] = color
  }
  return palette
}

/** Escape text inserted into the self-contained confirmation document. */
export function escapeQuitDialogHtml(value: string): string {
  return value
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;')
}

export function parseDesktopQuitDialogResponse(value: unknown): DesktopQuitDialogResponse | null {
  if (value === 'confirm' || value === 'cancel' || value === 'background') return value
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return null
  const candidate = value as Record<string, unknown>
  if (Object.keys(candidate).length !== 2 || !Object.hasOwn(candidate, 'response')
    || !Object.hasOwn(candidate, 'remember') || typeof candidate.remember !== 'boolean') return null
  return candidate.response === 'confirm' || candidate.response === 'cancel' || candidate.response === 'background'
    ? candidate.response : null
}

/** Build a small, keyboard-accessible modal without depending on the host OS dialog theme. */
export function buildDesktopQuitDialogHtml(copy: DesktopQuitDialogCopy, palette?: unknown): string {
  const text = Object.fromEntries(
    Object.entries(copy).map(([key, value]) => [key, escapeQuitDialogHtml(value ?? '')]),
  ) as unknown as DesktopQuitDialogCopy
  const themeStyle = Object.entries(normalizeDesktopQuitDialogPalette(palette))
    .map(([token, color]) => `${token}: ${color};`).join(' ')
  return `<!doctype html>
<html lang="${text.lang || 'en'}">
<head>
  <meta charset="utf-8">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'">
  <meta name="color-scheme" content="light dark">
  <style>
    :root { color-scheme: light dark; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      --bg-surface: #FFFFFF; --bg-hover: #F0F0F0; --text: #202020; --text-muted: #626262;
      --border: #E5E5E5; --accent: #BA4D0F; --accent-hover: #A5440C; --accent-foreground: #FFFFFF; }
    @media (prefers-color-scheme: dark) {
      :root { --bg-surface: #202022; --bg-hover: #353539; --text: #F5F5F7; --text-muted: #B0B0B6;
        --border: #303034; --accent: #F26A1B; --accent-hover: #FF7A2E; --accent-foreground: #160B02; }
    }
    /* The cached application palette also covers custom themes; startup and
       system mode use the matching light/dark token defaults above. */
    :root { ${themeStyle} }
    * { box-sizing: border-box; }
    html, body { margin: 0; min-height: 100%; }
    body { padding: 1px; background: transparent; color: var(--text); }
    .card { min-height: calc(100vh - 2px); display: flex; flex-direction: column;
      padding: 24px 28px; border: 1px solid var(--border); border-radius: 8px; background: var(--bg-surface); }
    .heading { -webkit-app-region: drag; }
    h1 { margin: 0; font-size: 20px; line-height: 1.35; font-weight: 600; letter-spacing: -.01em; }
    .message, .detail { color: var(--text-muted); font-size: 14px; line-height: 1.6; white-space: pre-line; }
    .message { margin: 16px 0 0; }
    .detail { margin: 8px 0 0; }
    .remember { display: flex; align-items: center; gap: 8px; margin-top: 16px;
      color: var(--text-muted); font-size: 12px; cursor: pointer; }
    .remember input { width: 15px; height: 15px; margin: 0; accent-color: var(--accent); }
    .actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: auto; padding-top: 24px; }
    .actions.with-background #keep { margin-right: auto; }
    button { min-width: 72px; min-height: 36px; padding: 7px 16px; border: 1px solid var(--border); border-radius: 5px; font: inherit; font-size: 13px;
      line-height: 1.45; -webkit-app-region: no-drag;
      font-weight: 500; cursor: pointer; }
    button:disabled { cursor: default; opacity: .7; }
    button:focus-visible, input:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }
    .secondary, .with-background .primary { color: var(--text); background: transparent; }
    .secondary:hover, .with-background .primary:hover { background: var(--bg-hover); }
    .primary, .background { color: var(--accent-foreground); background: var(--accent); border-color: var(--accent); }
    .primary:hover, .background:hover { background: var(--accent-hover); }
    .with-background .primary { border-color: var(--border); }
  </style>
</head>
<body>
  <main class="card" role="dialog" aria-modal="true" aria-labelledby="title" aria-describedby="message${text.detail ? ' detail' : ''}">
    <div class="heading">
      <h1 id="title">${text.title}</h1>
    </div>
    <p id="message" class="message">${text.message}</p>
    ${text.detail ? `<p id="detail" class="detail">${text.detail}</p>` : ''}
    ${text.remember ? `<label class="remember"><input id="remember" type="checkbox">${text.remember}</label>` : ''}
    <div class="actions${text.background ? ' with-background' : ''}">
      <button class="secondary" id="keep" type="button">${text.keepRunning}</button>
      ${text.background ? `<button class="background" id="background" type="button">${text.background}</button>` : ''}
      <button class="primary" id="confirm" type="button">${text.confirm}</button>
    </div>
  </main>
  <script>
    let sent = false
    const send = response => {
      if (sent) return
      sent = true
      for (const button of document.querySelectorAll('button')) button.disabled = true
      window.opensquillaDesktop.quitDialogRespond({ response,
        remember: document.getElementById('remember')?.checked === true })
    }
    document.getElementById('keep').addEventListener('click', () => send('cancel'))
    document.getElementById('background')?.addEventListener('click', () => send('background'))
    document.getElementById('confirm').addEventListener('click', () => send('confirm'))
    window.addEventListener('keydown', event => {
      if (event.key === 'Escape') { event.preventDefault(); send('cancel') }
      if (event.key === 'Tab') {
        const controls = [...document.querySelectorAll('input, button')].filter(control => !control.disabled)
        const first = controls[0], last = controls[controls.length - 1]
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus() }
        if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus() }
      }
    })
    // Enter follows the focused safe action; it never implicitly chooses quit.
    if (document.getElementById('background')) document.getElementById('background').focus()
    else document.getElementById('keep').focus()
  </script>
</body>
</html>`
}
