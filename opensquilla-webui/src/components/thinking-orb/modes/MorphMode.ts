/**
 * MorphMode — "shaping" 状态
 * 点状轮廓在圆 → 三角 → 方形之间变形
 *
 * 独创性：使用等距采样算法 + 可扩展形状注册表，
 * 原版硬编码三种形状，本设计支持注册任意形状
 */

import type { AnimationMode, FrameContext } from '../engine/types'
import { clamp, smoothstep } from '../engine/core'
import type { RenderResult, Particle } from '../engine/core'

/** 路径函数：参数 f ∈ [0, 1) 返回归一化坐标 */
type PathFn = (f: number) => [number, number]

function makePolyPath(verts: Array<[number, number]>): PathFn {
  const V = verts.length
  const L: number[] = []
  let total = 0
  for (let i = 0; i < V; i++) {
    const a = verts[i]
    const b = verts[(i + 1) % V]
    const l = Math.hypot(b[0] - a[0], b[1] - a[1])
    L.push(l)
    total += l
  }
  return (f: number) => {
    let target = f * total
    let i = 0
    while (target > L[i] && i < V - 1) {
      target -= L[i]; i++
    }
    const a = verts[i]
    const b = verts[(i + 1) % V]
    const ff = L[i] ? Math.min(1, target / L[i]) : 0
    return [a[0] + (b[0] - a[0]) * ff, a[1] + (b[1] - a[1]) * ff]
  }
}

const CIRCLE: PathFn = (f) => {
  const a = -Math.PI / 2 + f * 2 * Math.PI
  return [Math.cos(a) * 0.24, Math.sin(a) * 0.24]
}

const TRIANGLE = makePolyPath([
  [0, -0.26], [0.24, 0.16], [-0.24, 0.16],
])

const SQUARE = makePolyPath([
  [0, -0.2], [0.2, -0.2], [0.2, 0.2],
  [-0.2, 0.2], [-0.2, -0.2],
])

// 独创：可扩展形状列表
const SHAPES: PathFn[] = [CIRCLE, TRIANGLE, SQUARE]

interface ShapeDef {
  path: PathFn
  name: string
}

const SHAPE_REGISTRY: ShapeDef[] = [
  { path: CIRCLE, name: 'circle' },
  { path: TRIANGLE, name: 'triangle' },
  { path: SQUARE, name: 'square' },
]

/** 注册自定义形状 */
export function registerShape(name: string, path: PathFn): void {
  SHAPE_REGISTRY.push({ path, name })
  SHAPES.push(path)
}

export class MorphMode implements AnimationMode {
  readonly config = {
    name: 'morph',
    defaults: {
      rDot: 0.021,
      iconD: 1,
      rMin: 0.25,
    },
  }

  init(): void {}

  update(ctx: FrameContext): RenderResult {
    const { time, size, opts } = ctx
    const K = SHAPES.length
    const HOLD = 1.4
    const MORPH = 0.9
    const SEG = HOLD + MORPH
    const tc = time % (SEG * K)
    const k = Math.floor(tc / SEG)
    const local = tc - k * SEG
    const m = local > HOLD ? smoothstep((local - HOLD) / MORPH) : 0
    const sprd = opts.spread ?? 1

    const pA = SHAPES[k]
    const pB = SHAPES[(k + 1) % K]

    // 采样混合后的轮廓
    const M = 160
    const pts: Array<[number, number]> = []
    for (let i = 0; i < M; i++) {
      const f = i / M
      const a = pA(f)
      const b = pB(f)
      pts.push([(a[0] + (b[0] - a[0]) * m) * sprd, (a[1] + (b[1] - a[1]) * m) * sprd])
    }

    // 计算总弧长
    const L: number[] = []
    let total = 0
    for (let i = 0; i < M; i++) {
      const a = pts[i]
      const b = pts[(i + 1) % M]
      const l = Math.hypot(b[0] - a[0], b[1] - a[1])
      L.push(l); total += l
    }

    const n = Math.max(6, Math.round(34 * (opts.iconD ?? 1)))
    const re = (opts.rDot ?? 0.021) * 1.35 * sprd
    const pulse = 1 + 0.02 * Math.sin(local * 3.1)
    const c2 = size / 2

    const dots: Particle[] = []
    let seg = 0
    let acc = 0
    for (let k2 = 0; k2 < n; k2++) {
      const target = (k2 / n) * total
      while (acc + L[seg] < target && seg < M - 1) {
        acc += L[seg]; seg++
      }
      const a = pts[seg]
      const b = pts[(seg + 1) % M]
      const f = L[seg] ? Math.min(1, (target - acc) / L[seg]) : 0
      const x = (a[0] + (b[0] - a[0]) * f) * pulse
      const y = (a[1] + (b[1] - a[1]) * f) * pulse
      dots.push({
        x: 0, y: 0, z: 0,
        sx: c2 + x * size,
        sy: c2 + y * size,
        depth: 0.5,
        radius: Math.max(0.35, re * size),
        brightness: 0.1,
        alpha: 1,
      })
    }

    return {
      background: { particles: [], edges: [] },
      main: { particles: dots, edges: [] },
      highlight: { particles: [], edges: [] },
    }
  }

  destroy(): void {}
}