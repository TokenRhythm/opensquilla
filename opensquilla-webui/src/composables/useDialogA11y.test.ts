// @vitest-environment happy-dom
import { afterEach, describe, expect, it } from 'vitest'
import { createApp, defineComponent, h, nextTick, ref } from 'vue'

import { hasOpenDialogLayer, useDialogA11y, useDialogLayer } from './useDialogA11y'

describe('useDialogA11y modal stack', () => {
  let app: ReturnType<typeof createApp> | null = null

  afterEach(() => {
    app?.unmount()
    app = null
    document.body.innerHTML = ''
  })

  it('lets only the topmost dialog own Tab and Escape, then restores each invoker', async () => {
    const Host = defineComponent({
      setup() {
        const lowerOpen = ref(false)
        const upperOpen = ref(false)
        const lowerRoot = ref<HTMLElement | null>(null)
        const upperRoot = ref<HTMLElement | null>(null)

        useDialogA11y(lowerRoot, lowerOpen, () => { lowerOpen.value = false })
        useDialogA11y(upperRoot, upperOpen, () => { upperOpen.value = false })

        return () => h('div', [
          h('button', {
            id: 'lower-trigger',
            onClick: () => { lowerOpen.value = true },
          }, 'Open lower'),
          lowerOpen.value
            ? h('section', { ref: lowerRoot, role: 'dialog', 'aria-label': 'Lower' }, [
                h('button', {
                  id: 'upper-trigger',
                  onClick: () => { upperOpen.value = true },
                }, 'Open upper'),
                h('button', { id: 'lower-last' }, 'Lower last'),
              ])
            : null,
          upperOpen.value
            ? h('section', { ref: upperRoot, role: 'dialog', 'aria-label': 'Upper' }, [
                h('button', { id: 'upper-first' }, 'Upper first'),
                h('button', { id: 'upper-last' }, 'Upper last'),
              ])
            : null,
        ])
      },
    })

    const root = document.createElement('div')
    document.body.appendChild(root)
    app = createApp(Host)
    app.mount(root)

    const lowerTrigger = document.querySelector<HTMLButtonElement>('#lower-trigger')!
    lowerTrigger.focus()
    lowerTrigger.click()
    await nextTick()
    await nextTick()
    const upperTrigger = document.querySelector<HTMLButtonElement>('#upper-trigger')!
    expect(document.activeElement).toBe(upperTrigger)

    upperTrigger.click()
    await nextTick()
    await nextTick()
    const upperFirst = document.querySelector<HTMLButtonElement>('#upper-first')!
    expect(document.activeElement).toBe(upperFirst)

    // If the lower trap were still active, Tab from its last button would wrap
    // inside the lower dialog before the upper dialog could handle the event.
    document.querySelector<HTMLButtonElement>('#lower-last')!.focus()
    const tab = new KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true })
    document.dispatchEvent(tab)
    expect(tab.defaultPrevented).toBe(true)
    expect(document.activeElement).toBe(upperFirst)

    document.dispatchEvent(new KeyboardEvent('keydown', {
      key: 'Escape', bubbles: true, cancelable: true,
    }))
    await nextTick()
    expect(document.querySelector('[aria-label="Upper"]')).toBeNull()
    expect(document.querySelector('[aria-label="Lower"]')).toBeTruthy()
    expect(document.activeElement).toBe(upperTrigger)

    document.dispatchEvent(new KeyboardEvent('keydown', {
      key: 'Escape', bubbles: true, cancelable: true,
    }))
    await nextTick()
    expect(document.querySelector('[aria-label="Lower"]')).toBeNull()
    expect(document.activeElement).toBe(lowerTrigger)
  })

  it('shares ownership with components that implement their own dialog keyboard handling', async () => {
    let customTopmost = false
    const Host = defineComponent({
      setup() {
        const standardOpen = ref(true)
        const customOpen = ref(false)
        const standardRoot = ref<HTMLElement | null>(null)
        useDialogA11y(standardRoot, standardOpen, () => { standardOpen.value = false })
        const customIsTopmost = useDialogLayer(customOpen)
        return () => {
          customTopmost = customIsTopmost.value
          return h('div', [
            standardOpen.value ? h('section', { ref: standardRoot }, [h('button', 'standard')]) : null,
            h('button', { id: 'open-custom', onClick: () => { customOpen.value = true } }, 'custom'),
            customOpen.value ? h('section', { id: 'custom' }, 'custom layer') : null,
          ])
        }
      },
    })

    const root = document.createElement('div')
    document.body.appendChild(root)
    app = createApp(Host)
    app.mount(root)
    await nextTick()
    expect(hasOpenDialogLayer()).toBe(true)
    document.querySelector<HTMLButtonElement>('#open-custom')!.click()
    await nextTick()
    expect(customTopmost).toBe(true)
    expect(hasOpenDialogLayer()).toBe(true)

    app.unmount()
    app = null
    expect(hasOpenDialogLayer()).toBe(false)
  })

  it('reactively transfers ownership between two custom layers after the lower value was cached', async () => {
    let lowerTopmost = false
    let upperTopmost = false
    const Host = defineComponent({
      setup() {
        const lowerOpen = ref(true)
        const upperOpen = ref(false)
        const lower = useDialogLayer(lowerOpen)
        const upper = useDialogLayer(upperOpen)
        return () => {
          lowerTopmost = lower.value
          upperTopmost = upper.value
          return h('div', [
            h('button', { id: 'open-upper-custom', onClick: () => { upperOpen.value = true } }, 'open'),
            h('button', { id: 'close-upper-custom', onClick: () => { upperOpen.value = false } }, 'close'),
          ])
        }
      },
    })

    const root = document.createElement('div')
    document.body.appendChild(root)
    app = createApp(Host)
    app.mount(root)
    await nextTick()
    expect(lowerTopmost).toBe(true)
    expect(upperTopmost).toBe(false)

    document.querySelector<HTMLButtonElement>('#open-upper-custom')!.click()
    await nextTick()
    expect(lowerTopmost).toBe(false)
    expect(upperTopmost).toBe(true)

    document.querySelector<HTMLButtonElement>('#close-upper-custom')!.click()
    await nextTick()
    expect(lowerTopmost).toBe(true)
    expect(upperTopmost).toBe(false)
  })

  it('invalidates a cached custom layer when a standard dialog opens above it', async () => {
    let customTopmost = false
    const Host = defineComponent({
      setup() {
        const customOpen = ref(true)
        const standardOpen = ref(false)
        const standardRoot = ref<HTMLElement | null>(null)
        const custom = useDialogLayer(customOpen)
        useDialogA11y(standardRoot, standardOpen, () => { standardOpen.value = false })
        return () => {
          customTopmost = custom.value
          return h('div', [
            h('button', { id: 'open-standard', onClick: () => { standardOpen.value = true } }, 'open'),
            standardOpen.value
              ? h('section', { ref: standardRoot }, [h('button', 'standard')])
              : null,
          ])
        }
      },
    })

    const root = document.createElement('div')
    document.body.appendChild(root)
    app = createApp(Host)
    app.mount(root)
    await nextTick()
    expect(customTopmost).toBe(true)
    document.querySelector<HTMLButtonElement>('#open-standard')!.click()
    await nextTick()
    await nextTick()
    expect(customTopmost).toBe(false)
  })
})
