import hljs from 'highlight.js/lib/common'

const languages: Record<string, string> = {
  js: 'javascript', jsx: 'javascript', ts: 'typescript', tsx: 'typescript',
  json: 'json', py: 'python', css: 'css', html: 'xml', xml: 'xml', vue: 'xml',
  sh: 'bash', bash: 'bash', ps1: 'powershell', yml: 'yaml', yaml: 'yaml',
  md: 'markdown', rs: 'rust', go: 'go', java: 'java', c: 'c', h: 'c', cpp: 'cpp',
  sql: 'sql', toml: 'ini', ini: 'ini', rb: 'ruby', php: 'php', swift: 'swift',
}
const escape = (value: string) => value.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')

/** Bounded highlighting; preserve multiline tokens while returning balanced, inert line fragments. */
export function highlightWorkspaceLines(path: string, lines: string[]): string[] {
  const language = languages[path.split('.').pop()?.toLowerCase() || '']
  const plain = () => lines.map(escape)
  const text = lines.join('\n')
  if (!language || !hljs.getLanguage(language) || text.length > 30_000) return plain()
  try {
    const html = hljs.highlight(text, { language, ignoreIllegals: true }).value
    const stack: string[] = []
    const result: string[] = []
    let line = ''
    for (const part of html.split(/(<span class="[\w -]+">|<\/span>|\n)/)) {
      if (part === '\n') {
        result.push(line + '</span>'.repeat(stack.length))
        line = stack.join('')
      } else {
        if (part.startsWith('<span ')) stack.push(part)
        else if (part === '</span>') stack.pop()
        // highlight.js escapes source text; only its generated span markup is retained.
        line += part
      }
    }
    result.push(line + '</span>'.repeat(stack.length))
    return result.length === lines.length ? result : plain()
  } catch { return plain() }
}
