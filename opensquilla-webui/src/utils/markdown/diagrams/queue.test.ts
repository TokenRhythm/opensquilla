import { describe, expect, it, vi } from 'vitest'
import { DiagramTaskQueue } from './queue'
import { DiagramCache } from './cache'

describe('diagram scheduling and cache', () => {
  it('serializes render/configuration and recovers after a rejected render', async () => {
    const queue = new DiagramTaskQueue()
    let finish!: () => void
    const first = queue.add(() => new Promise(resolve => { finish = () => resolve('first') }))
    const secondRun = vi.fn(async () => { throw new Error('invalid diagram') })
    const second = queue.add(secondRun)
    const third = queue.add(async () => 'third')
    expect(secondRun).not.toHaveBeenCalled()
    finish()
    await expect(first).resolves.toBe('first')
    await expect(second).rejects.toThrow('invalid diagram')
    await expect(third).resolves.toBe('third')
  })

  it('drops cancelled queued work without loading a renderer', async () => {
    const queue = new DiagramTaskQueue(1)
    let finish!: () => void
    const first = queue.add(() => new Promise(resolve => { finish = () => resolve('first') }))
    const controller = new AbortController()
    const run = vi.fn(async () => 'cancelled')
    const queued = queue.add(run, controller.signal)
    controller.abort()
    await expect(queued).rejects.toMatchObject({ name: 'AbortError' })
    const replacement = queue.add(async () => 'replacement')
    await expect(queue.add(async () => 'too much')).rejects.toMatchObject({ code: 'busy' })
    finish()
    await first
    await expect(replacement).resolves.toBe('replacement')
    expect(run).not.toHaveBeenCalled()
  })

  it('does not start a second Mermaid render while a cancelled active render is finishing', async () => {
    const queue = new DiagramTaskQueue()
    const controller = new AbortController()
    let finish!: () => void
    const first = queue.add(() => new Promise(resolve => { finish = () => resolve('discard') }), controller.signal)
    controller.abort()
    await expect(first).rejects.toMatchObject({ name: 'AbortError' })
    const nextRun = vi.fn(async () => 'next')
    const next = queue.add(nextRun)
    expect(nextRun).not.toHaveBeenCalled()
    finish()
    await expect(next).resolves.toBe('next')
  })

  it('notifies blocked producers when a bounded queue gains capacity', async () => {
    const queue = new DiagramTaskQueue(1)
    let finish!: () => void
    const active = queue.add(() => new Promise(resolve => { finish = () => resolve('active') }))
    const queued = queue.add(async () => 'queued')
    const available = vi.fn()
    const capacity = queue.waitForCapacity().then(available)
    await Promise.resolve()
    expect(available).not.toHaveBeenCalled()
    await expect(queue.add(async () => 'overflow')).rejects.toMatchObject({ code: 'busy' })
    finish()
    await capacity
    await expect(queue.add(async () => 'retry')).resolves.toBe('retry')
    expect(available).toHaveBeenCalledOnce()
    await Promise.all([active, queued])
  })

  it('cancels capacity waits and releases a slot when queued work is cancelled', async () => {
    const queue = new DiagramTaskQueue(1)
    let finish!: () => void
    const active = queue.add(() => new Promise(resolve => { finish = () => resolve('active') }))
    const queuedController = new AbortController()
    const queuedRun = vi.fn(async () => 'queued')
    const queued = queue.add(queuedRun, queuedController.signal)
    const waitingController = new AbortController()
    const abandoned = queue.waitForCapacity(waitingController.signal)
    waitingController.abort()
    await expect(abandoned).rejects.toMatchObject({ name: 'AbortError' })
    const available = queue.waitForCapacity()
    queuedController.abort()
    await expect(queued).rejects.toMatchObject({ name: 'AbortError' })
    await available
    const retry = queue.add(async () => 'replacement')
    finish()
    await expect(retry).resolves.toBe('replacement')
    await active
    expect(queuedRun).not.toHaveBeenCalled()
  })

  it('evicts by retained bytes and recency, with distinct theme keys', () => {
    const cache = new DiagramCache(40, 2)
    cache.set('light', 'light svg')
    cache.set('dark', 'dark svg')
    expect(cache.get('light')).toBeUndefined()
    expect(cache.get('dark')).toBe('dark svg')
    cache.set('large', 'x'.repeat(100))
    expect(cache.get('large')).toBeUndefined()
    const lru = new DiagramCache(1000, 2)
    lru.set('a', 'one'); lru.set('b', 'two'); lru.get('a'); lru.set('c', 'three')
    expect(lru.get('b')).toBeUndefined()
    expect(lru.get('a')).toBe('one')
  })
})
