import { toRaw } from 'vue'

/** Bound departure work before JSON allocates an entire tool-result payload. */
export function createChatViewCaptureBudget(maxBytes: number) {
  let bytes = 2 // enclosing object
  let fields = 0
  let nodes = 0
  let objectProperties = 0
  const ancestors = new WeakSet<object>()
  const exhausted = Symbol('view-capture-budget')
  const add = (count: number) => {
    bytes += count
    if (bytes > maxBytes) throw exhausted
  }
  function string(value: string) {
    // UTF-8 JSON is never shorter than its UTF-16 code-unit count. Reject giant
    // values in O(1), without encoding or escaping their complete contents.
    if (value.length > maxBytes - bytes) throw exhausted
    add(2)
    for (let index = 0; index < value.length; index++) {
      const code = value.charCodeAt(index)
      if (code === 0x22 || code === 0x5c) add(2)
      else if (code < 0x20) add([8, 9, 10, 12, 13].includes(code) ? 2 : 6)
      else if (code < 0x80) add(1)
      else if (code < 0x800) add(2)
      else if (code >= 0xd800 && code <= 0xdbff) {
        const next = value.charCodeAt(index + 1)
        if (next >= 0xdc00 && next <= 0xdfff) { add(4); index++ }
        else add(6) // JSON escapes lone surrogates
      } else add(code >= 0xdc00 && code <= 0xdfff ? 6 : 3)
    }
  }
  function walk(value: unknown, depth: number) {
    if (++nodes > 20_000 || depth > 32) throw exhausted
    if (value === null) { add(4); return }
    if (typeof value === 'string') { string(value); return }
    if (typeof value === 'boolean') { add(value ? 4 : 5); return }
    if (typeof value === 'number') { add(Number.isFinite(value) ? String(value).length : 4); return }
    if (typeof value !== 'object') throw exhausted
    const raw = toRaw(value)
    if (ancestors.has(raw)) throw exhausted
    const array = Array.isArray(raw)
    if (!array && Object.getPrototypeOf(raw) !== Object.prototype && Object.getPrototypeOf(raw) !== null) throw exhausted
    // Cache data only. Accessors/toJSON could perform unbounded work or return
    // a different payload after this preflight; they are not presentation data.
    if ('toJSON' in raw) throw exhausted
    ancestors.add(raw)
    add(2)
    if (array) {
      if (raw.length > 20_000 - nodes) throw exhausted
      for (let index = 0; index < raw.length; index++) {
        if (index) add(1)
        const descriptor = Object.getOwnPropertyDescriptor(raw, String(index))
        if (descriptor && !('value' in descriptor)) throw exhausted
        walk(descriptor?.value === undefined ? null : descriptor.value, depth + 1)
      }
    } else {
      let count = 0
      for (const key in raw) {
        // Omitted undefined fields still cost traversal work, even though
        // JSON does not emit them or visit their values.
        if (++objectProperties > 20_000) throw exhausted
        const descriptor = Object.getOwnPropertyDescriptor(raw, key)
        if (!descriptor) continue
        if (!('value' in descriptor)) throw exhausted
        if (descriptor.value === undefined) continue
        if (count++) add(1)
        string(key); add(1); walk(descriptor.value, depth + 1)
      }
    }
    ancestors.delete(raw)
  }
  return {
    get bytes() { return bytes },
    include(key: string, value: unknown): boolean {
      if (value === undefined) return true
      try {
        if (fields++) add(1)
        string(key); add(1); walk(value, 0)
        return true
      } catch {
        return false
      }
    },
  }
}
