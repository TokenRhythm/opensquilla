import { execFile } from 'node:child_process'

// Keep the original execFile policy and error. Phase wrappers often retain only
// error.message, so the same subprocess evidence must also survive in that text.
export async function execFileWithDiagnostics(executable, args, options, {
  execFileImpl = execFile,
} = {}) {
  const startedAt = performance.now()
  let child
  const failure = (cause, stdout, stderr) => {
    const subprocess = {
      executable,
      args,
      pid: child?.pid ?? null,
      timeoutMs: options.timeout ?? null,
      killSignal: options.killSignal ?? null,
      elapsedMs: Math.round(performance.now() - startedAt),
      code: cause?.code ?? null,
      killed: cause?.killed ?? null,
      signal: cause?.signal ?? null,
      stdout: stdout == null ? null : String(stdout),
      stderr: stderr == null ? null : String(stderr),
    }
    const error = new Error(
      `${cause?.message ?? String(cause)}\nDESKTOP_E2E_SUBPROCESS_FAILED: ${JSON.stringify(subprocess)}`,
      { cause },
    )
    error.subprocess = subprocess
    return error
  }
  return await new Promise((resolve, reject) => {
    try {
      child = execFileImpl(executable, args, options, (error, stdout, stderr) => {
        if (error) reject(failure(error, stdout, stderr))
        else resolve({ stdout, stderr })
      })
    } catch (error) {
      reject(failure(error, null, null))
    }
  })
}
