export interface BrowserPointerPayload {
  x: number
  y: number
  action: string
  immediate?: boolean
  persistent?: boolean
}

/** Self-contained so CDP can run the same renderer in an isolated world. */
export function browserPointerRenderer(payload: BrowserPointerPayload): void {
  if (!document.documentElement || !Number.isFinite(payload.x) || !Number.isFinite(payload.y)) return
  let host = document.getElementById('__opensquilla-browser-pointer')
  if (host && host.dataset.opensquillaBrowserPointer !== 'true') return
  if (!host) {
    host = document.createElement('div')
    host.id = '__opensquilla-browser-pointer'
    host.dataset.opensquillaBrowserPointer = 'true'
    host.setAttribute('aria-hidden', 'true')
    host.attachShadow({ mode: 'open' })
    document.documentElement.appendChild(host)
  }
  const root = host.shadowRoot
  if (!root) return
  // Direct style assignment works with strict site CSP. The decoration never
  // takes part in hit testing, focus, or the page's accessible content.
  host.style.cssText = 'all:initial!important;position:fixed!important;inset:0!important;z-index:2147483647!important;pointer-events:none!important;overflow:hidden!important;display:block!important;opacity:1!important;transition:opacity 160ms ease-out!important;'
  let cursor = root.querySelector<SVGSVGElement>('svg')
  if (!cursor) {
    cursor = document.createElementNS('http://www.w3.org/2000/svg', 'svg')
    cursor.setAttribute('viewBox', '0 0 28 28')
    cursor.setAttribute('width', '28')
    cursor.setAttribute('height', '28')
    cursor.style.cssText = 'position:absolute;left:0;top:0;pointer-events:none;overflow:visible;filter:drop-shadow(0 1px 1px #0003);transition:transform 100ms ease-out;'
    const arrow = document.createElementNS('http://www.w3.org/2000/svg', 'path')
    arrow.setAttribute('d', 'M2 2 L10 24 L13.5 14 L23 10 Z')
    arrow.setAttribute('fill', '#db7152')
    arrow.setAttribute('stroke', '#fff')
    arrow.setAttribute('stroke-width', '1.5')
    arrow.setAttribute('stroke-linejoin', 'round')
    cursor.appendChild(arrow)
    root.appendChild(cursor)
  }
  let ring = root.querySelector<HTMLDivElement>('div')
  if (!ring) {
    ring = document.createElement('div')
    ring.style.cssText = 'position:absolute;width:18px;height:18px;border:2px solid #db7152;border-radius:50%;box-sizing:border-box;pointer-events:none;opacity:0;transform:translate(-50%,-50%);box-shadow:0 0 0 1px #fff9,inset 0 0 0 1px #fff9;background:#db71521a;'
    root.appendChild(ring)
  }
  const x = Math.round(Math.max(0, Math.min(payload.x, innerWidth - 2)))
  const y = Math.round(Math.max(0, Math.min(payload.y, innerHeight - 2)))
  // Interpolated browser input already supplies the trajectory; a second CSS
  // transition would leave the visible cursor behind the real input position.
  cursor.style.transition = payload.immediate ? 'none' : 'transform 100ms ease-out'
  cursor.style.transform = `translate3d(${x - 2}px,${y - 2}px,0)`
  const arrow = cursor.querySelector('path')!
  arrow.style.transformOrigin = '2px 2px'
  const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)').matches
  if (payload.action === 'down' || payload.action === 'click') {
    for (const animation of arrow.getAnimations()) animation.cancel()
    arrow.style.transform = reducedMotion ? 'none' : 'scale(.82)'
    arrow.style.fill = '#b95036'
  }
  if (payload.action === 'click') {
    arrow.style.transform = 'none'
    arrow.style.fill = '#db7152'
    arrow.animate(reducedMotion ? [
      { fill: '#b95036' }, { fill: '#db7152' },
    ] : [
      { transform: 'scale(.82)', fill: '#b95036', offset: 0 },
      { transform: 'scale(.82)', fill: '#b95036', offset: .18 },
      { transform: 'scale(1.10)', fill: '#db7152', offset: .55 },
      { transform: 'scale(1)', fill: '#db7152', offset: 1 },
    ], { duration: reducedMotion ? 180 : 360, easing: 'ease-out' })
    ring.style.left = `${x}px`
    ring.style.top = `${y}px`
    for (const animation of ring.getAnimations()) animation.cancel()
    ring.animate(reducedMotion ? [
      { opacity: .95 }, { opacity: 0 },
    ] : [
      { opacity: .95, width: '18px', height: '18px' },
      { opacity: 0, width: '46px', height: '46px' },
    ], { duration: reducedMotion ? 180 : 440, easing: 'ease-out' })
  } else if (payload.action === 'cancel') {
    for (const animation of arrow.getAnimations()) animation.cancel()
    arrow.style.transform = 'none'
    arrow.style.fill = '#db7152'
  }
  const token = `${Date.now()}-${Math.random()}`
  host.dataset.pointerToken = token
  if (payload.persistent) return
  const current = host
  setTimeout(() => {
    if (current.dataset.pointerToken !== token) return
    current.style.setProperty('opacity', '0', 'important')
    setTimeout(() => {
      if (current.dataset.pointerToken === token) current.remove()
    }, 180)
  }, 1_350)
}

/** Screenshots must show website pixels without the transient action cursor. */
export function clearBrowserPointer(fade = false): void {
  const host = document.getElementById('__opensquilla-browser-pointer')
  if (host?.dataset.opensquillaBrowserPointer !== 'true') return
  if (!fade) { host.remove(); return }
  const token = `${Date.now()}-${Math.random()}`
  host.dataset.pointerToken = token
  host.style.setProperty('opacity', '0', 'important')
  setTimeout(() => { if (host.dataset.pointerToken === token) host.remove() }, 180)
}
