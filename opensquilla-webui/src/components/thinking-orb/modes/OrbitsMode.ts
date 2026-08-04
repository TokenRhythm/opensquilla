/**
 * OrbitsMode — "working" 状态
 * 粒子在倾斜轨道上运行，模拟工作状态
 *
 * 独创性：使用多层渲染（背景幽灵轨道 + 主工作粒子），
 * 原版 orbits 只用单层画所有点。
 */

import type { AnimationMode, FrameContext } from '../engine/types'
import { createOrthoProjector, emptyRenderResult, fibonacciSphere, hash, scaleRadius } from '../engine/core'
import type { RenderResult, Particle } from '../engine/core'

export class OrbitsMode implements AnimationMode {
  readonly config = {
    name: 'orbits',
    defaults: {
      orbitN: 12,
      ghostN: 40,
      ghostR: 0.9,
      ghostA: 0.5,
      particles: 3,
      partR: 1.2,
      partRDepth: 1.6,
      rsPow: 0.6,
      rMin: 0.3,
    },
  }

  private _orbitCache: Array<{
    ro: number
    nx: number; ny: number; nz: number
    ux: number; uy: number; uz: number
    vx: number; vy: number; vz: number
    speed: number
  }> = []

  init(): void {
    // 预计算轨道参数
    this._orbitCache = []
  }

  update(ctx: FrameContext): RenderResult {
    const { time, size, projector: pt, opts } = ctx
    const R = size * 0.41
    const rs = scaleRadius(size, opts.rsPow ?? 0.6)
    const density = opts.density ?? 1
    const orbitN = Math.max(3, Math.round((opts.orbitN ?? 12) * density))
    const ghostN = Math.max(6, Math.round((opts.ghostN ?? 40) * density))
    const particles = Math.max(1, Math.round((opts.particles ?? 3) * density))

    if (this._orbitCache.length !== orbitN) {
      this._orbitCache = []
      for (let orb = 0; orb < orbitN; orb++) {
        const h1 = hash(orb, 1.7)
        const h2 = hash(orb, 5.2)
        const h3 = hash(orb, 8.9)
        const ro = R * (0.45 + 0.52 * h1)
        const th = h1 * 2 * Math.PI
        const phi = Math.acos(2 * h2 - 1)
        const nx = Math.sin(phi) * Math.cos(th)
        const ny = Math.cos(phi)
        const nz = Math.sin(phi) * Math.sin(th)
        let ux = -ny
        let uy = nx
        const uz = 0
        const ul = Math.max(1e-6, Math.sqrt(ux * ux + uy * uy))
        ux /= ul; uy /= ul
        const vx = ny * uz - nz * uy
        const vy = nz * ux - nx * uz
        const vz = nx * uy - ny * ux
        const speed = (0.25 + 0.55 * h3) * (h3 > 0.5 ? 1 : -1)
        this._orbitCache.push({ ro, nx, ny, nz, ux, uy, uz, vx, vy, vz, speed })
      }
    }

    const bgParticles: Particle[] = []
    const mainParticles: Particle[] = []

    for (const orb of this._orbitCache) {
      // 背景层：幽灵轨道（暗淡的半透明点）
      for (let k = 0; k < ghostN; k++) {
        const a = (k / ghostN) * 2 * Math.PI
        const [px, py, z] = pt(
          (orb.ux * Math.cos(a) + orb.vx * Math.sin(a)) * orb.ro,
          (orb.uy * Math.cos(a) + orb.vy * Math.sin(a)) * orb.ro,
          (orb.uz * Math.cos(a) + orb.vz * Math.sin(a)) * orb.ro
        )
        const depth = (z / orb.ro + 1) / 2
        bgParticles.push({
          x: 0, y: 0, z,
          sx: px, sy: py,
          depth,
          radius: (opts.ghostR ?? 0.9) * rs,
          brightness: 0.72,
          alpha: (opts.ghostA ?? 0.5) * (0.4 + 0.6 * depth),
        })
      }

      // 主层：工作粒子（更亮、更大）
      for (let m = 0; m < particles; m++) {
        const a = time * orb.speed + (m / particles) * 2 * Math.PI + hash(orb.ro, m) * 6
        const [px, py, z] = pt(
          (orb.ux * Math.cos(a) + orb.vx * Math.sin(a)) * orb.ro,
          (orb.uy * Math.cos(a) + orb.vy * Math.sin(a)) * orb.ro,
          (orb.uz * Math.cos(a) + orb.vz * Math.sin(a)) * orb.ro
        )
        const depth = (z / orb.ro + 1) / 2
        mainParticles.push({
          x: 0, y: 0, z,
          sx: px, sy: py,
          depth,
          radius: ((opts.partR ?? 1.2) + (opts.partRDepth ?? 1.6) * depth) * rs,
          brightness: 0.3 - 0.22 * depth,
          alpha: 0.8 + 0.2 * depth,
        })
      }
    }

    return {
      background: { particles: bgParticles, edges: [] },
      main: { particles: mainParticles, edges: [] },
      highlight: { particles: [], edges: [] },
    }
  }

  destroy(): void {
    this._orbitCache = []
  }
}