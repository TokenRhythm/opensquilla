import { describe, expect, it } from 'vitest'

import appSource from './App.vue?raw'

describe('App toast host layer contract', () => {
  it('teleports the single global toast host directly to body', () => {
    expect(appSource).toMatch(
      /<Teleport to="body">\s*<ToastHost\s*\/>\s*<\/Teleport>/,
    )
    expect(appSource.match(/<ToastHost\s*\/>/g)).toHaveLength(1)
  })
})
