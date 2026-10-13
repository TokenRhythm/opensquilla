import { computed, onMounted, onUnmounted, ref, type ComputedRef } from 'vue'
import type { RegistryResult } from '@/types/skills'

/**
 * Owner-local favorites for the Skill marketplace. Favorites are a browser-side
 * preference, not registry data: they persist in localStorage under this device's
 * operator profile and sync across tabs through the storage event. Nothing is
 * sent to the Gateway and nothing is installed — starring a skill is only a
 * reading list for deciding what to install later.
 *
 * State is module-level so every mounted market view (the keep-alive'd tab and
 * any remount) shares one reactive source of truth.
 */
const STORAGE_KEY = 'opensquilla.skillMarket.favorites'

export function favoriteRowKey(row: RegistryResult): string {
  return row.installReference || row.install_reference || row.identifier || row.name
}

function readStorage(): string[] {
  try {
    const parsed: unknown = JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]')
    if (!Array.isArray(parsed)) return []
    return parsed.filter((item): item is string => typeof item === 'string' && item.trim().length > 0)
  } catch {
    return []
  }
}

const favoriteKeys = ref<string[]>(readStorage())
const favoriteSet = computed(() => new Set(favoriteKeys.value))
let listeners = 0

function persist(next: string[]) {
  favoriteKeys.value = next
  try {
    if (next.length) localStorage.setItem(STORAGE_KEY, JSON.stringify(next))
    else localStorage.removeItem(STORAGE_KEY)
  } catch {
    // Private mode or a full disk: the favorites still hold for this session.
  }
}

function onStorageEvent(event: StorageEvent) {
  if (event.key === STORAGE_KEY || event.key === null) favoriteKeys.value = readStorage()
}

export interface SkillMarketFavorites {
  favoriteCount: ComputedRef<number>
  isFavorite: (row: RegistryResult) => boolean
  toggleFavorite: (row: RegistryResult) => void
}

/** Test-only: drop the shared in-memory favorites so cases cannot leak state. */
export function _resetSkillMarketFavoritesForTests(): void {
  favoriteKeys.value = []
}

export function useSkillMarketFavorites(): SkillMarketFavorites {
  onMounted(() => {
    if (listeners === 0) window.addEventListener('storage', onStorageEvent)
    listeners += 1
  })
  onUnmounted(() => {
    listeners = Math.max(0, listeners - 1)
    if (listeners === 0) window.removeEventListener('storage', onStorageEvent)
  })

  return {
    favoriteCount: computed(() => favoriteKeys.value.length),
    isFavorite: row => favoriteSet.value.has(favoriteRowKey(row)),
    toggleFavorite: row => {
      const key = favoriteRowKey(row)
      if (favoriteSet.value.has(key)) persist(favoriteKeys.value.filter(item => item !== key))
      else persist([...favoriteKeys.value, key])
    },
  }
}
