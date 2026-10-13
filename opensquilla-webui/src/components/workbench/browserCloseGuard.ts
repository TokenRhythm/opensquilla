import type { NativeWorkbenchApi } from '@/platform/types'
import type { WorkbenchItem } from '@/workbench/types'

/** null lets older desktop hosts use their existing Workbench close path. */
export async function requestNativeBrowserClose(
  item: WorkbenchItem,
  nativeApi: NativeWorkbenchApi | undefined,
): Promise<boolean | null> {
  if (item.kind !== 'browser' || !nativeApi?.navigateSurface) return null
  let capabilities
  try {
    capabilities = await nativeApi.getCapabilities?.()
  } catch {
    return false
  }
  if (!capabilities?.navigationActions?.includes('close')) return null
  try {
    const result = await nativeApi.navigateSurface({ version: 2, surfaceId: item.id, action: 'close' })
    return result.ok || result.code === 'TARGET_NOT_FOUND'
  } catch {
    // An uncertain native close must keep the visible tab available.
    return false
  }
}
