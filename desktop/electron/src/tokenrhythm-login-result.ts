import { randomBytes } from 'node:crypto'
import type { IncomingMessage, Server, ServerResponse } from 'node:http'

export type LoginResultStatus = 'pending' | 'success' | 'cancelled' | 'error'

const resultScript = `(() => {
  const useProtocol = document.currentScript.dataset.protocol === 'true';
  const webReturn = document.currentScript.dataset.webReturn || '';
  const path = location.pathname;
  const title = document.getElementById('result-title');
  const description = document.getElementById('result-description');
  const open = document.getElementById('open-app');
  const hint = document.getElementById('open-hint');
  let opening = false;
  async function openDesktop() {
    if (opening) return;
    if (webReturn) { location.assign(webReturn); return; }
    if (useProtocol) {
      try { location.assign('opensquilla://open'); } catch {}
      return;
    }
    opening = true;
    try {
      const response = await fetch(path + '/open', {
        method: 'POST', credentials: 'omit', headers: { 'x-opensquilla-login-result': '1' }
      });
      if (!response.ok) throw Error();
      hint.textContent = '已打开 OpenSquilla，你可以关闭此页面。';
    } catch {
      hint.textContent = '请从程序坞或任务栏返回 OpenSquilla。';
    } finally { opening = false; }
  }
  open.addEventListener('click', event => { event.preventDefault(); void openDesktop(); });
  async function check() {
    try {
      const response = await fetch(path + '/status', { credentials: 'omit', cache: 'no-store' });
      if (!response.ok) throw Error();
      const { status } = await response.json();
      if (status === 'pending') { setTimeout(check, 350); return; }
      if (status === 'success') {
        title.textContent = '登录成功';
        description.textContent = webReturn ? '正在返回 OpenSquilla 网页界面。' : '你可以关闭此页面，返回桌面端继续工作。';
        if (webReturn) open.textContent = '返回 WebUI';
        open.hidden = false;
        hint.textContent = '如未自动打开，请点击上方按钮。';
        document.title = '登录成功 · OpenSquilla';
        void openDesktop();
      } else if (status === 'cancelled') {
        title.textContent = '已取消登录';
        description.textContent = webReturn ? '请返回 OpenSquilla 网页界面重新登录。' : '你可以关闭此页面，返回 OpenSquilla 重新登录。';
      } else if (status === 'error') {
        title.textContent = '登录未完成';
        description.textContent = webReturn ? '请返回 OpenSquilla 网页界面查看账号状态并重试。' : '请返回 OpenSquilla 查看账号状态并重试。';
      } else { throw Error(); }
    } catch {
      title.textContent = '请返回 OpenSquilla';
      description.textContent = webReturn ? '此页面已失去连接，请在 OpenSquilla 网页界面查看登录结果。' : '此页面已失去连接，请在桌面端查看登录结果。';
    }
  }
  void check();
})();`

const resultStyles = `
:root { color-scheme: light; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; color: #17191c; background: #fff; -webkit-font-smoothing: antialiased; }
* { box-sizing: border-box; }
body { margin: 0; min-width: 280px; }
main { width: min(400px, calc(100% - 40px)); margin: 0 auto; padding: clamp(80px, calc(45svh - 24px), 520px) 0 56px; text-align: center; }
.brand { display: flex; align-items: center; justify-content: center; gap: 10px; flex-wrap: wrap; margin-bottom: 78px; }
.wordmark { display: inline-flex; align-items: center; gap: 7px; font-size: 25px; font-weight: 650; letter-spacing: -1px; }
.mark { width: 40px; height: 27px; flex: none; }
.badge { padding: 3px 6px; border-radius: 3px; background: #17191c; color: #fff; font-size: 11px; font-weight: 600; line-height: 16px; }
h1 { margin: 0; font-size: 19px; line-height: 28px; font-weight: 500; }
.description { margin: 12px 0 0; color: #797e87; font-size: 13px; line-height: 22px; }
.actions { margin-top: 76px; }
.open-app { display: block; width: min(280px, 100%); margin-inline: auto; padding: 12px 20px; border-radius: 11px; background: #17191c; color: #fff; text-decoration: none; font-size: 13px; line-height: 20px; }
.open-app:hover { background: #34373c; }
.open-app:focus-visible { outline: 2px solid #5271ff; outline-offset: 4px; }
[hidden] { display: none !important; }
.hint { margin: 14px 0 0; min-height: 20px; color: #8a8f97; font-size: 12px; line-height: 20px; }
@media (max-width: 600px) { main { padding-top: max(80px, 28svh); } .brand { margin-bottom: 60px; } .actions { margin-top: 56px; } }
@media (max-height: 600px) { main { padding-top: 64px; } .brand { margin-bottom: 40px; } .actions { margin-top: 40px; } }
`

function resultPage(nonce: string, useProtocol: boolean, webReturnUrl: string): string {
  // Same brand mark as the website consent page; no network assets or credentials.
  return `<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer"><title>正在完成登录 · OpenSquilla</title>
<style nonce="${nonce}">${resultStyles}</style></head>
<body><main>
  <div class="brand" aria-label="TokenRhythm · OpenSquilla">
    <span class="wordmark"><svg class="mark" viewBox="10 15 62 38" aria-hidden="true"><path fill="currentColor" d="M28.2869 15.3313L52.3032 15.3318C56.6495 15.332 61.1409 15.3885 65.4782 15.3038C62.4476 17.8251 58.7745 21.2659 55.8259 23.9557C54.451 24.0375 52.5027 23.9839 51.0859 23.9842C48.459 23.9946 45.832 23.9874 43.2052 23.9626C44.8543 25.4287 47.4485 28.1941 49.1119 29.8604L61.0355 41.7999L68.4647 49.2348C68.9123 49.6831 71.0764 51.7891 71.2735 52.1321L71.1537 52.1688C67.1352 52.0344 62.9706 52.1522 58.9688 51.9728L45.811 38.8082C42.8897 35.885 39.8219 32.8966 36.9616 29.9257L36.9693 44.1437C34.1378 46.911 31.1624 49.6754 28.2845 52.4082L28.2869 15.3313Z"/><path fill="currentColor" d="M66.8075 16.3432C66.9854 16.5409 66.8988 26.5212 66.8939 27.8531C64.2096 30.6301 61.1162 33.5951 58.3448 36.3093L55.4318 33.3274C54.3728 32.2341 53.3067 31.1479 52.2334 30.0688L66.8075 16.3432Z"/><path fill="currentColor" d="M20.3634 15.2565C22.2816 15.2079 24.3745 15.2491 26.3065 15.2484C26.241 18.0694 26.2922 21.1045 26.2817 23.9411L16.4729 23.9386C14.5285 23.9342 12.3458 23.8846 10.4213 23.964C13.7597 21.1535 17.0176 18.0347 20.3634 15.2565Z"/></svg><span>TokenRhythm</span></span>
    <span class="badge">OpenSquilla</span>
  </div>
  <section aria-live="polite" aria-atomic="true">
    <h1 id="result-title">正在完成登录</h1>
    <p class="description" id="result-description">正在确认账号连接，请稍候。</p>
  </section>
  <div class="actions">
    <a class="open-app" id="open-app" href="${useProtocol ? 'opensquilla://open' : '#'}" hidden>打开 OpenSquilla</a>
    <p class="hint" id="open-hint" role="status"></p>
  </div>
  <noscript><p class="description">请返回 OpenSquilla 查看登录结果。</p></noscript>
</main><script nonce="${nonce}" data-protocol="${useProtocol}" data-web-return="${webReturnUrl}">${resultScript}</script></body></html>`
}

/** Short-lived browser receipt, isolated from the account and model API. */
export class TokenRhythmLoginResult {
  readonly path = `/oauth/result/${randomBytes(32).toString('base64url')}`
  private status: LoginResultStatus = 'pending'
  private readonly timer: NodeJS.Timeout
  private closed = false
  constructor(
    private readonly server: Server,
    private readonly origin: string,
    private readonly useProtocol: boolean,
    private readonly openDesktop: () => Promise<void>,
    lifetimeMs = 300_000,
    private readonly webReturnUrl = '',
  ) {
    if (webReturnUrl) {
      const target = new URL(webReturnUrl)
      if (target.protocol !== 'http:' || target.hostname !== '127.0.0.1' || target.username || target.password || target.hash || target.search || !/^\/control\/settings\/account\/?$/.test(target.pathname)) {
        throw new Error('Invalid local WebUI return URL')
      }
    }
    this.timer = setTimeout(() => this.close(), lifetimeMs)
    this.timer.unref()
    server.unref()
  }
  finish(status: Exclude<LoginResultStatus, 'pending'>): void {
    if (!this.closed && this.status === 'pending') this.status = status
  }
  handle(req: IncomingMessage, res: ServerResponse): boolean {
    const url = new URL(req.url ?? '/', this.origin)
    if (![this.path, `${this.path}/status`, `${this.path}/open`].includes(url.pathname)) return false
    if (this.closed || req.headers.host !== new URL(this.origin).host || url.origin !== this.origin || url.search
      || (req.headers.origin && req.headers.origin !== this.origin)
      || (url.pathname !== this.path && req.headers['sec-fetch-site'] && req.headers['sec-fetch-site'] !== 'same-origin' && req.headers['sec-fetch-site'] !== 'none')) {
      res.writeHead(403).end(); return true
    }
    res.setHeader('Cache-Control', 'no-store')
    res.setHeader('Referrer-Policy', 'no-referrer')
    res.setHeader('X-Content-Type-Options', 'nosniff')
    res.setHeader('X-Frame-Options', 'DENY')
    if (req.method === 'GET' && url.pathname === this.path) {
      const nonce = randomBytes(24).toString('base64url')
      res.setHeader('Content-Security-Policy', `default-src 'none'; style-src 'nonce-${nonce}'; script-src 'nonce-${nonce}'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`)
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' }).end(resultPage(nonce, this.useProtocol, this.webReturnUrl))
    } else if (req.method === 'GET' && url.pathname === `${this.path}/status`) {
      res.writeHead(200, { 'Content-Type': 'application/json' }).end(JSON.stringify({ status: this.status }))
    } else if (req.method === 'POST' && url.pathname === `${this.path}/open` && !this.useProtocol
      && req.headers.origin === this.origin && req.headers['x-opensquilla-login-result'] === '1' && this.status === 'success') {
      void this.openDesktop().then(() => { res.writeHead(204).end() }, () => { res.writeHead(503).end() })
    } else res.writeHead(403).end()
    return true
  }
  close(): void {
    if (this.closed) return
    this.closed = true
    clearTimeout(this.timer)
    this.server.close()
    this.server.closeAllConnections()
  }
}
