// Synthetic OOXML fixtures. No Office installation, authoring dependency,
// network access, user documents, or binary repository fixtures are required.
import { createHash } from 'node:crypto'

function crc32(bytes: Buffer): number {
  let crc = 0xffffffff
  for (const byte of bytes) {
    crc ^= byte
    for (let bit = 0; bit < 8; bit += 1) crc = (crc >>> 1) ^ (0xedb88320 & -(crc & 1))
  }
  return (crc ^ 0xffffffff) >>> 0
}

export function officeZip(parts: Record<string, string>): Buffer {
  const records: Buffer[] = []
  const directory: Buffer[] = []
  let offset = 0
  for (const [name, xml] of Object.entries(parts)) {
    const filename = Buffer.from(name)
    const payload = Buffer.from(xml)
    const checksum = crc32(payload)
    const header = Buffer.alloc(30)
    header.writeUInt32LE(0x04034b50)
    header.writeUInt16LE(20, 4)
    header.writeUInt16LE(0x800, 6)
    header.writeUInt32LE(checksum, 14)
    header.writeUInt32LE(payload.length, 18)
    header.writeUInt32LE(payload.length, 22)
    header.writeUInt16LE(filename.length, 26)
    records.push(header, filename, payload)
    const central = Buffer.alloc(46)
    central.writeUInt32LE(0x02014b50)
    central.writeUInt16LE(20, 4)
    central.writeUInt16LE(20, 6)
    central.writeUInt16LE(0x800, 8)
    central.writeUInt32LE(checksum, 16)
    central.writeUInt32LE(payload.length, 20)
    central.writeUInt32LE(payload.length, 24)
    central.writeUInt16LE(filename.length, 28)
    central.writeUInt32LE(offset, 42)
    directory.push(central, filename)
    offset += header.length + filename.length + payload.length
  }
  const central = Buffer.concat(directory)
  const end = Buffer.alloc(22)
  end.writeUInt32LE(0x06054b50)
  end.writeUInt16LE(Object.keys(parts).length, 8)
  end.writeUInt16LE(Object.keys(parts).length, 10)
  end.writeUInt32LE(central.length, 12)
  end.writeUInt32LE(offset, 16)
  return Buffer.concat([...records, central, end])
}

const relNs = 'http://schemas.openxmlformats.org/package/2006/relationships'
const officeRel = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
const typesNs = 'http://schemas.openxmlformats.org/package/2006/content-types'
const sheetNs = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
const drawingNs = 'http://schemas.openxmlformats.org/drawingml/2006/main'
const presentationNs = 'http://schemas.openxmlformats.org/presentationml/2006/main'
const wordNs = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
const font = 'Noto Sans SC'
const esc = (text: string) => text.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;')
const relationships = (items: [string, string, string][]) => `<Relationships xmlns="${relNs}">${items.map(([id, type, target]) => `<Relationship Id="${id}" Type="${officeRel}/${type}" Target="${target}"/>`).join('')}</Relationships>`
const types = (items: [string, string][]) => `<Types xmlns="${typesNs}"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>${items.map(([part, type]) => `<Override PartName="/${part}" ContentType="application/vnd.openxmlformats-officedocument.${type}+xml"/>`).join('')}</Types>`

export function docxFixture(title = '中文预览验收', pages = 2): Buffer {
  const paragraph = (text: string, id: number, bold = false) => `<w:p w14:paraId="${id.toString(16).padStart(8, '0')}"><w:r><w:rPr><w:rFonts w:ascii="${font}" w:hAnsi="${font}" w:eastAsia="${font}"/><w:sz w:val="${bold ? 40 : 24}"/>${bold ? '<w:b/>' : ''}</w:rPr><w:t>${esc(text)}</w:t></w:r></w:p>`
  const table = `<w:tbl><w:tblPr><w:tblW w:w="8000" w:type="dxa"/><w:tblBorders>${['top', 'left', 'bottom', 'right', 'insideH', 'insideV'].map(side => `<w:${side} w:val="single" w:sz="6" w:color="819BB5"/>`).join('')}</w:tblBorders></w:tblPr><w:tblGrid><w:gridCol w:w="4000"/><w:gridCol w:w="4000"/></w:tblGrid>${[['中文表格项目', '金额（元）'], ['预览测试', '1,234.50']].map((row, i) => `<w:tr>${row.map((text, j) => `<w:tc><w:tcPr><w:tcW w:w="4000" w:type="dxa"/>${i === 0 ? '<w:shd w:fill="DBE7F2"/>' : ''}</w:tcPr>${paragraph(text, 9000 + i * 2 + j)}</w:tc>`).join('')}</w:tr>`).join('')}</w:tbl><w:p/>`
  const body = Array.from({ length: pages }, (_, index) => `${index ? '<w:p><w:r><w:br w:type="page"/></w:r></w:p>' : ''}${paragraph(index ? `第 ${index + 1} 页：繁體與简体` : title, index * 10 + 1, true)}${paragraph('跨平台中文，标点、数字 1,234.50 与 English 混排。', index * 10 + 2)}${paragraph('请选择这段中文提出修改要求。', index * 10 + 3)}${index === 0 ? table : ''}`).join('')
  return officeZip({
    '[Content_Types].xml': types([['word/document.xml', 'wordprocessingml.document.main']]),
    '_rels/.rels': relationships([['rId1', 'officeDocument', 'word/document.xml']]),
    'word/document.xml': `<w:document xmlns:w="${wordNs}" xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"><w:body>${body}<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134"/></w:sectPr></w:body></w:document>`,
  })
}

export function pptxFixture(title = '中文预览验收', slides = 2, externalImage = false): Buffer {
  const parts: Record<string, string> = {
    '[Content_Types].xml': types([['ppt/presentation.xml', 'presentationml.presentation.main'], ...Array.from({ length: slides }, (_, i): [string, string] => [`ppt/slides/slide${i + 1}.xml`, 'presentationml.slide'])]),
    '_rels/.rels': relationships([['rId1', 'officeDocument', 'ppt/presentation.xml']]),
    'ppt/presentation.xml': `<p:presentation xmlns:p="${presentationNs}" xmlns:r="${officeRel}"><p:sldIdLst>${Array.from({ length: slides }, (_, i) => `<p:sldId id="${256 + i}" r:id="rId${i + 1}"/>`).join('')}</p:sldIdLst><p:sldSz cx="12192000" cy="6858000"/><p:notesSz cx="6858000" cy="9144000"/></p:presentation>`,
    'ppt/_rels/presentation.xml.rels': relationships(Array.from({ length: slides }, (_, i) => [`rId${i + 1}`, 'slide', `slides/slide${i + 1}.xml`])),
  }
  const shape = (id: number, text: string, y: number, size: number) => `<p:sp><p:nvSpPr><p:cNvPr id="${id}" name="Chinese text ${id}"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr><p:spPr><a:xfrm><a:off x="762000" y="${y}"/><a:ext cx="10668000" cy="1143000"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/></p:spPr><p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:rPr lang="zh-CN" sz="${size}"><a:solidFill><a:srgbClr val="193B60"/></a:solidFill><a:latin typeface="${font}"/><a:ea typeface="${font}"/></a:rPr><a:t>${esc(text)}</a:t></a:r></a:p></p:txBody></p:sp>`
  for (let i = 0; i < slides; i += 1) {
    parts[`ppt/slides/slide${i + 1}.xml`] = `<p:sld xmlns:p="${presentationNs}" xmlns:a="${drawingNs}" xmlns:r="${officeRel}"><p:cSld><p:bg><p:bgPr><a:solidFill><a:srgbClr val="FFFFFF"/></a:solidFill></p:bgPr></p:bg><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>${shape(4, i ? `第二页：繁體與简体 ${i + 1}` : title, 1524000, 3600)}${shape(5, '选择标题，提出修改要求。', 3048000, 2400)}</p:spTree></p:cSld></p:sld>`
  }
  if (externalImage) {
    parts['ppt/slides/_rels/slide1.xml.rels'] = `<Relationships xmlns="${relNs}"><Relationship Id="externalImage" Type="${officeRel}/image" Target="https://office-fixture.invalid/remote-image.png" TargetMode="External"/></Relationships>`
    parts['ppt/slides/slide1.xml'] = parts['ppt/slides/slide1.xml']!.replace('</p:spTree>', '<p:pic><p:nvPicPr><p:cNvPr id="9" name="External image"/><p:cNvPicPr/><p:nvPr/></p:nvPicPr><p:blipFill><a:blip r:link="externalImage"/><a:stretch><a:fillRect/></a:stretch></p:blipFill><p:spPr><a:xfrm><a:off x="762000" y="4572000"/><a:ext cx="1524000" cy="1524000"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr></p:pic></p:spTree>')
  }
  return officeZip(parts)
}

export function xlsxFixture(title = '中文预览验收', rows = 8): Buffer {
  const cell = (ref: string, text: string) => `<c r="${ref}" t="inlineStr"><is><t>${esc(text)}</t></is></c>`
  return officeZip({
    '[Content_Types].xml': types([['xl/workbook.xml', 'spreadsheetml.sheet.main'], ['xl/styles.xml', 'spreadsheetml.styles'], ['xl/worksheets/sheet1.xml', 'spreadsheetml.worksheet'], ['xl/worksheets/sheet2.xml', 'spreadsheetml.worksheet']]),
    '_rels/.rels': relationships([['rId1', 'officeDocument', 'xl/workbook.xml']]),
    'xl/workbook.xml': `<workbook xmlns="${sheetNs}" xmlns:r="${officeRel}"><sheets><sheet name="销售概览" sheetId="1" r:id="rId1"/><sheet name="销售明细" sheetId="2" r:id="rId2"/></sheets></workbook>`,
    'xl/_rels/workbook.xml.rels': relationships([['rId1', 'worksheet', 'worksheets/sheet1.xml'], ['rId2', 'worksheet', 'worksheets/sheet2.xml'], ['rId3', 'styles', 'styles.xml']]),
    'xl/styles.xml': `<styleSheet xmlns="${sheetNs}"><fonts count="1"><font><sz val="14"/><name val="${font}"/></font></fonts><fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="4" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/></cellXfs></styleSheet>`,
    'xl/worksheets/sheet1.xml': `<worksheet xmlns="${sheetNs}"><dimension ref="A1:B${rows}"/><sheetViews><sheetView workbookViewId="0"/></sheetViews><sheetFormatPr defaultRowHeight="28"/><cols><col min="1" max="1" width="38" customWidth="1"/><col min="2" max="2" width="22" customWidth="1"/></cols><sheetData><row r="1">${cell('A1', title)}</row><row r="2">${cell('A2', '销售额')}<c r="B2" s="1"><v>1234.5</v></c></row><row r="3">${cell('A3', '含公式的合计')}<c r="B3" s="1"><f>B2*2</f><v>2469</v></c></row>${Array.from({ length: Math.max(0, rows - 3) }, (_, i) => `<row r="${i + 4}">${cell(`A${i + 4}`, `繁體中文与简体 第 ${i + 4} 行`)}<c r="B${i + 4}"><v>${i + 4}</v></c></row>`).join('')}</sheetData></worksheet>`,
    'xl/worksheets/sheet2.xml': `<worksheet xmlns="${sheetNs}"><sheetData><row r="1">${cell('A1', '第二张工作表')}</row></sheetData></worksheet>`,
  })
}

export function pdfFixture(): Buffer {
  const content = 'BT /F1 24 Tf 60 740 Td (Native PDF preview) Tj ET'
  const objects = ['<< /Type /Catalog /Pages 2 0 R >>', '<< /Type /Pages /Kids [3 0 R] /Count 1 >>', '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>', '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>', `<< /Length ${content.length} >>\nstream\n${content}\nendstream`]
  let body = '%PDF-1.4\n'
  const offsets = [0]
  for (const [i, object] of objects.entries()) { offsets.push(Buffer.byteLength(body)); body += `${i + 1} 0 obj\n${object}\nendobj\n` }
  const xref = Buffer.byteLength(body)
  body += `xref\n0 6\n0000000000 65535 f \n${offsets.slice(1).map(offset => `${String(offset).padStart(10, '0')} 00000 n \n`).join('')}trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`
  return Buffer.from(body)
}

export function officeHash(bytes: Buffer): string {
  return createHash('sha256').update(bytes).digest('hex')
}
