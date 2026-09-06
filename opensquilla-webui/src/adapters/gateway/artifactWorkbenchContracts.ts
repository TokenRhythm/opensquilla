import {
  ARTIFACTS_CHANGES_GET_METHOD,
} from '@/contracts/generated/v4/artifactsChangesGet'
import { validateResult as validateArtifactsChangesGet } from '@/contracts/generated/v4/artifactsChangesGetValidators.mjs'
import {
  ARTIFACTS_CHANGES_LIST_METHOD,
} from '@/contracts/generated/v4/artifactsChangesList'
import { validateResult as validateArtifactsChangesList } from '@/contracts/generated/v4/artifactsChangesListValidators.mjs'
import {
  ARTIFACTS_CHANGES_REVERT_METHOD,
} from '@/contracts/generated/v4/artifactsChangesRevert'
import { validateResult as validateArtifactsChangesRevert } from '@/contracts/generated/v4/artifactsChangesRevertValidators.mjs'
import {
  ARTIFACTS_DOCUMENTS_CLOSE_METHOD,
} from '@/contracts/generated/v4/artifactsDocumentsClose'
import { validateResult as validateArtifactsDocumentsClose } from '@/contracts/generated/v4/artifactsDocumentsCloseValidators.mjs'
import {
  ARTIFACTS_DOCUMENTS_GET_METHOD,
} from '@/contracts/generated/v4/artifactsDocumentsGet'
import { validateResult as validateArtifactsDocumentsGet } from '@/contracts/generated/v4/artifactsDocumentsGetValidators.mjs'
import {
  ARTIFACTS_DOCUMENTS_LIST_METHOD,
} from '@/contracts/generated/v4/artifactsDocumentsList'
import { validateResult as validateArtifactsDocumentsList } from '@/contracts/generated/v4/artifactsDocumentsListValidators.mjs'
import {
  ARTIFACTS_DOCUMENTS_OPEN_METHOD,
} from '@/contracts/generated/v4/artifactsDocumentsOpen'
import { validateResult as validateArtifactsDocumentsOpen } from '@/contracts/generated/v4/artifactsDocumentsOpenValidators.mjs'
import {
  ARTIFACTS_DOCUMENTS_RENAME_METHOD,
} from '@/contracts/generated/v4/artifactsDocumentsRename'
import { validateResult as validateArtifactsDocumentsRename } from '@/contracts/generated/v4/artifactsDocumentsRenameValidators.mjs'
import {
  ARTIFACTS_EDIT_CAPABILITIES_METHOD,
} from '@/contracts/generated/v4/artifactsEditCapabilities'
import { validateResult as validateArtifactsEditCapabilities } from '@/contracts/generated/v4/artifactsEditCapabilitiesValidators.mjs'
import { ARTIFACTS_GET_METHOD } from '@/contracts/generated/v4/artifactsGet'
import { validateResult as validateArtifactsGet } from '@/contracts/generated/v4/artifactsGetValidators.mjs'
import {
  ARTIFACTS_MUTATIONS_RESOLVE_METHOD,
} from '@/contracts/generated/v4/artifactsMutationsResolve'
import { validateResult as validateArtifactsMutationsResolve } from '@/contracts/generated/v4/artifactsMutationsResolveValidators.mjs'
import {
  ARTIFACTS_PROMPT_ANNOTATIONS_CREATE_METHOD,
} from '@/contracts/generated/v4/artifactsPromptAnnotationsCreate'
import { validateResult as validatePromptAnnotationsCreate } from '@/contracts/generated/v4/artifactsPromptAnnotationsCreateValidators.mjs'
import {
  ARTIFACTS_PROMPT_ANNOTATIONS_DISCARD_METHOD,
} from '@/contracts/generated/v4/artifactsPromptAnnotationsDiscard'
import { validateResult as validatePromptAnnotationsDiscard } from '@/contracts/generated/v4/artifactsPromptAnnotationsDiscardValidators.mjs'
import {
  ARTIFACTS_PROMPT_ANNOTATIONS_FOCUS_METHOD,
} from '@/contracts/generated/v4/artifactsPromptAnnotationsFocus'
import { validateResult as validatePromptAnnotationsFocus } from '@/contracts/generated/v4/artifactsPromptAnnotationsFocusValidators.mjs'
import {
  ARTIFACTS_PROMPT_ANNOTATIONS_LIST_METHOD,
} from '@/contracts/generated/v4/artifactsPromptAnnotationsList'
import { validateResult as validatePromptAnnotationsList } from '@/contracts/generated/v4/artifactsPromptAnnotationsListValidators.mjs'
import {
  ARTIFACTS_PROMPT_ANNOTATIONS_UPDATE_METHOD,
} from '@/contracts/generated/v4/artifactsPromptAnnotationsUpdate'
import { validateResult as validatePromptAnnotationsUpdate } from '@/contracts/generated/v4/artifactsPromptAnnotationsUpdateValidators.mjs'
import {
  ARTIFACTS_REVISIONS_LIST_METHOD,
} from '@/contracts/generated/v4/artifactsRevisionsList'
import { validateResult as validateArtifactsRevisionsList } from '@/contracts/generated/v4/artifactsRevisionsListValidators.mjs'
import {
  ARTIFACTS_REVISIONS_RESTORE_METHOD,
} from '@/contracts/generated/v4/artifactsRevisionsRestore'
import { validateResult as validateArtifactsRevisionsRestore } from '@/contracts/generated/v4/artifactsRevisionsRestoreValidators.mjs'
import {
  ARTIFACTS_SOURCE_PATCH_METHOD,
} from '@/contracts/generated/v4/artifactsSourcePatch'
import { validateResult as validateArtifactsSourcePatch } from '@/contracts/generated/v4/artifactsSourcePatchValidators.mjs'
import {
  ARTIFACTS_SOURCE_READ_METHOD,
} from '@/contracts/generated/v4/artifactsSourceRead'
import { validateResult as validateArtifactsSourceRead } from '@/contracts/generated/v4/artifactsSourceReadValidators.mjs'
import {
  DOCUMENT_STATE_CHANGED_EVENT_METADATA,
} from '@/contracts/generated/v4/artifactDocumentChangedEvent'
import { validatePayload as validateDocumentStateChangedPayload } from '@/contracts/generated/v4/artifactDocumentChangedEventValidators.mjs'
import {
  DOCUMENTS_EDIT_SESSIONS_CLOSE_METHOD,
} from '@/contracts/generated/v4/documentsEditSessionsClose'
import { validateResult as validateEditSessionsClose } from '@/contracts/generated/v4/documentsEditSessionsCloseValidators.mjs'
import {
  DOCUMENTS_EDIT_SESSIONS_HEARTBEAT_METHOD,
} from '@/contracts/generated/v4/documentsEditSessionsHeartbeat'
import { validateResult as validateEditSessionsHeartbeat } from '@/contracts/generated/v4/documentsEditSessionsHeartbeatValidators.mjs'
import {
  DOCUMENTS_EDIT_SESSIONS_START_METHOD,
} from '@/contracts/generated/v4/documentsEditSessionsStart'
import { validateResult as validateEditSessionsStart } from '@/contracts/generated/v4/documentsEditSessionsStartValidators.mjs'
import { DOCUMENTS_IMPORT_METHOD } from '@/contracts/generated/v4/documentsImport'
import { validateResult as validateDocumentsImport } from '@/contracts/generated/v4/documentsImportValidators.mjs'
import { DOCUMENTS_PUBLISH_METHOD } from '@/contracts/generated/v4/documentsPublish'
import { validateResult as validateDocumentsPublish } from '@/contracts/generated/v4/documentsPublishValidators.mjs'
import {
  WORKBENCH_PREVIEWS_CREATE_METHOD,
} from '@/contracts/generated/v4/workbenchPreviewsCreate'
import { validateResult as validateWorkbenchPreviewsCreate } from '@/contracts/generated/v4/workbenchPreviewsCreateValidators.mjs'
import {
  WORKBENCH_RESOURCES_GET_METHOD,
} from '@/contracts/generated/v4/workbenchResourcesGet'
import { validateResult as validateWorkbenchResourcesGet } from '@/contracts/generated/v4/workbenchResourcesGetValidators.mjs'
import {
  WORKBENCH_RESOURCES_LIST_METHOD,
} from '@/contracts/generated/v4/workbenchResourcesList'
import { validateResult as validateWorkbenchResourcesList } from '@/contracts/generated/v4/workbenchResourcesListValidators.mjs'
import {
  WORKBENCH_RESOURCES_OPEN_METHOD,
} from '@/contracts/generated/v4/workbenchResourcesOpen'
import { validateResult as validateWorkbenchResourcesOpen } from '@/contracts/generated/v4/workbenchResourcesOpenValidators.mjs'

export interface WorkbenchContractDescriptor {
  readonly method: string
  readonly validateResult: (value: unknown) => boolean
}

const descriptor = (
  method: string,
  validateResult: (value: unknown) => boolean,
): WorkbenchContractDescriptor => ({ method, validateResult })

export const artifactDocumentContracts = {
  capabilities: descriptor(ARTIFACTS_EDIT_CAPABILITIES_METHOD, validateArtifactsEditCapabilities),
  documentsList: descriptor(ARTIFACTS_DOCUMENTS_LIST_METHOD, validateArtifactsDocumentsList),
  documentsGet: descriptor(ARTIFACTS_DOCUMENTS_GET_METHOD, validateArtifactsDocumentsGet),
  documentsOpen: descriptor(ARTIFACTS_DOCUMENTS_OPEN_METHOD, validateArtifactsDocumentsOpen),
  documentsClose: descriptor(ARTIFACTS_DOCUMENTS_CLOSE_METHOD, validateArtifactsDocumentsClose),
  documentsRename: descriptor(ARTIFACTS_DOCUMENTS_RENAME_METHOD, validateArtifactsDocumentsRename),
  revisionsList: descriptor(ARTIFACTS_REVISIONS_LIST_METHOD, validateArtifactsRevisionsList),
  revisionsRestore: descriptor(ARTIFACTS_REVISIONS_RESTORE_METHOD, validateArtifactsRevisionsRestore),
  changesList: descriptor(ARTIFACTS_CHANGES_LIST_METHOD, validateArtifactsChangesList),
  changesGet: descriptor(ARTIFACTS_CHANGES_GET_METHOD, validateArtifactsChangesGet),
  changesRevert: descriptor(ARTIFACTS_CHANGES_REVERT_METHOD, validateArtifactsChangesRevert),
  sourceRead: descriptor(ARTIFACTS_SOURCE_READ_METHOD, validateArtifactsSourceRead),
  sourcePatch: descriptor(ARTIFACTS_SOURCE_PATCH_METHOD, validateArtifactsSourcePatch),
  mutationResolve: descriptor(ARTIFACTS_MUTATIONS_RESOLVE_METHOD, validateArtifactsMutationsResolve),
  editSessionStart: descriptor(DOCUMENTS_EDIT_SESSIONS_START_METHOD, validateEditSessionsStart),
  editSessionHeartbeat: descriptor(
    DOCUMENTS_EDIT_SESSIONS_HEARTBEAT_METHOD,
    validateEditSessionsHeartbeat,
  ),
  editSessionClose: descriptor(DOCUMENTS_EDIT_SESSIONS_CLOSE_METHOD, validateEditSessionsClose),
  legacyGet: descriptor(ARTIFACTS_GET_METHOD, validateArtifactsGet),
} as const

export const promptAnnotationContracts = {
  create: descriptor(ARTIFACTS_PROMPT_ANNOTATIONS_CREATE_METHOD, validatePromptAnnotationsCreate),
  list: descriptor(ARTIFACTS_PROMPT_ANNOTATIONS_LIST_METHOD, validatePromptAnnotationsList),
  update: descriptor(ARTIFACTS_PROMPT_ANNOTATIONS_UPDATE_METHOD, validatePromptAnnotationsUpdate),
  discard: descriptor(ARTIFACTS_PROMPT_ANNOTATIONS_DISCARD_METHOD, validatePromptAnnotationsDiscard),
  focus: descriptor(ARTIFACTS_PROMPT_ANNOTATIONS_FOCUS_METHOD, validatePromptAnnotationsFocus),
} as const

export const workbenchResourceContracts = {
  list: descriptor(WORKBENCH_RESOURCES_LIST_METHOD, validateWorkbenchResourcesList),
  get: descriptor(WORKBENCH_RESOURCES_GET_METHOD, validateWorkbenchResourcesGet),
  open: descriptor(WORKBENCH_RESOURCES_OPEN_METHOD, validateWorkbenchResourcesOpen),
  createPreview: descriptor(WORKBENCH_PREVIEWS_CREATE_METHOD, validateWorkbenchPreviewsCreate),
  importDocument: descriptor(DOCUMENTS_IMPORT_METHOD, validateDocumentsImport),
  publishDocument: descriptor(DOCUMENTS_PUBLISH_METHOD, validateDocumentsPublish),
  mutationResolve: artifactDocumentContracts.mutationResolve,
} as const

export const documentChangeEventContract: {
  readonly wireNames: readonly string[]
  readonly validatePayload: (value: unknown) => boolean
} = {
  wireNames: [...DOCUMENT_STATE_CHANGED_EVENT_METADATA.wireNames],
  validatePayload: value => validateDocumentStateChangedPayload(value),
}

export function acceptsWorkbenchResult(
  contract: WorkbenchContractDescriptor,
  value: unknown,
): boolean {
  if (contract.validateResult(value)) return true
  // Older v4 gateways used snake_case fields but always returned an object.
  // Keep only this additive shape fallback; primitives and arrays fail closed.
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}
