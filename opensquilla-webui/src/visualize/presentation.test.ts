// @vitest-environment happy-dom
import { describe, expect, it } from 'vitest'
import { createApp, h, inject, ref, type Ref } from 'vue'
import { provideVisualizeMessagePins, VISUALIZE_MESSAGE_PIN_KEY, type VisualizeMessagePins } from './presentation'

describe('Visualize message lifetime', () => {
  it('keeps each row pinned until all owners release, and refuses elements from another list', () => {
    let pins!: VisualizeMessagePins
    let keys!: Ref<ReadonlySet<string>>
    const Child = { setup() { pins = inject(VISUALIZE_MESSAGE_PIN_KEY)!; return () => h('div', { 'data-chat-message-key': 'message-42' }, [h('button')]) } }
    const host = document.createElement('div'); document.body.append(host)
    const app = createApp({ setup() {
      const root = ref<HTMLElement | null>(null)
      keys = provideVisualizeMessagePins(root)
      return () => h('div', { ref: root }, [h(Child)])
    } })
    app.mount(host)
    const element = host.querySelector('button')!
    const releaseA = pins.pin(element); const releaseB = pins.pin(element)
    expect([...keys.value]).toEqual(['message-42'])
    releaseA(); releaseA()
    expect([...keys.value]).toEqual(['message-42'])
    releaseB()
    expect(keys.value.size).toBe(0)
    pins.pin(document.createElement('button'))
    expect(keys.value.size).toBe(0)
    pins.pin(element); app.unmount()
    expect(keys.value.size).toBe(0)
    host.remove()
  })
})
