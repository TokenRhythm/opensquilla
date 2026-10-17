export type DesktopQuitDialogResponse = 'cancel' | 'confirm' | 'background'

export interface DesktopQuitDialogDecision {
  response: DesktopQuitDialogResponse
  remember: boolean
}

export interface DesktopQuitDialogCopy {
  title: string
  message: string
  detail: string
  keepRunning: string
  confirm: string
  background?: string
  remember?: string
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
export function buildDesktopQuitDialogHtml(copy: DesktopQuitDialogCopy): string {
  const text = Object.fromEntries(
    Object.entries(copy).map(([key, value]) => [key, escapeQuitDialogHtml(value ?? '')]),
  ) as unknown as DesktopQuitDialogCopy
  return `<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'">
  <meta name="color-scheme" content="light dark">
  <style>
    :root { color-scheme: light dark; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    * { box-sizing: border-box; }
    html, body { margin: 0; min-height: 100%; }
    body { padding: 1px; background: transparent; color: #1f2937; }
    .card { min-height: calc(100vh - 2px); display: flex; flex-direction: column;
      padding: 27px 30px 24px; border: 1px solid rgba(15, 23, 42, .11); border-radius: 17px;
      background: #fff; box-shadow: 0 18px 48px rgba(15, 23, 42, .22); }
    .heading { display: flex; align-items: flex-start; gap: 14px; -webkit-app-region: drag; }
    .icon { flex: 0 0 38px; width: 38px; height: 38px; display: grid; place-items: center;
      border-radius: 12px; color: #a16207; background: #fef3c7; }
    h1 { margin: 0; font-size: 19px; line-height: 1.35; font-weight: 650; letter-spacing: -.01em; }
    .message { margin: 18px 0 7px; font-size: 14px; line-height: 1.55; font-weight: 600; }
    .detail { margin: 0; color: #64748b; font-size: 13px; line-height: 1.65; white-space: pre-line; }
    .remember { display: flex; align-items: center; gap: 8px; margin-top: 21px;
      color: #64748b; font-size: 12px; cursor: pointer; }
    .remember input { width: 15px; height: 15px; margin: 0; accent-color: #2563eb; }
    .actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: auto; padding-top: 25px; }
    .actions.with-background { display: grid; grid-template-columns: auto minmax(0, 1fr) minmax(0, 1fr); }
    button { min-width: 0; min-height: 39px; padding: 9px 15px; border: 0; border-radius: 9px; font: inherit; font-size: 13px;
      line-height: 1.45; -webkit-app-region: no-drag;
      font-weight: 600; cursor: pointer; }
    button:disabled { cursor: default; opacity: .7; }
    button:focus-visible { outline: 3px solid rgba(37, 99, 235, .35); outline-offset: 2px; }
    .secondary { color: #334155; background: #f1f5f9; }
    .secondary:hover { background: #e2e8f0; }
    .primary { color: #fff; background: #b45309; }
    .primary:hover { background: #92400e; }
    .background { color: #fff; background: #2563eb; }
    .background:hover { background: #1d4ed8; }
    @media (prefers-color-scheme: dark) {
      body { color: #e5e7eb; }
      .card { border-color: rgba(148, 163, 184, .22); background: #20242b; box-shadow: 0 18px 48px rgba(0, 0, 0, .45); }
      .icon { color: #fbbf24; background: rgba(146, 64, 14, .35); }
      .detail, .remember { color: #aab5c4; }
      .secondary { color: #e5e7eb; background: #343b47; }
      .secondary:hover { background: #414b5b; }
      .primary { color: #1f1303; background: #f59e0b; }
      .primary:hover { background: #fbbf24; }
    }
  </style>
</head>
<body>
  <main class="card" role="dialog" aria-modal="true" aria-labelledby="title" aria-describedby="message detail">
    <div class="heading">
      <div class="icon" aria-hidden="true">
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M10.3 3.3 2.2 17.2A2 2 0 0 0 3.9 20h16.2a2 2 0 0 0 1.7-2.8L13.7 3.3a2 2 0 0 0-3.4 0Z"/><path d="M12 9v4M12 17h.01"/>
        </svg>
      </div>
      <h1 id="title">${text.title}</h1>
    </div>
    <p id="message" class="message">${text.message}</p>
    <p id="detail" class="detail">${text.detail}</p>
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
