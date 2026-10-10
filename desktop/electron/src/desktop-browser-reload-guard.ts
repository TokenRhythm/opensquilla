/** Serialize browser tab closure before reloading the Control UI. */
export function createBrowserReloadGuard(
  closeBrowsers: () => Promise<boolean>,
): (reload: () => void) => Promise<boolean> {
  let pending: Promise<boolean> | undefined

  return reload => {
    if (pending) return pending
    const attempt = Promise.resolve()
      .then(closeBrowsers)
      .then(accepted => {
        if (!accepted) return false
        reload()
        return true
      })
      .catch(() => false)
    pending = attempt
    void attempt.then(() => {
      if (pending === attempt) pending = undefined
    })
    return attempt
  }
}
