/** A closing native modal can disappear before Playwright receives its click ACK.
 * Keep this fixture-only exception narrower than a general target-closed catch;
 * callers must still verify the resulting production decision and retained data.
 */
export async function completeClosingDialogClick({ page, click, isApplicationRunning,
  dialogWebContentsId, expectedResponse, readDecisionReceipts }) {
  const targetWasOpen = !page.isClosed()
  const checkpoint = Math.max(0, ...(await readDecisionReceipts()).map(receipt => receipt.sequence))
  try {
    await click()
  } catch (error) {
    const closedTarget = error instanceof Error
      && error.message.split(/\r?\n/, 1)[0] === 'locator.click: Target page, context or browser has been closed'
    const actionLog = Array.isArray(error?.log) ? error.log : String(error?.message ?? '').split(/\r?\n/)
    const performing = actionLog.some(line => typeof line === 'string' && line.trim() === '- performing click action')
    if (!targetWasOpen || !closedTarget || !performing || !page.isClosed() || !isApplicationRunning()) throw error
    // Playwright logs "performing" before sending input. Only a fresh, parsed
    // production response from this modal proves the intended action arrived;
    // its native `closed` fallback to cancel is deliberately insufficient.
    let receipts
    try { receipts = await readDecisionReceipts() } catch { throw error }
    if (!receipts.some(receipt => receipt.sequence > checkpoint
      && receipt.webContentsId === dialogWebContentsId && receipt.response === expectedResponse)
      || !isApplicationRunning()) throw error
  }
}
