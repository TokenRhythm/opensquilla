import { modeRegistry } from './registry'
import { OrbitsMode } from './modes/OrbitsMode'; import { GlobeMode } from './modes/GlobeMode'
import { RubikMode } from './modes/RubikMode'; import { WaveMode } from './modes/WaveMode'
import { WebMode } from './modes/WebMode'; import { BraidMode } from './modes/BraidMode'
import { RibbonMode } from './modes/RibbonMode'; import { RingMode } from './modes/RingMode'
import { MorphMode } from './modes/MorphMode'

export function registerBuiltinModes(): void {
  modeRegistry.register('orbits', OrbitsMode); modeRegistry.register('globe', GlobeMode)
  modeRegistry.register('rubik', RubikMode); modeRegistry.register('wave', WaveMode)
  modeRegistry.register('web', WebMode); modeRegistry.register('braid', BraidMode)
  modeRegistry.register('ribbon', RibbonMode); modeRegistry.register('ring', RingMode)
  modeRegistry.register('morph', MorphMode)
  modeRegistry.alias('working', 'web'); modeRegistry.alias('searching', 'globe'); modeRegistry.alias('solving', 'rubik')
  modeRegistry.alias('listening', 'wave'); modeRegistry.alias('connecting', 'orbits'); modeRegistry.alias('weaving', 'braid')
  modeRegistry.alias('composing', 'ribbon'); modeRegistry.alias('breathing', 'ring'); modeRegistry.alias('shaping', 'morph')
}

export type OrbState = 'working' | 'searching' | 'solving' | 'listening' | 'connecting' | 'weaving' | 'composing' | 'breathing' | 'shaping'
export const ALL_STATES: OrbState[] = ['working', 'searching', 'solving', 'listening', 'connecting', 'weaving', 'composing', 'breathing', 'shaping']
export const STATE_LABELS: Record<OrbState, string> = {
  working: '工作中…', searching: '搜索中…', solving: '计算中…', listening: '监听中…',
  connecting: '连接中…', weaving: '编织中…', composing: '创作中…', breathing: '思考中…', shaping: '塑形中…',
}

interface DensityPoint { small: { speed: number; density: number; extra?: Record<string, number> }; large: { speed: number; density: number; extra?: Record<string, number> } }

export function resolvePreset(state: OrbState, size: number): { mode: undefined; speed: number; density: number; opts: Record<string, number> } {
  const point = DENSITY_PRESETS[state] ?? DENSITY_PRESETS.working
  const f = Math.min(1, Math.max(0, (size - 20) / (64 - 20)))
  const speed = point.small.speed + (point.large.speed - point.small.speed) * f
  const density = point.small.density + (point.large.density - point.small.density) * f
  const extra = { ...point.small.extra, ...point.large.extra }
  return { mode: undefined, speed, density, opts: extra }
}

const DENSITY_PRESETS: Record<string, DensityPoint> = {
  working: {
    small: { speed: 2.5, density: 0.50 },
    large: { speed: 1.89, density: 1.0 },
  },
  searching: {
    small: { speed: 1.8, density: 0.30, extra: { scanMul: 5.0, dimBase: 0.45 } },
    large: { speed: 2.02, density: 0.42, extra: { scanMul: 4.08, dimBase: 0.45 } },
  },
  solving: {
    small: { speed: 1.5, density: 0.25 },
    large: { speed: 1.82, density: 0.35 },
  },
  listening: {
    small: { speed: 3.0, density: 0.25 },
    large: { speed: 4.39, density: 0.34 },
  },
  connecting: {
    small: { speed: 4.0, density: 0.50 },
    large: { speed: 3.32, density: 1.35 },
  },
  weaving: {
    small: { speed: 2.0, density: 0.25 },
    large: { speed: 1.63, density: 0.50 },
  },
  composing: {
    small: { speed: 2.0, density: 0.15, extra: { spin: 0, bandMul: 6.0, wobMul: 1 } },
    large: { speed: 2.34, density: 0.25, extra: { spin: 0, bandMul: 3.9, wobMul: 1 } },
  },
  breathing: {
    small: { speed: 2.5, density: 0.10, extra: { spin: 0, bandMul: 5.0, wobMul: 0.5 } },
    large: { speed: 3.24, density: 0.25, extra: { spin: 0, bandMul: 3.63, wobMul: 0.37 } },
  },
  shaping: {
    small: { speed: 1.5, density: 0.70, extra: { spread: 1.5 } },
    large: { speed: 2.41, density: 0.70, extra: { spread: 1.45 } },
  },
}