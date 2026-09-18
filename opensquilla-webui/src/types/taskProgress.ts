export interface ExecutionProgress {
  explanation: string | null
  steps: Array<{ text: string; status: 'pending' | 'in_progress' | 'completed' }>
}

export interface TaskProgressSnapshot extends ExecutionProgress {
  revision: number
}
