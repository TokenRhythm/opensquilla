const TIME_PREFIX_RE = /^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}[+\-]\d{2}:\d{2} (?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) [A-Za-z0-9_+\-/]+\]\n/

export function stripChatTimePrefix(text: string): string {
  return typeof text === 'string' ? text.replace(TIME_PREFIX_RE, '') : text
}
