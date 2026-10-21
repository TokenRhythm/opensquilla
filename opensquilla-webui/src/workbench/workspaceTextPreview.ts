import type { WorkspaceFilePage } from '@/modules/workspaceFiles'

/** Full Markdown has a separate render budget; source remains paged above it. */
export const WORKSPACE_MARKDOWN_PREVIEW_LIMIT = 128_000

export function isWorkspaceMarkdown(file: { name: string; mime: string }): boolean {
  return /\.(md|markdown|mdown|mkd)$/i.test(file.name)
    || file.mime.split(';', 1)[0]!.trim().toLowerCase() === 'text/markdown'
}

export async function completeWorkspaceMarkdown(path: string, blob: Blob): Promise<WorkspaceFilePage> {
  if (blob.size > WORKSPACE_MARKDOWN_PREVIEW_LIMIT) throw new Error('workbench.artifactPreview.tooLarge')
  const content = new TextDecoder('utf-8', { fatal: true }).decode(await blob.arrayBuffer())
  if (content.includes('\0')) throw new Error('workspaceReference.unsupported')
  const lines = content.split(/\r\n|[\n\v\f\r\x1c-\x1e\x85\u2028\u2029]/)
  if (lines.length > 1 && lines[lines.length - 1] === '') lines.pop()
  return { relativePath: path, content, startLine: 1, endLine: lines.length, totalLines: lines.length }
}
