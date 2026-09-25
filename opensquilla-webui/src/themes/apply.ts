import { getManifest } from './registry'

// Theme-asset runtime: lazily load a value theme's global "world" layer. A
// theme's token pack and optional world CSS live in co-located files — the
// single source of truth the CI guards validate — so this module only
// orchestrates loading.

const worldLoaded = new Set<string>()

/** Lazily load a value theme's global "world" layer (fonts + app-wide stylesheet)
 *  when it becomes the active data-theme. Idempotent; a no-op for themes without
 *  a world (light/dark/arctic and the flat vivid palettes). */
export async function ensureThemeWorld(id: string): Promise<void> {
  if (!id || worldLoaded.has(id)) return
  const m = getManifest(id)
  if (!m || !m.world) return
  worldLoaded.add(id)
  try {
    await Promise.all([
      ...(m.world.fonts ?? []).map((f) => f.load()),
      m.world.styles ? m.world.styles() : Promise.resolve(),
    ])
  } catch (err) {
    worldLoaded.delete(id)
    console.warn(`[themes] failed to load world "${id}":`, err)
  }
}
