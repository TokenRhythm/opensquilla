import Papa from 'papaparse'
export interface CsvPreview { rows: string[][]; truncated: boolean; columns: number }
/** Parsing never evaluates formulas or creates HTML. The view virtualizes these bounded rows. */
export function parseCsvPreview(source: string, delimiter = ''): CsvPreview {
  const rows: string[][] = []
  let cells = 0, columns = 0, truncated = false
  Papa.parse<string[]>(source, {
    delimiter, skipEmptyLines: true,
    step(result, parser) {
      const row = result.data
      if (rows.length >= 10000 || row.length > 200 || cells + row.length > 200000) {
        truncated = true; parser.abort(); return
      }
      cells += row.length; columns = Math.max(columns, row.length); rows.push(row)
    },
  })
  return { rows, columns, truncated }
}
