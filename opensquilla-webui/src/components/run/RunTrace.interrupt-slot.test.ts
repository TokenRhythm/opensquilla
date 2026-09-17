// @vitest-environment happy-dom
import { createApp, h, nextTick } from 'vue'
import { describe, expect, it } from 'vitest'
import i18n from '@/i18n'
import RunTrace from './RunTrace.vue'

describe('RunTrace interrupt slot', () => {
  it('renders an approval outcome between the surrounding timeline items', async () => {
    const root = document.createElement('div')
    document.body.appendChild(root)
    const items = [
      { type: 'text', key: 'before', html: '<p data-order="before">before</p>' },
      {
        type: 'interrupt',
        key: 'approval-1',
        part: {
          type: 'interrupt',
          key: 'approval-1',
          interruptKind: 'approval',
          resolution: 'approved',
          busy: false,
          error: '',
        },
      },
      { type: 'text', key: 'after', html: '<p data-order="after">after</p>' },
    ] as any
    const app = createApp({
      render: () => h(
        RunTrace,
        { items },
        {
          interrupt: ({ part }: any) => h(
            'div',
            { 'data-order': 'approval' },
            part.resolution,
          ),
        },
      ),
    })

    app.use(i18n)
    app.mount(root)
    await nextTick()

    expect(
      [...root.querySelectorAll<HTMLElement>('[data-order]')]
        .map(element => element.dataset.order),
    ).toEqual(['before', 'approval', 'after'])
    app.unmount()
  })
})
