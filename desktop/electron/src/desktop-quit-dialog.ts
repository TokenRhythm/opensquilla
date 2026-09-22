export type DesktopQuitDialogResponse = 'cancel' | 'confirm'

export interface DesktopQuitDialogCopy {
  title: string
  message: string
  detail: string
  keepRunning: string
  confirm: string
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
  return value === 'confirm' || value === 'cancel' ? value : null
}

/** Build a small, keyboard-accessible modal without depending on the host OS dialog theme. */
export function buildDesktopQuitDialogHtml(copy: DesktopQuitDialogCopy): string {
  const text = Object.fromEntries(
    Object.entries(copy).map(([key, value]) => [key, escapeQuitDialogHtml(value)]),
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
    .heading { display: flex; align-items: flex-start; gap: 14px; }
    .icon { flex: 0 0 38px; width: 38px; height: 38px; display: grid; place-items: center;
      border-radius: 12px; color: #a16207; background: #fef3c7; }
    h1 { margin: 0; font-size: 19px; line-height: 1.35; font-weight: 650; letter-spacing: -.01em; }
    .message { margin: 18px 0 7px; font-size: 14px; line-height: 1.55; font-weight: 600; }
    .detail { margin: 0; color: #64748b; font-size: 13px; line-height: 1.55; }
    .actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: auto; padding-top: 25px; }
    button { min-height: 36px; padding: 0 16px; border: 0; border-radius: 9px; font: inherit; font-size: 13px;
      font-weight: 600; cursor: pointer; }
    button:focus-visible { outline: 3px solid rgba(37, 99, 235, .35); outline-offset: 2px; }
    .secondary { color: #334155; background: #f1f5f9; }
    .secondary:hover { background: #e2e8f0; }
    .primary { color: #fff; background: #b45309; }
    .primary:hover { background: #92400e; }
    @media (prefers-color-scheme: dark) {
      body { color: #e5e7eb; }
      .card { border-color: rgba(148, 163, 184, .22); background: #20242b; box-shadow: 0 18px 48px rgba(0, 0, 0, .45); }
      .icon { color: #fbbf24; background: rgba(146, 64, 14, .35); }
      .detail { color: #aab5c4; }
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
    <div class="actions">
      <button class="secondary" id="keep" type="button">${text.keepRunning}</button>
      <button class="primary" id="confirm" type="button">${text.confirm}</button>
    </div>
  </main>
  <script>
    const send = response => window.opensquillaDesktop.quitDialogRespond(response)
    document.getElementById('keep').addEventListener('click', () => send('cancel'))
    document.getElementById('confirm').addEventListener('click', () => send('confirm'))
    window.addEventListener('keydown', event => {
      if (event.key === 'Escape') send('cancel')
      if (event.key === 'Enter' && event.target === document.body) send('confirm')
    })
    // Keep the safe default from the native dialog: Enter confirms staying open.
    document.getElementById('keep').focus()
  </script>
</body>
</html>`
}
