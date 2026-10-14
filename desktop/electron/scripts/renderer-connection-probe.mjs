// The renderer projects its actual Gateway transport state here independently
// of the optional connection notice. A ready main-process descriptor or an
// editable composer alone does not prove that the renderer has connected.
export const RENDERER_CONNECTION_SELECTOR = '.topbar[data-connection-state]'

// Self-contained because Playwright serializes this function into the page.
export function rendererConnectionMatches(expected) {
  const shells = document.querySelectorAll('.topbar[data-connection-state]')
  if (shells.length !== 1) return false
  const state = shells[0].getAttribute('data-connection-state')
  if (expected === 'connected') return state === 'connected'
  return expected === 'not-connected' && (state === 'connecting' || state === 'disconnected')
}

export async function waitForRendererConnection(page, expected = 'connected', options = {}) {
  if (expected !== 'connected' && expected !== 'not-connected') {
    throw new TypeError('Expected connected or not-connected renderer transport')
  }
  // Missing/unmounted UI must never satisfy the child-crash observation.
  const observed = await page.waitForFunction(rendererConnectionMatches, expected, options)
  await observed.dispose()
}
