import { computed, ref } from 'vue'
import { localPathPresentation } from '@/types/localPathReferences'

/** The wire format is still ordinary message text, not an uploaded attachment. */
export function composeLocalPathText(text: string, paths: readonly string[]): string {
  if (!paths.length) return text
  const prefix = text.trim() ? `${text.trimEnd()}\n` : ''
  return prefix + paths.join('\n')
}

/** Only explicit native selections become chips; never infer paths from typed/history text. */
export function useLocalPathDraft() {
  const composerText = ref('')
  const localPaths = ref<string[]>([])
  // Existing send snapshots, queue/WAL and recovery keep the full immutable text.
  // Replacing that text (e.g. editing a historical message) safely restores plain text.
  const inputText = computed({
    get: () => composeLocalPathText(composerText.value, localPaths.value),
    set: (text: string) => {
      // Retry recovery may write back the unchanged draft to preserve newer
      // input. Keep its explicit presentation metadata in that case too.
      if (text === composeLocalPathText(composerText.value, localPaths.value)) return
      localPaths.value = []
      composerText.value = text
    },
  })

  function appendLocalPaths(text: string) {
    localPaths.value = [...new Set([...localPaths.value, ...text.split('\n')])]
  }

  function removeLocalPath(index: number) {
    localPaths.value = localPaths.value.filter((_, i) => i !== index)
  }

  function restoreInput(text: string, paths?: readonly string[]) {
    const restored = localPathPresentation(text, paths)
    composerText.value = restored.text
    localPaths.value = restored.paths
  }

  return { composerText, localPaths, inputText, appendLocalPaths, removeLocalPath, restoreInput }
}
