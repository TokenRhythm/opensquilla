import { DiagramRenderError, throwIfAborted } from './types'

interface QueueEntry {
  run: () => Promise<string>
  resolve: (value: string) => void
  reject: (reason: unknown) => void
  signal?: AbortSignal
  abort: () => void
}

// Mermaid has process-global configuration. Keep initialize/render atomic,
// and bound retained sources when a long conversation becomes visible.
export class DiagramTaskQueue {
  private entries: QueueEntry[] = []
  private active = false
  private capacityWaiters = new Set<() => void>()

  constructor(private readonly maxPending = 12) {}

  // A visible block owns its retry; the execution queue never retains more
  // source/render closures than maxPending. Visibility loss cancels this wait.
  waitForCapacity(signal?: AbortSignal): Promise<void> {
    throwIfAborted(signal)
    if (this.entries.length < this.maxPending) return Promise.resolve()
    return new Promise<void>((resolve, reject) => {
      const clear = () => {
        this.capacityWaiters.delete(available)
        signal?.removeEventListener('abort', abort)
      }
      const available = () => { clear(); resolve() }
      const abort = () => { clear(); reject(new DOMException('Diagram rendering cancelled', 'AbortError')) }
      this.capacityWaiters.add(available)
      signal?.addEventListener('abort', abort, { once: true })
    })
  }

  private notifyCapacity(): void {
    const slots = this.maxPending - this.entries.length
    if (slots <= 0) return
    const available = [...this.capacityWaiters].slice(0, slots)
    for (const notify of available) notify()
    // Let the notified blocks enqueue first. If a block was cancelled between
    // notification and retry, offer its unused slot to the next waiter.
    if (available.length && this.capacityWaiters.size) queueMicrotask(() => this.notifyCapacity())
  }

  add(run: () => Promise<string>, signal?: AbortSignal): Promise<string> {
    throwIfAborted(signal)
    if (this.entries.length >= this.maxPending) return Promise.reject(new DiagramRenderError('busy'))
    return new Promise<string>((resolve, reject) => {
      const entry: QueueEntry = { run, resolve, reject, signal, abort: () => {} }
      entry.abort = () => {
        this.entries = this.entries.filter(item => item !== entry)
        reject(new DOMException('Diagram rendering cancelled', 'AbortError'))
        this.notifyCapacity()
      }
      signal?.addEventListener('abort', entry.abort, { once: true })
      this.entries.push(entry)
      void this.pump()
    })
  }

  private async pump(): Promise<void> {
    if (this.active) return
    const entry = this.entries.shift()
    if (!entry) { this.notifyCapacity(); return }
    this.active = true
    this.notifyCapacity()
    try {
      throwIfAborted(entry.signal)
      const result = await entry.run()
      throwIfAborted(entry.signal)
      entry.resolve(result)
    } catch (error) {
      entry.reject(error)
    } finally {
      entry.signal?.removeEventListener('abort', entry.abort)
      this.active = false
      void this.pump()
    }
  }
}
