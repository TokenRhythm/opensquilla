/** An ephemeral input draft; never evidence that a tool ran or wrote a file. */
export interface ToolInputPreview {
  previewId: string
  attemptId: string
  toolUseId: string
  generationEpoch: number
  revision: number
  state: 'generating' | 'committed' | 'discarded'
  toolName: 'write_file' | 'execute_code'
  path: string
  content: string
  truncated: boolean
}

export interface LiveToolInputPreview extends ToolInputPreview {
  activityOrder: number
}
