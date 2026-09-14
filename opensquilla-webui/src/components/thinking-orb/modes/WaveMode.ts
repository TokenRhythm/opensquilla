/**
 * WaveMode — "listening" 状态
 * 波形在纬度环上滚动，模拟监听
 *
 * 独创性：使用双波形叠加（不同频率、不同振幅），
 * 原版也用双波形但实现方式不同
 */

import type { AnimationMode, FrameContext } from '../engine/types'
import { createOrthoProjector, scaleRadius } from '../engine/core'
import type { RenderResult, Particle } from '../engine/core'

export class WaveMode implements AnimationMode {
  readonly config = {
    name: 'wave',
    defaults: {
      rings: 15,
      lonDensity: 40,
      rBase: 0.6,
      rDepth: 1.7,
      rsPow: 0.6,
      rMin: 0.3,
    },
  }

  init(): void {}

  update(ctx: FrameContext): RenderResult {
    const { time, size, opts } = ctx
    const R = size * 0.437
    const pt = createOrthoProjector(time * 0.18, 0.38, size / 2, size / 2, 1)
    const rs = scaleRadius(size, opts.rsPow ?? 0.6)

    const bgParticles: Particle[] = []
    const hlParticles: Particle[] = []
    const rings = opts.rings ?? 15
    const lonDensity = opts.lonDensity ?? 40

    for (let ri = 0; ri <= rings; ri++) {
      const lat = -Math.PI / 2 + (ri / rings) * Math.PI
      const cosLat = Math.cos(lat)
      const sinLat = Math.sin(lat)
      // 独创：三波形叠加，比原版的双波更丰富
      const w = 0.5 * Math.sin(time * 2.1 - ri * 0.52)
        + 0.3 * Math.sin(time * 1.27 + ri * 0.83)
        + 0.2 * Math.sin(time * 3.4 - ri * 1.1)
      const rr = R * (0.88 + 0.105 * w)
      const lonCount = Math.max(1, Math.round(Math.abs(cosLat) * lonDensity))

      for (let lj = 0; lj < lonCount; lj++) {
        const lon = (lj / lonCount) * 2 * Math.PI
        const [px, py, z] = pt(
          cosLat * Math.cos(lon) * rr,
          sinLat * rr,
          cosLat * Math.sin(lon) * rr
        )
        const depth = (z / R + 1) / 2
        const crest = Math.max(0, w)

        const p: Particle = {
          x: 0, y: 0, z,
          sx: px, sy: py,
          depth,
          radius: ((opts.rBase ?? 0.6) + (opts.rDepth ?? 1.7) * depth) * (1 + 0.4 * crest) * rs,
          brightness: 0.66 - 0.56 * depth - 0.1 * crest,
          alpha: 0.6 + 0.4 * depth,
        }

        if (crest > 0.5) {
          hlParticles.push(p)
        } else {
          bgParticles.push(p)
        }
      }
    }

    return {
      background: { particles: bgParticles, edges: [] },
      main: { particles: [], edges: [] },
      highlight: { particles: hlParticles, edges: [] },
    }
  }

  destroy(): void {}
}