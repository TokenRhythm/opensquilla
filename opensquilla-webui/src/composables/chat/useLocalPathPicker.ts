import { computed, onScopeDispose, ref, watch } from 'vue'
import type { PlatformFilesApi } from '@/platform/types'

const MAX_COMPOSER_LENGTH = 100_000
const MAX_NATIVE_DROP_PATHS = 10
const MAX_NATIVE_PATH_LENGTH = 32_768

/** Paths are local path references carried as message strings, not file content or attachments. */
export function useLocalPathPicker(options: {
  available: () => boolean
  nativeDropAvailable?: () => boolean
  scope: () => readonly unknown[]
  getBinding: () => Promise<{ instanceId: string; profileFingerprint: string } | null>
  choosePaths: NonNullable<PlatformFilesApi['chooseLocalFilePaths']>
  resolveNativeFilePath?: (file: File) => Promise<string | null>
  text: () => string
  append: (text: string) => void
  onError: (kind: 'failed' | 'too-long' | 'too-many') => void
}) {
  const available = computed(options.available)
  const nativeDropAvailable = computed(() => options.nativeDropAvailable?.() ?? available.value)
  const busy = ref(false)
  let generation = 0
  const cancel = () => { generation += 1; busy.value = false }
  // Sync invalidation also catches a switch away and back while the OS dialog is open.
  // Parent-object refreshes can rerun this getter without changing any scope value.
  watch(() => [available.value, nativeDropAvailable.value, ...options.scope()], (next, previous) => {
    if (next.length !== previous.length || next.some((value, index) => !Object.is(value, previous[index]))) cancel()
  }, { flush: 'sync' })
  onScopeDispose(cancel)

  async function choose() {
    if (!available.value || busy.value) return
    const operation = ++generation
    busy.value = true
    const current = () => operation === generation && available.value
    try {
      const binding = await options.getBinding()
      if (!current()) return
      if (!binding) throw new Error('Owned Gateway unavailable')
      const paths = await options.choosePaths({ gatewayInstanceId: binding.instanceId })
      if (!current()) return
      // Refresh main's binding too: a reconnect may precede delivery of its renderer event.
      const now = await options.getBinding()
      if (!current() || !now || now.instanceId !== binding.instanceId
        || now.profileFingerprint !== binding.profileFingerprint) return
      if (!Array.isArray(paths) || paths.length > MAX_NATIVE_DROP_PATHS || paths.some(path => typeof path !== 'string'
        || !path || path.length > MAX_NATIVE_PATH_LENGTH || path.trim() !== path || /[\u0000-\u001f\u007f]/.test(path))) {
        throw new Error('Invalid selected paths')
      }
      if (!paths.length) return
      const text = paths.join('\n')
      const existing = options.text()
      const nextLength = existing.trim() ? existing.trimEnd().length + 1 + text.length : text.length
      if (nextLength > MAX_COMPOSER_LENGTH) { options.onError('too-long'); return }
      options.append(text)
    } catch {
      if (current()) options.onError('failed')
    } finally {
      if (operation === generation) busy.value = false
    }
  }

  /**
   * Resolve native Desktop drag payloads to local path references. Files
   * without a native path are returned to the existing byte-attachment path;
   * this function never reads a File or creates an attachment state.
   */
  async function appendNativeDrop(
    files: readonly File[],
    isImage: (file: File) => boolean,
  ): Promise<File[] | null> {
    const resolver = options.resolveNativeFilePath
    if (!resolver || !nativeDropAvailable.value || busy.value) return [...files]
    const operation = ++generation
    busy.value = true
    const current = () => operation === generation && nativeDropAvailable.value
    try {
      const resolved = await Promise.all(files.map(async file => {
        if (isImage(file)) return { file, path: null as string | null, native: false }
        try {
          const path = await resolver(file)
          if (typeof path !== 'string' || !path || path.length > MAX_NATIVE_PATH_LENGTH
            || path.trim() !== path || /[\u0000-\u001f\u007f]/.test(path)) {
            return { file, path: null as string | null, native: false }
          }
          return { file, path, native: true }
        } catch {
          return { file, path: null as string | null, native: false }
        }
      }))
      if (!current()) return null
      const pathEntries = resolved.filter(entry => entry.native && entry.path)
      const fallback = resolved.filter(entry => !entry.native).map(entry => entry.file)
      if (pathEntries.length > MAX_NATIVE_DROP_PATHS) {
        options.onError('too-many')
        return fallback
      }
      const paths = pathEntries.map(entry => entry.path!)
      if (paths.length > 0) {
        const text = paths.join('\n')
        const existing = options.text()
        const nextLength = existing.trim() ? existing.trimEnd().length + 1 + text.length : text.length
        if (nextLength > MAX_COMPOSER_LENGTH) {
          options.onError('too-long')
          return fallback
        }
        options.append(text)
      }
      return fallback
    } finally {
      if (operation === generation) busy.value = false
    }
  }

  return { available, nativeDropAvailable, busy, choose, appendNativeDrop, cancel }
}
