import type { WorkbenchResourceCapabilities } from '@/types/workbenchResources'

export type WorkbenchResourceAction = 'preview' | 'edit'

export function workbenchResourceActionReasonCode(
  capabilities: WorkbenchResourceCapabilities,
  action: WorkbenchResourceAction,
): string | null {
  if (action === 'preview') {
    return capabilities.previewReasonCode || capabilities.reasonCode || null
  }
  return capabilities.editReasonCode || capabilities.reasonCode || null
}

export function workbenchResourceUnavailableReasonKey(reasonCode: string | null | undefined) {
  switch (reasonCode) {
    case 'html_encoding_unsupported':
      return 'workbench.resources.unavailableReasons.htmlEncodingUnsupported'
    case 'html_validation_failed':
      return 'workbench.resources.unavailableReasons.htmlValidationFailed'
    case 'html_edit_size_unsupported':
      return 'workbench.resources.unavailableReasons.htmlEditTooLarge'
    case 'html_preview_size_unsupported':
      return 'workbench.resources.unavailableReasons.htmlPreviewTooLarge'
    case 'office_adapter_not_available':
      return 'workbench.resources.unavailableReasons.officeAdapterNotAvailable'
    default:
      return 'workbench.resources.unavailableReasons.unsupported'
  }
}
