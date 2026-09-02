// Compatibility exports for pure parsing and focused contract tests.
// Production staged downloads are composed through ArtifactWorkbench.
export {
  attachmentAccessUrl,
  attachmentAccessHeaders,
  fetchDisplayAttachmentBlob,
} from '@/adapters/gateway/attachmentAccessV4'
export type {
  AttachmentDownloadOptions,
  AttachmentDownloadResult,
} from '@/adapters/gateway/attachmentAccessV4'
