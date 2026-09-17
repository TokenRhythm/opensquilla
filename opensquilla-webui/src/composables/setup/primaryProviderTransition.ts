import i18n from '@/i18n'
import { useConfirm } from '@/composables/useConfirm'
import { SetupWorkflowError, type RouterResolutionAction } from '@/modules/setupWorkflow'
import { ProviderConfigurationError } from '@/modules/providerConfiguration'

/** Only typed server evidence can authorize a conflict resolution. */
export async function chooseRouterConflictAction(
  error: unknown,
  providerLabel: (id: string) => string = id => id,
  allowRecommended = true,
): Promise<RouterResolutionAction | null> {
  if (!(error instanceof SetupWorkflowError || error instanceof ProviderConfigurationError)
    || !error.details || error.details.reason !== 'router_provider_conflict') throw error
  const actions = (['use_recommended', 'disable'] as const)
    .filter(action => error.details!.allowedRouterActions.includes(action)
      && (action !== 'use_recommended' || allowRecommended))
  if (!actions.length) throw error
  const t = i18n.global.t
  const label = (action: RouterResolutionAction) => t(action === 'use_recommended'
    ? 'setup.provider.routerUseRecommended' : 'setup.provider.routerDisablePreserve')
  const choice = await useConfirm().confirmChoice({
    title: t('setup.provider.routerConflictTitle'),
    body: t('setup.provider.routerConflictBody', {
      provider: providerLabel(error.details.providerId),
      conflicts: error.details.conflictProviders.map(providerLabel).join(', '),
    }),
    primaryLabel: label(actions[0]!),
    secondaryLabel: actions[1] ? label(actions[1]) : undefined,
    primaryClass: 'btn--primary',
    secondaryClass: 'btn--ghost',
    showCancel: true,
  })
  return choice === 'cancel' ? null : choice === 'primary' ? actions[0]! : actions[1] ?? null
}

/** All primary changes ask the same question, after the server validates a frozen request. */
export async function submitPrimaryProviderTransition<T extends object, R>(
  command: T,
  submit: (snapshot: T) => Promise<R>,
  providerLabel: (id: string) => string = id => id,
): Promise<{ result: R; routerAction?: RouterResolutionAction } | null> {
  // Clone plain wire data: callers may keep editing drafts while a dialog is open.
  const snapshot: T = JSON.parse(JSON.stringify(command))
  try {
    return { result: await submit(snapshot) }
  } catch (error) {
    if (!(error instanceof SetupWorkflowError) || !error.details
      || error.reason !== 'router-provider-conflict') throw error
    const routerAction = await chooseRouterConflictAction(error, providerLabel)
    if (!routerAction) return null
    // A second error is surfaced; no automatic retry after uncertain network results.
    return { result: await submit({ ...snapshot, routerAction }), routerAction }
  }
}
