// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick } from 'vue'
import i18n from '@/i18n'
import zhHans from '@/locales/zh-Hans.json'
import SetupModelCombobox from './SetupModelCombobox.vue'
import type { DiscoveredModel } from '@/composables/setup/useSetupProviderForm'

const MODELS: DiscoveredModel[] = [
  {
    id: 'test-vendor/alpha',
    name: 'Alpha',
    contextWindow: 262144,
    maxOutputTokens: 16384,
    capabilities: ['chat', 'tools'],
    pricing: null,
    capabilitySource: 'provider',
  },
  {
    id: 'test-vendor/beta-vision',
    name: 'Beta Vision',
    contextWindow: 128000,
    maxOutputTokens: null,
    capabilities: ['chat', 'vision'],
    pricing: null,
    capabilitySource: 'synthesized',
  },
]

const PUBLISHED_METADATA: NonNullable<DiscoveredModel['metadata']> = {
  schemaVersion: 1,
  published: {
    name: 'Alpha Official',
    providerDisplayName: 'Test Vendor',
    modelType: 'chat',
    status: 'available',
    modalities: ['text'],
    contextWindow: 1048576,
    maxOutputTokens: 131072,
    reasoningMode: 'hybrid',
    reasoningDefault: 'provider',
    reasoningSupportedEfforts: [],
    reasoningSupportsMaxTokens: false,
    capabilities: {
      tools: true,
      reasoning: true,
      vision: false,
      anthropic: false,
      responses: true,
      streaming: true,
    },
    responses: null,
    pricing: null,
  },
  declared: null,
}

const AUTH_ONLY_METADATA: NonNullable<DiscoveredModel['metadata']> = {
  schemaVersion: 1,
  published: null,
  declared: {
    displayName: 'Alpha Preview',
    modelType: 'chat',
    status: 'testing',
    contextWindow: 262144,
    maxOutputTokens: 16384,
    capabilities: {
      tools: true,
      reasoning: true,
      vision: false,
      anthropic: false,
      responses: true,
      streaming: true,
    },
    responses: null,
    pricing: null,
  },
}

const FIELD = { name: 'model', label: 'Model' }

function makeModel(id: string, capabilitySource = 'provider'): DiscoveredModel {
  return {
    id,
    name: id,
    contextWindow: 32768,
    maxOutputTokens: null,
    capabilities: ['chat'],
    pricing: null,
    capabilitySource,
  }
}

const mountedApps: ReturnType<typeof createApp>[] = []

async function mountCombobox(props: Record<string, unknown> = {}) {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(SetupModelCombobox, {
    field: FIELD,
    value: '',
    models: MODELS,
    modelSource: 'live',
    ...props,
  })
  app.use(i18n)
  app.mount(el)
  mountedApps.push(app)
  await nextTick()
  return { el }
}

async function openList(el: HTMLElement) {
  const input = el.querySelector<HTMLInputElement>('input[role="combobox"]')!
  input.dispatchEvent(new Event('focus'))
  await nextTick()
  return input
}

// The listbox is teleported to <body>, so its DOM lives outside the mount el.
function listbox(): HTMLElement | null {
  return document.querySelector<HTMLElement>('[role="listbox"]')
}

function popup(): HTMLElement | null {
  return document.querySelector<HTMLElement>('.setup-model-combobox__popup')
}

function optionRows(): HTMLButtonElement[] {
  return Array.from(document.querySelectorAll<HTMLButtonElement>('[role="option"]'))
}

function footerRows(): HTMLElement[] {
  return Array.from(document.querySelectorAll<HTMLElement>('.setup-model-combobox__footer'))
}

function stubRect(input: HTMLInputElement, rect: Partial<DOMRect>) {
  input.getBoundingClientRect = () =>
    ({ x: 0, y: 0, top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0, toJSON: () => ({}), ...rect }) as DOMRect
}

beforeEach(() => {
  i18n.global.locale.value = 'en'
})

afterEach(() => {
  // Unmount here, not per-test, so a failed assertion cannot leak a live app
  // (and its focus/blur handlers) into the next test.
  mountedApps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
})

describe('SetupModelCombobox', () => {
  it('is a plain text input until opened — free text always works', async () => {
    const { el } = await mountCombobox({ value: 'my/custom-model' })
    const input = el.querySelector<HTMLInputElement>('input[name="setup_provider_model"]')
    expect(input?.value).toBe('my/custom-model')
    expect(listbox()).toBeNull()
  })

  it('opens a curated fallback catalog without claiming it is live', async () => {
    const { el } = await mountCombobox({ modelSource: 'catalog' })
    await openList(el)

    expect(optionRows()).toHaveLength(2)
    expect(popup()?.textContent).not.toContain('Live')
  })

  it('pins an optional semantic choice above models and supports keyboard selection', async () => {
    const onUpdate = vi.fn()
    const leadingOption = {
      value: '__ensemble__',
      label: 'Multi-model fusion',
      description: 'Use the shared plan',
    }
    const { el } = await mountCombobox({
      value: '__ensemble__',
      leadingOption,
      onUpdate,
    })

    const input = await openList(el)
    expect(input.value).toBe('Multi-model fusion')
    const rows = optionRows()
    expect(rows[0]?.textContent).toContain('Multi-model fusion')
    expect(rows[0]?.getAttribute('aria-selected')).toBe('true')
    expect(rows[1]?.textContent).toContain('test-vendor/alpha')
    expect(rows).toHaveLength(3)
    expect(listbox()?.textContent).not.toContain('__ensemble__')

    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown' }))
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter' }))
    await nextTick()
    expect(onUpdate).toHaveBeenCalledWith('__ensemble__')
  })

  it('keeps the semantic choice available without a discovered model catalog', async () => {
    const { el } = await mountCombobox({
      models: [],
      modelSource: 'none',
      leadingOption: { value: '__ensemble__', label: 'Multi-model fusion' },
    })

    await openList(el)
    expect(optionRows()).toHaveLength(1)
    expect(optionRows()[0]?.textContent).toContain('Multi-model fusion')
  })

  it('opts out of password-manager field classification', async () => {
    const { el } = await mountCombobox()
    const input = el.querySelector<HTMLInputElement>('input[name="setup_provider_model"]')

    expect(input?.getAttribute('autocomplete')).toBe('one-time-code')
    expect(input?.getAttribute('data-form-type')).toBe('other')
    expect(input?.getAttribute('data-lpignore')).toBe('true')
  })

  it('advertises model search and custom-id entry when no field placeholder is provided', async () => {
    const { el } = await mountCombobox()
    const input = el.querySelector<HTMLInputElement>('input[role="combobox"]')

    expect(input?.placeholder).toBe('Search models or enter a custom ID')
  })

  it('associates field help and live catalog context with the input', async () => {
    const { el } = await mountCombobox({
      field: { ...FIELD, description: 'Used for direct requests and fallback.' },
      externalDescriptionId: 'external-model-status',
    })
    const input = el.querySelector<HTMLInputElement>('input[name="setup_provider_model"]')
    const description = el.querySelector<HTMLElement>('#setup-provider-model-description')

    expect(description?.textContent).toBe('Used for direct requests and fallback.')
    expect(input?.getAttribute('aria-describedby'))
      .toBe('setup-provider-model-description external-model-status setup-provider-model-catalog-count')
  })

  it('keeps field help associated when no live catalog is available', async () => {
    const { el } = await mountCombobox({
      field: { ...FIELD, description: 'Enter a provider model ID.' },
      models: [],
      modelSource: 'none',
    })
    const input = el.querySelector<HTMLInputElement>('input[name="setup_provider_model"]')

    expect(input?.getAttribute('aria-describedby')).toBe('setup-provider-model-description')
  })

  it('lists compact context and output metrics without capability clutter', async () => {
    const { el } = await mountCombobox()
    await openList(el)

    const rows = optionRows()
    expect(rows).toHaveLength(2)
    expect(rows[0].textContent).toContain('test-vendor/alpha')
    expect(rows[0].textContent).toContain('Alpha')
    expect(rows[0].textContent).toContain('Context')
    expect(rows[0].textContent).toContain('262k')
    expect(rows[0].textContent).toContain('Output')
    expect(rows[0].textContent).toContain('16k')
    expect(rows[0].textContent).not.toContain('tools')
    expect(rows[0].textContent).not.toContain('chat')
    expect(rows[1].textContent).toContain('Context')
    expect(rows[1].textContent).toContain('128k')
    expect(rows[1].textContent).not.toContain('Output')
    expect(rows[1].querySelector('.setup-model-combobox__meta')?.textContent)
      .not.toContain('vision')
    expect(rows[1].querySelectorAll('.setup-model-combobox__metric-separator')).toHaveLength(0)

    const metricSemantics = Array.from(
      rows[0].querySelectorAll<HTMLElement>('.setup-model-combobox__metric'),
      metric => ({
        title: metric.title,
        screenReaderText: metric.querySelector('.setup-model-combobox__sr-only')?.textContent?.trim(),
      }),
    )
    expect(metricSemantics).toEqual([
      { title: 'Catalog context 262k', screenReaderText: 'Catalog context 262k' },
      { title: 'Catalog max output 16k', screenReaderText: 'Catalog max output 16k' },
    ])
  })

  it('prefers published limits, suppresses normal status, and labels accessible sources', async () => {
    const { el } = await mountCombobox({
      models: [
        { ...MODELS[0], metadata: PUBLISHED_METADATA },
        MODELS[1],
      ],
    })
    await openList(el)

    const rows = optionRows()
    expect(rows[0].textContent).toContain('Context')
    expect(rows[0].textContent).toContain('1M')
    expect(rows[0].textContent).toContain('Output')
    expect(rows[0].textContent).toContain('131k')
    expect(rows[0].textContent).not.toContain('available')
    expect(rows[0].querySelector('.setup-model-combobox__badge--status')).toBeNull()
    expect(Array.from(
      rows[0].querySelectorAll<HTMLElement>('.setup-model-combobox__metric'),
      metric => metric.querySelector('.setup-model-combobox__sr-only')?.textContent?.trim(),
    )).toEqual(['Official context 1M', 'Official max output 131k'])
    expect(rows[1].querySelector<HTMLElement>('.setup-model-combobox__metric')?.title)
      .toBe('Catalog context 128k')
    expect(footerRows().map(row => row.textContent).join(' '))
      .toContain('official values when available')
  })

  it('preserves per-field source semantics when only one published limit is available', async () => {
    const mixedMetadata: NonNullable<DiscoveredModel['metadata']> = {
      ...PUBLISHED_METADATA,
      published: {
        ...PUBLISHED_METADATA.published!,
        maxOutputTokens: null,
      },
    }
    const { el } = await mountCombobox({
      models: [{ ...MODELS[0], metadata: mixedMetadata }],
    })
    await openList(el)

    expect(Array.from(
      optionRows()[0].querySelectorAll<HTMLElement>('.setup-model-combobox__metric'),
      metric => ({
        title: metric.title,
        screenReaderText: metric.querySelector('.setup-model-combobox__sr-only')?.textContent?.trim(),
      }),
    )).toEqual([
      { title: 'Official context 1M', screenReaderText: 'Official context 1M' },
      { title: 'Catalog max output 16k', screenReaderText: 'Catalog max output 16k' },
    ])
  })

  it('marks auth-only testing models without repeating ordinary metadata', async () => {
    const { el } = await mountCombobox({
      models: [{ ...MODELS[0], metadata: AUTH_ONLY_METADATA }],
    })
    await openList(el)

    const row = optionRows()[0]
    expect(row.querySelector('.setup-model-combobox__badge--status')?.textContent).toContain('Testing')
    expect(row.querySelector('.setup-model-combobox__badge--catalog')?.textContent)
      .toContain('Catalog value')
    expect(row.textContent).not.toContain('tools')
    expect(Array.from(
      row.querySelectorAll<HTMLElement>('.setup-model-combobox__metric'),
      metric => metric.querySelector('.setup-model-combobox__sr-only')?.textContent?.trim(),
    )).toEqual(['Catalog context 262k', 'Catalog max output 16k'])
  })

  it('keeps the model count in the catalog header instead of the input chrome', async () => {
    const { el } = await mountCombobox()
    const trigger = el.querySelector<HTMLButtonElement>('[data-testid="setup-model-options-toggle"]')

    expect(trigger?.textContent?.trim()).toBe('')
    expect(trigger?.querySelector('.setup-model-combobox__count')).toBeNull()
    expect(trigger?.getAttribute('aria-label')).toBe('Model catalog · 2')

    await openList(el)

    const readout = document.querySelector('.setup-model-combobox__readout')?.textContent
    expect(readout).toContain('Available · 2')
    expect(readout).toContain('Live')
  })

  it('describes a live catalog as real-time in Simplified Chinese', async () => {
    i18n.global.setLocaleMessage('zh-Hans', zhHans)
    i18n.global.locale.value = 'zh-Hans'
    const { el } = await mountCombobox()

    await openList(el)

    const readout = document.querySelector('.setup-model-combobox__readout')?.textContent
    expect(readout).toContain('实时')
    expect(readout).not.toContain('已生效')
  })

  it('teleports the open list to the body so scrolling panels cannot clip it', async () => {
    const { el } = await mountCombobox()
    await openList(el)

    const layer = popup()!
    expect(layer.parentElement).toBe(document.body)
    expect(el.contains(layer)).toBe(false)
  })

  it('opens a readable wide catalog even when the table model cell is narrow', async () => {
    const { el } = await mountCombobox()
    const input = el.querySelector<HTMLInputElement>('input[role="combobox"]')!
    stubRect(input, { top: 100, bottom: 132, left: 320, right: 580, width: 260, height: 32 })

    input.dispatchEvent(new Event('focus'))
    await nextTick()

    expect(popup()!.style.width).toBe('480px')
  })

  it('flips the list above the input when the space below is too short', async () => {
    const { el } = await mountCombobox()
    const input = el.querySelector<HTMLInputElement>('input[role="combobox"]')!
    // Input sits near the bottom of the (happy-dom default 768px) viewport.
    stubRect(input, { top: 700, bottom: 724, left: 100, right: 400, width: 300, height: 24 })
    input.dispatchEvent(new Event('focus'))
    await nextTick()

    const layer = popup()!
    expect(layer.style.bottom).not.toBe('auto')
    expect(layer.style.bottom).not.toBe('')
    expect(layer.style.top).toBe('auto')
  })

  it('shows the full list on focus even when the field holds a saved model id', async () => {
    // Regression: filtering against the pre-filled value on open used to hide
    // every other discovered model behind an exact match.
    const { el } = await mountCombobox({ value: 'test-vendor/alpha' })
    await openList(el)

    const rows = optionRows()
    expect(rows).toHaveLength(2) // both models, no escape row for an exact id
    expect(rows[0].getAttribute('aria-selected')).toBe('true')
    expect(rows[1].textContent).toContain('test-vendor/beta-vision')
  })

  it('marks the selected model with a visible check affordance', async () => {
    const { el } = await mountCombobox({ value: 'test-vendor/alpha' })
    await openList(el)

    const selected = document.querySelector<HTMLElement>('[role="option"][aria-selected="true"]')
    const selectedMark = selected?.querySelector('.setup-model-combobox__selected')
    expect(selectedMark?.querySelector('svg')).toBeTruthy()
    expect(selectedMark?.querySelector('.setup-model-combobox__sr-only')?.textContent)
      .toContain('Selected')
  })

  it('pins the saved model to the top when the list exceeds the visible window', async () => {
    // Regression: with >MAX_ROWS discovered models, a saved id sorted past the
    // window used to be truncated out of the dropdown entirely.
    const many = Array.from({ length: 60 }, (_, i) => makeModel(`test-vendor/bulk-${i}`))
    const { el } = await mountCombobox({ models: [...many, makeModel('test-vendor/omega')], value: 'test-vendor/omega' })
    await openList(el)

    const rows = optionRows()
    expect(rows).toHaveLength(40) // MAX_ROWS window
    expect(rows[0].textContent).toContain('test-vendor/omega')
    expect(rows[0].getAttribute('aria-selected')).toBe('true')
    expect(footerRows().some(f => (f.textContent || '').includes('Showing 40 of 61'))).toBe(true)
  })

  it('filters rows once the user types and offers a free-text escape row', async () => {
    const { el } = await mountCombobox({ value: 'beta' })
    const input = await openList(el)
    input.dispatchEvent(new Event('input'))
    await nextTick()

    const rows = optionRows()
    // one match + the "use what you typed" escape row
    expect(rows).toHaveLength(2)
    expect(rows[0].textContent).toContain('test-vendor/beta-vision')
    expect(rows[1].textContent).toContain('Use "beta"')
  })

  it('drops the filter again on the next open after blur', async () => {
    const { el } = await mountCombobox({ value: 'beta' })
    const input = await openList(el)
    input.dispatchEvent(new Event('input'))
    await nextTick()
    expect(optionRows()).toHaveLength(2) // filtered + escape row

    input.dispatchEvent(new Event('blur'))
    await nextTick()
    await openList(el)

    expect(optionRows()).toHaveLength(3) // full list again + escape row for "beta"
  })

  it('emits update with the model id when a row is clicked and closes the list', async () => {
    const onUpdate = vi.fn()
    const { el } = await mountCombobox({ onUpdate })
    await openList(el)

    optionRows()[1].click()
    await nextTick()

    expect(onUpdate).toHaveBeenCalledWith('test-vendor/beta-vision')
    expect(listbox()).toBeNull()
  })

  it('reopens the full list when the still-focused input is clicked again', async () => {
    // Row clicks keep DOM focus on the input (mousedown is prevented), so no
    // new `focus` event will fire — the click handler must reopen the list.
    const { el } = await mountCombobox()
    const input = await openList(el)
    optionRows()[0].click()
    await nextTick()
    expect(listbox()).toBeNull()

    input.dispatchEvent(new MouseEvent('click'))
    await nextTick()
    expect(optionRows()).toHaveLength(2) // full list again
  })

  it('consumes Escape while the list is open and lets it bubble once closed', async () => {
    const { el } = await mountCombobox()
    const input = await openList(el)

    const whileOpen = new KeyboardEvent('keydown', { key: 'Escape', cancelable: true, bubbles: true })
    input.dispatchEvent(whileOpen)
    await nextTick()
    expect(listbox()).toBeNull()
    expect(whileOpen.defaultPrevented).toBe(true) // dropdown-only dismiss

    const whileClosed = new KeyboardEvent('keydown', { key: 'Escape', cancelable: true, bubbles: true })
    input.dispatchEvent(whileClosed)
    await nextTick()
    expect(whileClosed.defaultPrevented).toBe(false) // enclosing dialog may close
  })

  it('clicking the free-text escape row keeps the typed value and just closes', async () => {
    const onUpdate = vi.fn()
    const { el } = await mountCombobox({ value: 'my/custom-model', onUpdate })
    await openList(el)

    const rows = optionRows()
    const escapeRow = rows[rows.length - 1]
    expect(escapeRow.textContent).toContain('Use "my/custom-model"')
    escapeRow.click()
    await nextTick()

    expect(onUpdate).not.toHaveBeenCalled() // the typed value is already the field value
    expect(listbox()).toBeNull()
  })

  it('typing emits update and reopens the list', async () => {
    const onUpdate = vi.fn()
    const { el } = await mountCombobox({ onUpdate })
    const input = el.querySelector<HTMLInputElement>('input[role="combobox"]')!
    input.value = 'alp'
    input.dispatchEvent(new Event('input'))
    await nextTick()

    expect(onUpdate).toHaveBeenCalledWith('alp')
    expect(listbox()).toBeTruthy()
  })

  it('keeps search local in commit-on-select mode and restores the committed choice on blur', async () => {
    const onUpdate = vi.fn()
    const { el } = await mountCombobox({
      value: '__ensemble__',
      leadingOption: {
        value: '__ensemble__',
        label: 'Multi-model fusion',
      },
      commitOnSelect: true,
      onUpdate,
    })
    const input = el.querySelector<HTMLInputElement>('input[role="combobox"]')!

    input.dispatchEvent(new Event('focus'))
    input.value = 'alpha'
    input.dispatchEvent(new Event('input'))
    await nextTick()

    expect(onUpdate).not.toHaveBeenCalled()
    expect(input.value).toBe('alpha')
    expect(optionRows().map(row => row.textContent)).toEqual(expect.arrayContaining([
      expect.stringContaining('test-vendor/alpha'),
      expect.stringContaining('Use "alpha"'),
    ]))

    input.dispatchEvent(new Event('blur'))
    await nextTick()
    expect(input.value).toBe('Multi-model fusion')
    expect(onUpdate).not.toHaveBeenCalled()

    input.dispatchEvent(new Event('focus'))
    input.value = 'beta'
    input.dispatchEvent(new Event('input'))
    input.dispatchEvent(new KeyboardEvent('keydown', {
      key: 'Escape',
      cancelable: true,
      bubbles: true,
    }))
    await nextTick()
    expect(input.value).toBe('Multi-model fusion')
    expect(onUpdate).not.toHaveBeenCalled()
  })

  it('commits a typed custom id only through the explicit free-text row', async () => {
    const onUpdate = vi.fn()
    const { el } = await mountCombobox({
      value: '__ensemble__',
      leadingOption: { value: '__ensemble__', label: 'Multi-model fusion' },
      commitOnSelect: true,
      onUpdate,
    })
    const input = el.querySelector<HTMLInputElement>('input[role="combobox"]')!

    input.dispatchEvent(new Event('focus'))
    input.value = 'my/custom-model'
    input.dispatchEvent(new Event('input'))
    await nextTick()
    const custom = optionRows().find(row => row.textContent?.includes('Use "my/custom-model"'))!
    custom.click()
    await nextTick()

    expect(onUpdate).toHaveBeenCalledTimes(1)
    expect(onUpdate).toHaveBeenCalledWith('my/custom-model')
    expect(listbox()).toBeNull()
  })

  it('selects the active row with Enter after arrow-key navigation', async () => {
    const onUpdate = vi.fn()
    const { el } = await mountCombobox({ onUpdate })
    const input = await openList(el)

    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown' }))
    await nextTick()
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter' }))
    await nextTick()

    expect(onUpdate).toHaveBeenCalledWith('test-vendor/alpha')
  })

  it('announces the keyboard-active option through aria-activedescendant', async () => {
    const { el } = await mountCombobox()
    const input = await openList(el)

    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown' }))
    await nextTick()

    const active = optionRows()[0]
    expect(active.classList.contains('is-active')).toBe(true)
    expect(active.id).not.toBe('')
    expect(input.getAttribute('aria-activedescendant')).toBe(active.id)
  })

  it('keeps the keyboard-active option visible while navigating a long catalog', async () => {
    const many = Array.from({ length: 12 }, (_, i) => makeModel(`test-vendor/model-${i}`))
    const { el } = await mountCombobox({ models: many })
    const input = await openList(el)
    const rows = optionRows()
    const scrollIntoView = vi.fn()
    rows[6].scrollIntoView = scrollIntoView

    for (let i = 0; i < 7; i += 1) {
      input.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown' }))
    }
    await nextTick()

    expect(rows[6].classList.contains('is-active')).toBe(true)
    expect(scrollIntoView).toHaveBeenCalledWith({ block: 'nearest' })
  })

  it('keeps static catalog context outside the listbox semantics', async () => {
    const { el } = await mountCombobox()
    await openList(el)

    const list = listbox()!
    expect(list.querySelector('.setup-model-combobox__readout')).toBeNull()
    expect(list.querySelector('.setup-model-combobox__footer')).toBeNull()
    expect(list.querySelectorAll('[role="option"]')).toHaveLength(2)
  })

  it('exposes the full model id when the visible label is truncated', async () => {
    const longId = 'provider/really-long-model-id-with-a-distinguishing-free-suffix'
    const { el } = await mountCombobox({ models: [makeModel(longId)] })
    await openList(el)

    expect(optionRows()[0].querySelector('.setup-model-combobox__id')?.getAttribute('title')).toBe(longId)
  })

  it('shows provenance once in a muted footer, not per-row badges', async () => {
    const { el } = await mountCombobox()
    await openList(el)

    const provenance = footerRows().map(f => f.textContent || '').join(' ')
    expect(provenance).toContain('provider, synthesized')
    // per-row rows never carry the capabilitySource enum
    expect(optionRows().every(row => !(row.textContent || '').includes('synthesized'))).toBe(true)
  })

  it('omits the provenance footer when no row names a metadata source', async () => {
    const { el } = await mountCombobox({
      models: [makeModel('test-vendor/plain', ''), makeModel('test-vendor/other', '')],
    })
    await openList(el)

    // No dangling "details from " sentence with an empty source list.
    expect(footerRows().every(f => !(f.textContent || '').includes('Live list'))).toBe(true)
  })
})
