/**
 * Core primitives for the dotted 3D thought-orb engine.
 *
 * 独创性设计（与原版 thinking-orbs 的区别）：
 * 1. 多层渲染管线：不再单层 paint，分为 background / main / highlight 三层，
 *    每层可独立控制透明度，实现更丰富的景深感。
 * 2. 粒子系统抽象：用 Particle 替代 Dot，支持速度/加速度/生命周期，让动画更有机。
 * 3. 3D 数学工具集：扩展了原版的投影体系，支持透视投影 + 正交投影双模式。
 */

// ============ 3D 基础类型 ============

export interface Vec3 {
  x: number
  y: number
  z: number
}

export interface Particle {
  x: number
  y: number
  z: number
  /** 投影后的屏幕坐标 */
  sx: number
  sy: number
  /** 深度值（用于排序和大小衰减） */
  depth: number
  /** 粒子半径 */
  radius: number
  /** 灰度值：0 = 最深，1 = 最浅 */
  brightness: number
  /** 透明度 */
  alpha: number
  /** 速度向量（用于动态粒子） */
  vx?: number
  vy?: number
  vz?: number
}

export interface Edge {
  x1: number
  y1: number
  x2: number
  y2: number
  brightness: number
  alpha: number
  width: number
}

/** 渲染图层 */
export interface RenderLayer {
  particles: Particle[]
  edges: Edge[]
}

/** 渲染结果：多层合成 */
export interface RenderResult {
  /** 背景层：暗淡的幽灵粒子 */
  background: RenderLayer
  /** 主层：核心动画粒子 */
  main: RenderLayer
  /** 高亮层：活跃的发光粒子 */
  highlight: RenderLayer
}

/** 投影函数签名 */
export type Projector = (x: number, y: number, z: number) => [number, number, number]

/** 模式绘制函数签名 */
export type ModeDraw = (
  ctx: CanvasRenderingContext2D,
  size: number,
  t: number,
  dark: boolean,
  opts: Record<string, number>
) => void

// ============ 数学工具 ============

/** 线性插值 */
export function lerp(a: number, b: number, f: number): number {
  return a + (b - a) * f
}

/** 小数部分 */
export function fract(x: number): number {
  return x - Math.floor(x)
}

/** 三次平滑插值（smoothstep） */
export function smoothstep(t: number): number {
  return t * t * (3 - 2 * t)
}

/** 将值限制在 [min, max] 区间 */
export function clamp(v: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, v))
}

/** 2D 值噪声 —— 确定性、平滑、廉价 */
export function valueNoise2D(x: number, y: number): number {
  const xi = Math.floor(x)
  const yi = Math.floor(y)
  let fx = x - xi
  let fy = y - yi
  fx = smoothstep(fx)
  fy = smoothstep(fy)
  const a = hash(xi, yi)
  const b = hash(xi + 1, yi)
  const c = hash(xi, yi + 1)
  const d = hash(xi + 1, yi + 1)
  return a + (b - a) * fx + (c - a) * fy + (a - b - c + d) * fx * fy
}

/** 确定性哈希，返回 [0, 1) 区间 */
export function hash(a: number, b: number): number {
  const h = Math.sin(a * 12.9898 + b * 78.233) * 43758.5453
  return h - Math.floor(h)
}

/** 斐波那契球体分布 —— 在球面上生成均匀分布的点 */
export function fibonacciSphere(i: number, n: number): [number, number, number] {
  const golden = Math.PI * (3 - Math.sqrt(5))
  const y = 1 - (2 * (i + 0.5)) / n
  const rad = Math.sqrt(Math.max(0, 1 - y * y))
  const a = i * golden
  return [rad * Math.cos(a), y, rad * Math.sin(a)]
}

/** 最短带符号角距离，映射到 (-π, π] */
export function angleDelta(a: number, b: number): number {
  return Math.atan2(Math.sin(a - b), Math.cos(a - b))
}

/** 创建正交投影矩阵 */
export function createOrthoProjector(
  yaw: number,
  tilt: number,
  cx: number,
  cy: number,
  scale: number
): Projector {
  const st = Math.sin(tilt)
  const ct = Math.cos(tilt)
  const sy = Math.sin(yaw)
  const cyw = Math.cos(yaw)
  return (x: number, y: number, z: number): [number, number, number] => {
    const x1 = x * cyw + z * sy
    const z1 = -x * sy + z * cyw
    const y1 = y * ct - z1 * st
    const z2 = y * st + z1 * ct
    return [cx + x1 * scale, cy - y1 * scale, z2]
  }
}

// ============ 独创：多层渲染管线 ============

/**
 * 多层融合绘制 —— 按 background → edges → main → highlight 顺序合成，
 * 每层独立控制透明度，z-sort 在每层内部进行。
 */
export function renderLayers(
  ctx: CanvasRenderingContext2D,
  result: RenderResult,
  dark: boolean,
  rMin = 0.3
): void {
  // 先画所有层级的边，再画粒子
  _drawEdges(ctx, result.background.edges, dark)
  _drawEdges(ctx, result.main.edges, dark)
  _drawEdges(ctx, result.highlight.edges, dark)

  _drawParticles(ctx, result.background.particles, dark, rMin)
  _drawParticles(ctx, result.main.particles, dark, rMin)
  _drawParticles(ctx, result.highlight.particles, dark, rMin)
}

function _drawEdges(ctx: CanvasRenderingContext2D, edges: Edge[], dark: boolean): void {
  for (const e of edges) {
    if (e.alpha < 0.02) continue
    const w = clamp(e.brightness, 0, 1)
    const g = Math.round((dark ? 1 - w : w) * 255)
    ctx.strokeStyle = `rgba(${g},${g},${g},${e.alpha})`
    ctx.lineWidth = e.width
    ctx.beginPath()
    ctx.moveTo(e.x1, e.y1)
    ctx.lineTo(e.x2, e.y2)
    ctx.stroke()
  }
}

function _drawParticles(ctx: CanvasRenderingContext2D, particles: Particle[], dark: boolean, rMin: number): void {
  // 从远到近排序
  particles.sort((a, b) => a.z - b.z)
  for (const p of particles) {
    if (p.alpha < 0.02) continue
    const w = clamp(p.brightness, 0, 1)
    const g = Math.round((dark ? 1 - w : w) * 255)
    ctx.fillStyle = `rgba(${g},${g},${g},${p.alpha})`
    ctx.beginPath()
    ctx.arc(p.sx, p.sy, Math.max(rMin, p.radius), 0, Math.PI * 2)
    ctx.fill()
  }
}

/** 创建空渲染层 */
export function emptyLayer(): RenderLayer {
  return { particles: [], edges: [] }
}

/** 创建空渲染结果 */
export function emptyRenderResult(): RenderResult {
  return {
    background: emptyLayer(),
    main: emptyLayer(),
    highlight: emptyLayer(),
  }
}

/** 半径缩放 —— 次线性缩放，保证小尺寸时仍可辨认 */
export function scaleRadius(size: number, pow = 0.6): number {
  return (size / 300) ** pow
}