/**
 * GlobeMode — "searching" 状态
 * 扫描子午线扫过点阵球体，模拟搜索
 *
 * 独创性：高亮层专门画扫描线上的增强点，而非原版统一在单层中处理
 */

import type { AnimationMode, FrameContext } from '../engine/types'
import { angleDelta, createOrthoProjector, scaleRadius } from '../engine/core'
import type { RenderResult, Particle } from '../engine/core'

export class GlobeMode implements AnimationMode {
  readonly config = {
    name: 'globe',
    defaults: {
      latRings: 17,
      lonDensity: 44,
      rBase: 0.6,
      rDepth: 1.7,
      rBoost: 1.0,
      inkFar: 0.62,
      inkSpan: 0.54,
      rsPow: 0.6,
      rMin: 0.3,
    },
  }

  init(): void {}

  update(ctx: FrameContext): RenderResult {
    const { time, size, opts } = ctx
    const spin = 0.5
    const R = size * 0.41
    const tilt = 0.4 + 0.06 * Math.sin(time * 0.35)
    const pt = createOrthoProjector(time * spin, tilt, size / 2, size / 2, R)
    const scan = time * (spin + (1.7 - spin) * (opts.scanMul ?? 1))
    const rs = scaleRadius(size, opts.rsPow ?? 0.6)
    const dimBase = opts.dimBase ?? 1

    const bgParticles: Particle[] = []
    const hlParticles: Particle[] = []
    const latRings = opts.latRings ?? 17
    const lonDensity = opts.lonDensity ?? 44

    for (let li = 0; li <= latRings; li++) {
      const lat = -Math.PI / 2 + (li / latRings) * Math.PI
      const cosLat = Math.cos(lat)
      const sinLat = Math.sin(lat)
      const lonCount = Math.max(1, Math.round(Math.abs(cosLat) * lonDensity))
      for (let lj = 0; lj < lonCount; lj++) {
        const lon = (lj / lonCount) * 2 * Math.PI
        const [px, py, z] = pt(cosLat * Math.cos(lon), sinLat, cosLat * Math.sin(lon))
        const depth = (z + 1) / 2
        const d = angleDelta(lon + time * spin, scan)
        const boost = Math.exp(-(d * d) / 0.18) * Math.max(0, z)

        const p: Particle = {
          x: 0, y: 0, z,
          sx: px, sy: py,
          depth,
          radius: ((opts.rBase ?? 0.6) + (opts.rDepth ?? 1.7) * depth + (opts.rBoost ?? 1) * boost) * rs,
          brightness: (opts.inkFar ?? 0.62) - (opts.inkSpan ?? 0.54) * depth,
          alpha: dimBase + (1 - dimBase) * Math.min(1, boost),
        }

        // 独创：扫描线上的点放到高亮层，其余放背景层
        if (boost > 0.3) {
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