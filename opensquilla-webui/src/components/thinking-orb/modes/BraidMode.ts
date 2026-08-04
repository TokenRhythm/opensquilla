/**
 * BraidMode — "weaving" 状态
 * 三股辫子围绕球体编织
 *
 * 独创性：使用径向呼吸动画 + 端部淡入淡出，比原版更丰富的编织感
 */

import type { AnimationMode, FrameContext } from '../engine/types'
import { createOrthoProjector, fibonacciSphere, fract, scaleRadius } from '../engine/core'
import type { RenderResult, Particle } from '../engine/core'

export class BraidMode implements AnimationMode {
  readonly config = {
    name: 'braid',
    defaults: {
      strandN: 52,
      turns: 3.0,
      ghostN: 150,
      rBase: 1.2,
      rDepth: 1.8,
      rsPow: 0.6,
      rMin: 0.3,
    },
  }

  init(): void {}

  update(ctx: FrameContext): RenderResult {
    const { time, size, opts } = ctx
    const R = size * 0.38
    const pt = createOrthoProjector(time * 0.4, 0.3, size / 2, size / 2, 1)
    const rs = scaleRadius(size, opts.rsPow ?? 0.6)

    const bgParticles: Particle[] = []
    const mainParticles: Particle[] = []
    const ghostN = opts.ghostN ?? 150

    // 背景层：幽灵球体
    for (let i = 0; i < ghostN; i++) {
      const d = fibonacciSphere(i, ghostN)
      const [px, py, z] = pt(d[0] * R, d[1] * R, d[2] * R)
      const depth = (z / R + 1) / 2
      bgParticles.push({
        x: 0, y: 0, z,
        sx: px, sy: py,
        depth,
        radius: 0.8 * rs,
        brightness: 0.78,
        alpha: 0.1 + 0.22 * depth,
      })
    }

    // 独创：三股半透明辫子，每股有微妙的颜色/透明度差异
    const strandN = opts.strandN ?? 52
    const turns = opts.turns ?? 3
    const strandColors = [0.1, 0.25, 0.4] // 不同亮度偏移

    for (let s = 0; s < 3; s++) {
      const phase = (s / 3) * 2 * Math.PI
      const colorOffset = strandColors[s]
      for (let i = 0; i < strandN; i++) {
        const u = (fract(i / strandN + time * 0.045) * 2 - 1) * 0.96
        const surf = Math.sqrt(Math.max(0, 1 - u * u))
        const endFade = Math.min(1, (1 - Math.abs(u)) / 0.1)
        const a = u * Math.PI * turns + phase
        const weave = 1 + 0.075 * Math.sin(u * Math.PI * turns * 2 + phase * 2 + time * 0.8)
        const rr = surf * R * weave
        const [px, py, zr] = pt(Math.cos(a) * rr, u * R * weave, Math.sin(a) * rr)
        const depth = (zr / R + 1) / 2

        mainParticles.push({
          x: 0, y: 0, z: zr,
          sx: px, sy: py,
          depth,
          radius: ((opts.rBase ?? 1.2) + (opts.rDepth ?? 1.8) * depth) * rs,
          brightness: 0.55 - 0.45 * depth + colorOffset * 0.3,
          alpha: endFade * (0.45 + 0.55 * depth),
        })
      }
    }

    return {
      background: { particles: bgParticles, edges: [] },
      main: { particles: mainParticles, edges: [] },
      highlight: { particles: [], edges: [] },
    }
  }

  destroy(): void {}
}