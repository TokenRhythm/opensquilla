/**
 * RibbonMode — "composing" 状态
 * 多条平行带状波浪在轨道上摆动
 *
 * 独创性：独创的"波形传播"算法，使用多重谐波叠加，
 * 加上边缘淡出效果，比原版 ribbon 更丰富
 */

import type { AnimationMode, FrameContext } from '../engine/types'
import { createOrthoProjector, fibonacciSphere, scaleRadius } from '../engine/core'
import type { RenderResult, Particle } from '../engine/core'

export class RibbonMode implements AnimationMode {
  readonly config = {
    name: 'ribbon',
    defaults: {
      lanes: 5,
      segs: 88,
      ghostN: 150,
      rBase: 1.1,
      rDepth: 1.7,
      rsPow: 0.6,
      rMin: 0.3,
    },
  }

  init(): void {}

  update(ctx: FrameContext): RenderResult {
    const { time, size, opts } = ctx
    const R = size * 0.39
    const spin = opts.spin ?? 1
    const camTilt = 0.3
    const pt = createOrthoProjector(time * 0.1 * spin, camTilt, size / 2, size / 2, 1)
    const rs = scaleRadius(size, opts.rsPow ?? 0.6)

    const bgParticles: Particle[] = []
    const mainParticles: Particle[] = []
    const ghostN = opts.ghostN ?? 150

    // 背景层
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

    // 带状平面
    const ya = time * 0.24 * spin
    const ta = 0.55 + 0.3 * Math.sin(time * 0.18) * spin
    const ux = Math.cos(ya)
    const uy = 0
    const uz = Math.sin(ya)
    const vx = -uz * Math.sin(ta)
    const vy = Math.cos(ta)
    const vz = ux * Math.sin(ta)
    const nx = uy * vz - uz * vy
    const ny = uz * vx - ux * vz
    const nz = ux * vy - uy * vx

    const wobAmp = 0.23 * (opts.wobMul ?? 1)
    const baseR = R
    const baseLanes = opts.lanes ?? 5
    const segs = opts.segs ?? 88
    const lanes = Math.max(1, Math.round(baseLanes * (opts.bandMul ?? 1)))

    for (let w = 0; w < lanes; w++) {
      const laneOff = (w - (lanes - 1) / 2) * 0.075
      const edge = Math.abs(w - (lanes - 1) / 2) / Math.max(1, (lanes - 1) / 2)
      for (let k = 0; k < segs; k++) {
        const a = (k / segs) * 2 * Math.PI
        // 独创：三重谐波叠加，比原版的双波更丰富
        const wob =
          (0.16 * Math.sin(a * 3 - time * 1.7 + w * 0.22) +
           0.07 * Math.sin(a * 5 + time * 1.1) +
           0.04 * Math.sin(a * 7 - time * 0.8 + w * 0.5)) * (opts.wobMul ?? 1)
        const off = laneOff + wob
        const x = ux * Math.cos(a) + vx * Math.sin(a) + nx * off
        const y = uy * Math.cos(a) + vy * Math.sin(a) + ny * off
        const z = uz * Math.cos(a) + vz * Math.sin(a) + nz * off
        const l = Math.sqrt(x * x + y * y + z * z)
        const [px, py, zr] = pt((x / l) * baseR, (y / l) * baseR, (z / l) * baseR)
        const depth = (zr / R + 1) / 2

        mainParticles.push({
          x: 0, y: 0, z: zr,
          sx: px, sy: py,
          depth,
          radius: ((opts.rBase ?? 1.1) + (opts.rDepth ?? 1.7) * depth) * (1 - 0.25 * edge) * rs,
          brightness: 0.52 - 0.44 * depth + 0.18 * edge,
          alpha: 0.4 + 0.6 * depth,
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