import { ref } from 'vue'
import i18n from '@/i18n'

export interface ConfirmOptions {
  title: string
  body: string
  primaryLabel?: string
  primaryClass?: string
}

export interface ConfirmChoiceOptions extends ConfirmOptions {
  secondaryLabel: string
  secondaryClass?: string
  showCancel?: boolean
}

export type ConfirmChoice = 'cancel' | 'secondary' | 'primary'

type ConfirmResult = boolean | 'secondary'

interface ConfirmRequest extends ConfirmOptions {
  primaryLabel: string
  primaryClass: string
  secondaryLabel?: string
  secondaryClass?: string
  showCancel?: boolean
  resolve: (value: ConfirmResult) => void
}

// Module-level singleton so any composable or component can raise a confirm
// dialog without prop drilling; the globally-mounted ConfirmModal renders the
// shared request and resolves the promise. Mirrors useToasts.
const confirmState = ref<ConfirmRequest | null>(null)

function confirm(options: ConfirmOptions): Promise<boolean> {
  // A pending request is resolved as cancelled before a new one replaces it so
  // its awaiter never hangs.
  if (confirmState.value) {
    confirmState.value.resolve(false)
  }
  return new Promise<boolean>(resolve => {
    confirmState.value = {
      title: options.title,
      body: options.body,
      primaryLabel: options.primaryLabel ?? i18n.global.t('shared.confirm.defaultPrimary'),
      primaryClass: options.primaryClass ?? 'btn--danger',
      resolve: value => resolve(value === true),
    }
  })
}

// A small extension of the standard confirmation model for exits that need a
// third, non-destructive action (for example, save before closing). Keep the
// boolean `confirm` API above intact for existing destructive confirmations.
function confirmChoice(options: ConfirmChoiceOptions): Promise<ConfirmChoice> {
  if (confirmState.value) {
    confirmState.value.resolve(false)
  }
  return new Promise<ConfirmChoice>(resolve => {
    confirmState.value = {
      title: options.title,
      body: options.body,
      primaryLabel: options.primaryLabel ?? i18n.global.t('shared.confirm.defaultPrimary'),
      primaryClass: options.primaryClass ?? 'btn--primary',
      secondaryLabel: options.secondaryLabel,
      secondaryClass: options.secondaryClass ?? 'btn--danger',
      showCancel: options.showCancel,
      resolve: value => {
        if (value === true) resolve('primary')
        else if (value === 'secondary') resolve('secondary')
        else resolve('cancel')
      },
    }
  })
}

function resolveConfirm(ok: boolean) {
  const request = confirmState.value
  if (!request) return
  confirmState.value = null
  request.resolve(ok)
}

function resolveConfirmChoice(choice: Exclude<ConfirmChoice, 'cancel'>) {
  const request = confirmState.value
  if (!request) return
  confirmState.value = null
  request.resolve(choice === 'primary' ? true : 'secondary')
}

export function useConfirm() {
  return { confirm, confirmChoice, confirmState, resolveConfirm, resolveConfirmChoice }
}
