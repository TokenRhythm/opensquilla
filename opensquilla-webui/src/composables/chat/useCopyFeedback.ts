import { computed, onBeforeUnmount, ref } from 'vue'
import i18n from '@/i18n'
import type { IconName } from '@/utils/icons'

const COPY_FEEDBACK_RESET_MS = 1200

export function useCopyFeedback(copy: () => Promise<boolean>) {
  const copyState = ref<'ok' | 'err' | null>(null)
  let resetTimer: ReturnType<typeof setTimeout> | null = null
  let copyGeneration = 0

  async function onCopyClick() {
    const generation = ++copyGeneration
    const ok = await copy()
    if (generation !== copyGeneration) return
    copyState.value = ok ? 'ok' : 'err'
    if (resetTimer) clearTimeout(resetTimer)
    resetTimer = setTimeout(() => {
      copyState.value = null
    }, COPY_FEEDBACK_RESET_MS)
  }

  onBeforeUnmount(() => {
    copyGeneration += 1
    if (resetTimer) clearTimeout(resetTimer)
  })

  const copyIconName = computed<IconName>(() =>
    copyState.value === 'ok' ? 'check' : copyState.value === 'err' ? 'x' : 'copy',
  )
  const copyTitle = computed(() =>
    copyState.value === 'ok' ? i18n.global.t('chat.copied') : copyState.value === 'err' ? i18n.global.t('chat.toast.copyFailed') : i18n.global.t('chat.copy'),
  )
  const copyLiveText = computed(() =>
    copyState.value === 'ok' ? i18n.global.t('chat.copied') : copyState.value === 'err' ? i18n.global.t('chat.toast.copyFailed') : '',
  )

  return { copyState, copyIconName, copyTitle, copyLiveText, onCopyClick }
}
