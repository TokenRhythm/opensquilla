import type { WebContents } from 'electron'
import { browserPointerRenderer, clearBrowserPointer, type BrowserPointerPayload } from './browser-pointer.js'

/** Keeps visual state separate from input dispatch and document lifetime. */
export class BrowserPointerController {
  private taskId: string | null = null
  private managed = false
  private touched = false
  private lastTouchedTaskId: string | null = null
  private position: { x: number; y: number } | undefined
  private ready = true
  private disposed = false
  private generation = 0
  private captures = 0
  private queue: Promise<void> = Promise.resolve()

  constructor(private readonly contents: WebContents, private readonly visible: () => boolean) {}

  async setTask(taskId: string | null): Promise<void> {
    const wasManaged = this.managed
    const keepTouched = taskId !== null && (taskId === this.lastTouchedTaskId || (!wasManaged && this.touched))
    this.managed = true
    if (taskId === this.taskId && wasManaged) return
    this.generation++
    this.taskId = taskId
    this.touched = keepTouched
    if (keepTouched) this.lastTouchedTaskId = taskId
    await this.clear(taskId === null)
    if (keepTouched) await this.sync()
  }

  async touch(): Promise<void> {
    if (this.touched) return
    this.touched = true
    if (this.taskId) this.lastTouchedTaskId = this.taskId
    await this.sync()
  }

  private persistent(): boolean { return this.taskId !== null && this.touched }

  private canShow(): boolean {
    return !this.disposed && this.ready && this.captures === 0
      && !this.contents.isDestroyed() && this.visible()
      && (!this.managed || this.persistent())
  }

  private enqueue(work: () => Promise<void>): Promise<void> {
    const result = this.queue.then(work).catch(() => {})
    this.queue = result
    return result
  }

  private async evaluate(code: string): Promise<void> {
    if (this.contents.isDestroyed()) return
    // An isolated world shares DOM decoration without exposing privileged state.
    await this.contents.executeJavaScriptInIsolatedWorld(1004, [{ code }])
  }

  private clear(fade = false): Promise<void> {
    return this.enqueue(() => this.evaluate(`(${clearBrowserPointer.toString()})(${fade})`))
  }

  async update(payload: BrowserPointerPayload): Promise<void> {
    this.position = { x: payload.x, y: payload.y }
    const generation = this.generation
    await this.enqueue(async () => {
      if (generation !== this.generation) return
      if (!this.canShow()) {
        await this.evaluate(`(${clearBrowserPointer.toString()})()`)
        return
      }
      await this.evaluate(`(${browserPointerRenderer.toString()})(${JSON.stringify({
        ...payload, persistent: this.persistent(),
      })})`)
    })
  }

  async sync(): Promise<void> {
    if (this.persistent() && this.canShow()) {
      if (!this.position) {
        await this.enqueue(async () => {
          if (this.position || !this.canShow()) return
          const center = await this.contents.executeJavaScriptInIsolatedWorld(1004, [{
            code: '({ x: innerWidth / 2, y: innerHeight / 2 })',
          }]) as { x: number; y: number }
          if (!this.position && Number.isFinite(center?.x) && Number.isFinite(center?.y)) this.position = center
        })
      }
      if (this.position) await this.update({ ...this.position, action: 'idle', immediate: true })
    } else await this.clear()
  }

  currentPosition(): Readonly<{ x: number; y: number }> | undefined { return this.position }

  navigationStarted(): void {
    this.generation++
    this.ready = false
    void this.clear()
  }

  async documentReady(): Promise<void> {
    this.ready = true
    await this.sync()
  }

  async pauseForScreenshot<T>(capture: () => Promise<T>): Promise<T> {
    this.captures++
    try {
      await this.clear()
      return await capture()
    } finally {
      this.captures--
      await this.sync()
    }
  }

  async dispose(): Promise<void> {
    this.disposed = true
    this.taskId = null
    this.touched = false
    this.lastTouchedTaskId = null
    await this.clear()
  }
}
