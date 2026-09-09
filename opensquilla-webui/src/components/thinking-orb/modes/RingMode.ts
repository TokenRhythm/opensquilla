/**
 * RingMode — "breathing" 状态
 * 正面圆形环缓慢脉动，模拟呼吸
 *
 * 独创性：使用正弦波 + 随机噪声混合的呼吸模式，
 * 比原版纯正弦波更有机
 */

import type { AnimationMode, FrameContext } from '../engine/types'
import { createOrthoProjector, fibonacciSphere, scaleRadius, valueNoise2D } from '../engine/core'
import type { RenderResult, Particle } from '../engine/core'

export class RingMode implements AnimationMode {
  readonly config = {
    name: 'ring',
    defaults: {
      lanes: 5,
      segs: 88,
      ghostN: 0,
      faceOn: 1,
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
    const camTilt = 0.3
    const pt = createOrthoProjector(time * 0.1 * (opts.spin ?? 1), camTilt, size / 2, size / 2, 1)
    const rs = scaleRadius(size, opts.rsPow ?? 0.6)

    const mainParticles: Particle[] = []
    const ghostN = opts.ghostN ?? 0

    // 背景层：幽灵球体（如果启用）
    const bgParticles: Particle[] = []
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

    // 独创：呼吸模式使用正弦波 + 噪声混合
    const ya = time * 0.24 * (opts.spin ?? 1)
    const ta = 0.3
    const ux = Math.cos(ya)
    const uy = 0
    const uz = Math.sin(ya)
    const vx = -uz * Math.sin(ta)
    const vy = Math.cos(ta)
    const vz = ux * Math.sin(ta)
    const nx = uy * vz - uz * vy
    const ny = uz * vx - ux * vz
    const nz = ux * vy - uy * vx

    const wobAmp = 0.23 * (opts.wobMul ?? 0.37)
    const baseR = R / (1 + 0.85 * wobAmp)
    const baseLanes = opts.lanes ?? 5
    const segs = opts.segs ?? 88
    const lanes = Math.max(1, Math.round(baseLanes * (opts.bandMul ?? 1)))

    for (let w = 0; w < lanes; w++) {
      const laneOff = (w - (lanes - 1) / 2) * 0.075
      const edge = Math.abs(w - (lanes - 1) / 2) / Math.max(1, (lanes - 1) / 2)
      for (let k = 0; k < segs; k++) {
        const a = (k / segs) * 2 * Math.PI
        // 独创：呼吸使用正弦波 + 噪声，制造有机脉动
        const noise = valueNoise2D(a * 2 + time * 0.3, w * 0.5) - 0.5
        const wob = (0.16 * Math.sin(a * 3 - time * 1.7 + w * 0.22) +
          0.07 * Math.sin(a * 5 + time * 1.1) +
          0.03 * noise) * (opts.wobMul ?? 0.37)
        const radial = 1 + wob + laneOff * 0.3
        const x = ux * Math.cos(a) + vx * Math.sin(a) + nx * laneOff
        const y = uy * Math.cos(a) + vy * Math.sin(a) + ny * laneOff
        const z = uz * Math.cos(a) + vz * Math.sin(a) + nz * laneOff
        const l = Math.sqrt(x * x + y * y + z * z)
        const [px, py, zr] = pt((x / l) * baseR * radial, (y / l) * baseR * radial, (z / l) * baseR * radial)
        const depth = (zr / R + 1) / 2

        mainParticles.push({
          x: 0, y: 0, z: zr,
          sx: px, sy: py,
          depth,
          radius: ((opts.rBase ?? 1.1) + (opts.rDepth ?? 1.7) * depth) * (1 - 0.25 * edge) * rs,
          brightness: 0.52 - 0.44 * depth + 0.18 * edge,
          alpha: 0.5 + 0.5 * depth,
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