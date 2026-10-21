import { parseCsvPreview } from './csvPreview'
self.onmessage = (event: MessageEvent<{ source: string; delimiter: string }>) => {
  try { self.postMessage({ result: parseCsvPreview(event.data.source, event.data.delimiter) }) }
  catch { self.postMessage({ error: true }) }
}
