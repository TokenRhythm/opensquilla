/**
 * RubikMode — "solving" 状态
 * 色带扭动 → 复位，模拟计算过程
 *
 * 独创性：用独立的 MoveEngine 类管理扭动序列，与原版的纯函数不同
 */

import type { AnimationMode, FrameContext } from '../engine/types'
import { createOrthoProjector, hash, scaleRadius } from '../engine/core'
import type { RenderResult, Particle } from '../engine/core'

interface Move {
  axis: 0 | 1 | 2
  lo: number
  hi: number
  ang: number
}

class MoveEngine {
  private _moves: Move[] = []

  generate(count: number): void {
    this._moves = []
    for (let i = 0; i < count; i++) {
      const axis = Math.min(2, Math.floor(hash(i, 2.3) * 3)) as 0 | 1 | 2
      const lo = -1.0 + 0.5 * Math.min(3, Math.floor(hash(i, 5.9) * 4))
      const dir = hash(i, 7.7) < 0.5 ? 1 : -1
      this._moves.push({ axis, lo, hi: lo + 0.5, ang: (dir * Math.PI) / 2 })
    }
  }

  solveCycle(time: number, count: number, slotDur: number, rest: number): { active: number; amounts: number[] } {
    const cyc = 2 * count * slotDur + rest
    const tc = time % cyc
    const amounts = new Array<number>(count).fill(0)
    let active = -1
    if (tc < 2 * count * slotDur) {
      const slot = Math.floor(tc / slotDur)
      const p = (tc - slot * slotDur) / slotDur
      const cl = Math.min(1, p / 0.7)
      const ep = 1 - (1 - cl) ** 3
      if (slot < count) {
        for (let i = 0; i < slot; i++) amounts[i] = 1
        amounts[slot] = ep
        active = slot
      } else {
        const u = 2 * count - 1 - slot
        for (let i = 0; i < u; i++) amounts[i] = 1
        amounts[u] = 1 - ep
        active = u
      }
    }
    return { active, amounts }
  }

  apply(pt3: [number, number, number], amounts: number[], active: number): [number, number, number, boolean] {
    let [x, y, z] = pt3
    let inActive = false
    for (let i = 0; i < this._moves.length; i++) {
      if (amounts[i] <= 0) continue
      const mv = this._moves[i]
      const coord = mv.axis === 0 ? x : mv.axis === 1 ? y : z
      if (coord < mv.lo || coord >= mv.hi) continue
      if (i === active) inActive = true
      const a = mv.ang * amounts[i]
      const ca = Math.cos(a)
      const sa = Math.sin(a)
      if (mv.axis === 0) {
        const y2 = y * ca - z * sa; z = y * sa + z * ca; y = y2
      } else if (mv.axis === 1) {
        const x2 = x * ca + z * sa; z = -x * sa + z * ca; x = x2
      } else {
        const x2 = x * ca - y * sa; y = x * sa + y * ca; x = x2
      }
    }
    return [x, y, z, inActive]
  }

  get moves(): Move[] { return this._moves }
}

export class RubikMode implements AnimationMode {
  readonly config = {
    name: 'rubik',
    defaults: {
      latRings: 15,
      lonDensity: 40,
      moveCount: 14,
      rBase: 0.6,
      rDepth: 1.7,
      rActive: 0.3,
      inkFar: 0.62,
      inkSpan: 0.54,
      rsPow: 0.6,
      rMin: 0.3,
    },
  }

  private _moveEngine = new MoveEngine()

  init(): void {
    this._moveEngine.generate(14)
  }

  update(ctx: FrameContext): RenderResult {
    const { time, size, opts } = ctx
    const R = size * 0.41
    const pt = createOrthoProjector(time * 0.55, 0.35 + 0.1 * Math.sin(time * 0.9), size / 2, size / 2, R)
    const rs = scaleRadius(size, opts.rsPow ?? 0.6)
    const moveCount = opts.moveCount ?? 14
    const sc = this._moveEngine.solveCycle(time, moveCount, 0.42, 1.2)

    if (this._moveEngine.moves.length !== moveCount) {
      this._moveEngine.generate(moveCount)
    }

    const mainParticles: Particle[] = []
    const hlParticles: Particle[] = []
    const latRings = opts.latRings ?? 15
    const lonDensity = opts.lonDensity ?? 40

    for (let li = 0; li <= latRings; li++) {
      const lat = -Math.PI / 2 + (li / latRings) * Math.PI
      const cosLat = Math.cos(lat)
      const sinLat = Math.sin(lat)
      const lonCount = Math.max(1, Math.round(Math.abs(cosLat) * lonDensity))
      for (let lj = 0; lj < lonCount; lj++) {
        const lon = (lj / lonCount) * 2 * Math.PI
        const [x, y, z, inActive] = this._moveEngine.apply(
          [cosLat * Math.cos(lon), sinLat, cosLat * Math.sin(lon)],
          sc.amounts, sc.active
        )
        const [px, py, zr] = pt(x, y, z)
        const depth = (zr + 1) / 2
        const p: Particle = {
          x: 0, y: 0, z: zr,
          sx: px, sy: py,
          depth,
          radius: ((opts.rBase ?? 0.6) + (opts.rDepth ?? 1.7) * depth + (inActive ? (opts.rActive ?? 0.3) : 0)) * rs,
          brightness: (opts.inkFar ?? 0.62) - (opts.inkSpan ?? 0.54) * depth - (inActive ? 0.14 : 0),
          alpha: 0.8 + 0.2 * depth,
        }
        if (inActive) {
          hlParticles.push(p)
        } else {
          mainParticles.push(p)
        }
      }
    }

    return {
      background: { particles: [], edges: [] },
      main: { particles: mainParticles, edges: [] },
      highlight: { particles: hlParticles, edges: [] },
    }
  }

  destroy(): void {}
}