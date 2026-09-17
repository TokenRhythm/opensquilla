import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { mkdir, writeFile } from 'node:fs/promises'
import { join } from 'node:path'

/** Capture the existing page at two CSS widths, including every scroll segment. */
export async function capturePageVisualEvidence(app, { pageId, outputDirectory, stage }) {
  assert.ok(Number.isSafeInteger(pageId) && pageId > 0, 'VISUAL_TARGET_REQUIRED')
  assert.match(stage, /^[a-z][a-z0-9-]{0,63}$/)
  const results = []
  await mkdir(outputDirectory, { recursive: true, mode: 0o700 })
  for (const viewport of [
    { label: 'wide', width: 1280, height: 900 },
    { label: 'narrow', width: 390, height: 844 },
  ]) {
    const result = await app.evaluate(async ({ webContents }, request) => {
      const contents = webContents.fromId(request.pageId)
      if (!contents || contents.isDestroyed()) throw new Error('VISUAL_TARGET_LOST')
      const identity = contents.getURL()
      const prior = await contents.executeJavaScript('({x:scrollX,y:scrollY,width:innerWidth,height:innerHeight})')
      const attachedHere = !contents.debugger.isAttached()
      if (attachedHere) contents.debugger.attach('1.3')
      const frames = []
      let complete = false
      try {
        await contents.debugger.sendCommand('Emulation.setDeviceMetricsOverride', {
          width: request.width, height: request.height, deviceScaleFactor: 1, mobile: false,
        })
        let requestedTop = 0
        for (let index = 0; index < 32; index += 1) {
          if (contents.isDestroyed() || contents.getURL() !== identity) throw new Error('VISUAL_TARGET_CHANGED')
          await contents.executeJavaScript(`scrollTo({left:0,top:${requestedTop},behavior:'instant'});new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))`)
          // Allow ordinary entrance animations to settle after real scrolling.
          await new Promise(resolve => setTimeout(resolve, 450))
          const geometry = await contents.executeJavaScript(`({width:innerWidth,height:innerHeight,x:scrollX,y:scrollY,scrollWidth:document.documentElement.scrollWidth,scrollHeight:Math.max(document.body?.scrollHeight||0,document.documentElement.scrollHeight),visibleHeadings:[...document.querySelectorAll('h1,h2,h3,[role=heading]')].filter(e=>{const r=e.getBoundingClientRect();return r.bottom>0&&r.top<innerHeight&&r.width>0}).map(e=>(e.innerText||'').slice(0,200))})`)
          const screenshot = await contents.debugger.sendCommand('Page.captureScreenshot', {
            format: 'png', fromSurface: true, captureBeyondViewport: false,
          })
          frames.push({ index, ...geometry, png: screenshot.data })
          if (geometry.y + geometry.height >= geometry.scrollHeight - 1) { complete = true; break }
          const next = Math.min(geometry.y + Math.floor(geometry.height * 0.8), geometry.scrollHeight - geometry.height)
          if (next <= geometry.y) break
          requestedTop = next
        }
      } finally {
        try {
          if (!contents.isDestroyed() && contents.debugger.isAttached()) {
            await contents.debugger.sendCommand('Emulation.clearDeviceMetricsOverride')
            if (contents.getURL() === identity) {
              await contents.executeJavaScript(`scrollTo({left:${prior.x},top:${prior.y},behavior:'instant'});new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))`)
            }
          }
        } finally {
          if (attachedHere && !contents.isDestroyed() && contents.debugger.isAttached()) contents.debugger.detach()
        }
      }
      const restored = await contents.executeJavaScript('({x:scrollX,y:scrollY,width:innerWidth,height:innerHeight})')
      return { pageId: contents.id, complete, prior, restored, frames }
    }, { pageId, ...viewport })
    const frames = []
    for (const { png, ...frame } of result.frames) {
      const data = Buffer.from(png, 'base64')
      assert.ok(data.subarray(0, 8).equals(Buffer.from('89504e470d0a1a0a', 'hex')), 'VISUAL_INVALID_PNG')
      const pixelWidth = data.readUInt32BE(16)
      const pixelHeight = data.readUInt32BE(20)
      assert.equal(frame.width, viewport.width, 'VISUAL_CSS_WIDTH_MISMATCH')
      assert.equal(pixelWidth, viewport.width, 'VISUAL_PIXEL_WIDTH_MISMATCH')
      assert.equal(pixelHeight, viewport.height, 'VISUAL_PIXEL_HEIGHT_MISMATCH')
      const filename = `${stage}-${viewport.label}-${String(frame.index + 1).padStart(2, '0')}.png`
      await writeFile(join(outputDirectory, filename), data, { mode: 0o600 })
      frames.push({ ...frame, pixelWidth, pixelHeight, screenshot: filename, sha256: createHash('sha256').update(data).digest('hex') })
    }
    results.push({
      ...viewport, pageId, complete: result.complete,
      capture: 'existing WebContents; CSS viewport emulation and actual scrolling; deviceScaleFactor=1; no reload',
      prior: result.prior, restored: result.restored,
      restorationMatches: Object.keys(result.prior).every(key => Math.abs(result.prior[key] - result.restored[key]) <= 1),
      horizontalOverflow: frames.some(frame => frame.scrollWidth > frame.width + 1), frames,
    })
  }
  return results
}
