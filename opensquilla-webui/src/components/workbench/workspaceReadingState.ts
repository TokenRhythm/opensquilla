import type { WorkspaceFile } from '@/modules/workspaceFiles'
import { resourceReadingState, type ResourceReadingState } from './resourceReadingState'

/** Reading hints only: never content, resource handles, or read authority. */
export interface WorkspaceReadingState {
  mode: 'preview' | 'source'
  query: string
  jump: string
  toolsOpen: boolean
  startLine: number
  focusLine: number
  windowStart: number
  sourceTop: number
  sourceLeft: number
  markdownTop: number
  resource?: ResourceReadingState
}

const boundedNumber = (value: unknown, fallback: number, maximum = Number.MAX_SAFE_INTEGER) =>
  typeof value === 'number' && Number.isFinite(value) ? Math.max(0, Math.min(maximum, value)) : fallback

export function workspaceReadingState(value: unknown): WorkspaceReadingState | undefined {
  if (!value || typeof value !== 'object') return undefined
  const state = value as Record<string, unknown>
  return {
    mode: state.mode === 'source' ? 'source' : 'preview',
    query: typeof state.query === 'string' ? state.query.slice(0, 512) : '',
    jump: typeof state.jump === 'string' ? state.jump.slice(0, 16) : '',
    toolsOpen: state.toolsOpen === true,
    startLine: Math.max(1, Math.floor(boundedNumber(state.startLine, 1))),
    focusLine: Math.max(1, Math.floor(boundedNumber(state.focusLine, 1))),
    windowStart: Math.floor(boundedNumber(state.windowStart, 0)),
    sourceTop: boundedNumber(state.sourceTop, 0),
    sourceLeft: boundedNumber(state.sourceLeft, 0),
    markdownTop: boundedNumber(state.markdownTop, 0),
    ...(resourceReadingState(state.resource) ? { resource: resourceReadingState(state.resource) } : {}),
  }
}

export function workspaceReadingIdentity(sessionKey: string, file: WorkspaceFile): string {
  return JSON.stringify([sessionKey, file.workspaceBinding, file.path, file.revision ?? '', file.size])
}

export interface WorkspaceFilesReadingState {
  sessionKey: string
  binding: string
  expanded: string[]
  loadedCounts: Record<string, number>
  filter: string
  treeVisible: boolean
  treeTop: number
  selectedPath: string
  sourceMode: boolean
  selectedIdentity: string
  reading?: WorkspaceReadingState
}

export function workspaceFilesReadingState(value: unknown): WorkspaceFilesReadingState | undefined {
  if (!value || typeof value !== 'object') return undefined
  const state = value as Record<string, unknown>
  if (typeof state.sessionKey !== 'string' || typeof state.binding !== 'string') return undefined
  const path = (value: unknown): value is string => typeof value === 'string' && value.length <= 4096
  const selectedPath = path(state.selectedPath) ? state.selectedPath : ''
  const candidates = Array.isArray(state.expanded) ? state.expanded.filter(path) : []
  // Keep the selected file's visible ancestry first. A huge tree must not make
  // the manager reject the entire checkpoint, including the selected reader.
  const ancestors = candidates.filter(value => selectedPath.startsWith(`${value}/`))
  const expanded: string[] = []
  let pathCharacters = 0
  for (const value of new Set([...ancestors, ...candidates])) {
    if (expanded.length >= 256) break
    if (pathCharacters + value.length > 8192) continue
    expanded.push(value)
    pathCharacters += value.length
  }
  const counts = state.loadedCounts && typeof state.loadedCounts === 'object' ? state.loadedCounts : {}
  const loadedCounts: Record<string, number> = {}
  for (const key of ['', ...expanded]) {
    loadedCounts[key] = Math.floor(boundedNumber((counts as Record<string, unknown>)[key], 200, 20_000))
  }
  const reading = workspaceReadingState(state.reading)
  return {
    sessionKey: state.sessionKey, binding: state.binding, expanded, loadedCounts,
    filter: typeof state.filter === 'string' ? state.filter.slice(0, 512) : '',
    treeVisible: state.treeVisible !== false,
    treeTop: boundedNumber(state.treeTop, 0),
    selectedPath,
    sourceMode: state.sourceMode === true,
    selectedIdentity: typeof state.selectedIdentity === 'string' && state.selectedIdentity.length <= 8192 ? state.selectedIdentity : '',
    ...(reading ? { reading } : {}),
  }
}
