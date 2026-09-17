interface MetaSetupNavigationRouter {
  replace: (target: { path: string; query: Record<string, string> }) => Promise<unknown>
  push: (target: { path: string; hash: string }) => Promise<unknown>
}

export interface MetaSetupProviderNavigationOptions {
  providerId: string
  sessionKey: string
  currentRouteSession: unknown
  router: MetaSetupNavigationRouter
  beginHandoff: (providerId: string) => string
  cancelHandoff: (providerId: string, clientRequestId: string) => void
  materializeSession: (sessionKey: string) => void
}

/**
 * Leave chat for a provider connection without losing a provisional draft or
 * leaving an auto-resume marker behind after a failed navigation.
 */
export async function navigateMetaSetupProviderSettings(
  options: MetaSetupProviderNavigationOptions,
): Promise<boolean> {
  const providerId = options.providerId
  if (!options.sessionKey) return false
  let clientRequestId = ''

  try {
    if (options.currentRouteSession !== options.sessionKey) {
      const materializationFailure = await options.router.replace({
        path: '/chat',
        query: { session: options.sessionKey },
      })
      if (materializationFailure) return false
      options.materializeSession(options.sessionKey)
    }

    // Materializing /chat/new can remount ChatView. Write the handoff only
    // after that navigation so the intermediate mount cannot consume it before
    // the user has had a chance to configure the provider.
    clientRequestId = options.beginHandoff(providerId)
    if (!clientRequestId) return false

    const navigationFailure = await options.router.push({
      path: '/settings/provider',
      hash: `#provider-${encodeURIComponent(providerId)}`,
    })
    if (navigationFailure) {
      options.cancelHandoff(providerId, clientRequestId)
      return false
    }
    return true
  } catch {
    if (clientRequestId) options.cancelHandoff(providerId, clientRequestId)
    return false
  }
}
