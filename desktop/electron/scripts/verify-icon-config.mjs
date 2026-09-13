import { existsSync } from 'node:fs'
import { readFile } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { inflateSync } from 'node:zlib'

const scriptDir = dirname(fileURLToPath(import.meta.url))
const packageRoot = resolve(scriptDir, '..')
const packageJsonPath = join(packageRoot, 'package.json')
const macIconPath = join(packageRoot, 'assets', 'icon.icns')
const windowsIconPath = join(packageRoot, 'assets', 'icon.ico')

const failures = []

function fail(message) {
  failures.push(message)
}

function expectEqual(actual, expected, label) {
  if (actual !== expected) {
    fail(`${label} must be ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`)
  }
}

function parseIcoEntries(buffer) {
  if (buffer.length < 6 || buffer.readUInt16LE(0) !== 0 || buffer.readUInt16LE(2) !== 1) {
    throw new Error('invalid ICO header')
  }
  const count = buffer.readUInt16LE(4)
  if (buffer.length < 6 + count * 16) throw new Error('truncated ICO directory')
  return Array.from({ length: count }, (_, index) => {
    const offset = 6 + index * 16
    const width = buffer[offset] || 256
    const height = buffer[offset + 1] || 256
    const byteLength = buffer.readUInt32LE(offset + 8)
    const imageOffset = buffer.readUInt32LE(offset + 12)
    if (imageOffset + byteLength > buffer.length) throw new Error('truncated ICO image')
    return {
      width,
      height,
      image: buffer.subarray(imageOffset, imageOffset + byteLength),
    }
  })
}

function paeth(left, up, upperLeft) {
  const estimate = left + up - upperLeft
  const leftDistance = Math.abs(estimate - left)
  const upDistance = Math.abs(estimate - up)
  const upperLeftDistance = Math.abs(estimate - upperLeft)
  if (leftDistance <= upDistance && leftDistance <= upperLeftDistance) return left
  if (upDistance <= upperLeftDistance) return up
  return upperLeft
}

function decodeRgbaPng(buffer) {
  const pngSignature = '89504e470d0a1a0a'
  if (buffer.subarray(0, 8).toString('hex') !== pngSignature) {
    throw new Error('ICO taskbar representation must be a PNG')
  }
  let width = 0
  let height = 0
  let bitDepth = 0
  let colorType = 0
  let interlace = 0
  const compressed = []
  for (let offset = 8; offset + 12 <= buffer.length;) {
    const length = buffer.readUInt32BE(offset)
    const type = buffer.subarray(offset + 4, offset + 8).toString('ascii')
    const dataStart = offset + 8
    const dataEnd = dataStart + length
    if (dataEnd + 4 > buffer.length) throw new Error('truncated PNG chunk')
    if (type === 'IHDR') {
      width = buffer.readUInt32BE(dataStart)
      height = buffer.readUInt32BE(dataStart + 4)
      bitDepth = buffer[dataStart + 8]
      colorType = buffer[dataStart + 9]
      interlace = buffer[dataStart + 12]
    } else if (type === 'IDAT') {
      compressed.push(buffer.subarray(dataStart, dataEnd))
    } else if (type === 'IEND') {
      break
    }
    offset = dataEnd + 4
  }
  if (bitDepth !== 8 || colorType !== 6 || interlace !== 0) {
    throw new Error('ICO taskbar PNG must use non-interlaced 8-bit RGBA pixels')
  }
  const bytesPerPixel = 4
  const stride = width * bytesPerPixel
  const packed = inflateSync(Buffer.concat(compressed))
  if (packed.length !== (stride + 1) * height) throw new Error('unexpected PNG scanline size')
  const pixels = Buffer.alloc(stride * height)
  let inputOffset = 0
  for (let y = 0; y < height; y += 1) {
    const filter = packed[inputOffset]
    inputOffset += 1
    for (let x = 0; x < stride; x += 1) {
      const raw = packed[inputOffset + x]
      const outputOffset = y * stride + x
      const left = x >= bytesPerPixel ? pixels[outputOffset - bytesPerPixel] : 0
      const up = y > 0 ? pixels[outputOffset - stride] : 0
      const upperLeft = y > 0 && x >= bytesPerPixel
        ? pixels[outputOffset - stride - bytesPerPixel]
        : 0
      let value
      if (filter === 0) value = raw
      else if (filter === 1) value = raw + left
      else if (filter === 2) value = raw + up
      else if (filter === 3) value = raw + Math.floor((left + up) / 2)
      else if (filter === 4) value = raw + paeth(left, up, upperLeft)
      else throw new Error(`unsupported PNG filter ${filter}`)
      pixels[outputOffset] = value & 0xff
    }
    inputOffset += stride
  }
  return { width, height, pixels }
}

function pixelBounds(image, predicate) {
  let minX = image.width
  let minY = image.height
  let maxX = -1
  let maxY = -1
  for (let y = 0; y < image.height; y += 1) {
    for (let x = 0; x < image.width; x += 1) {
      const offset = (y * image.width + x) * 4
      if (!predicate(
        image.pixels[offset],
        image.pixels[offset + 1],
        image.pixels[offset + 2],
        image.pixels[offset + 3],
      )) continue
      minX = Math.min(minX, x)
      minY = Math.min(minY, y)
      maxX = Math.max(maxX, x)
      maxY = Math.max(maxY, y)
    }
  }
  if (maxX < minX || maxY < minY) return null
  return `${maxX - minX + 1}x${maxY - minY + 1}`
}

function isColoredPixel(red, green, blue, alpha) {
  const maximum = Math.max(red, green, blue)
  const minimum = Math.min(red, green, blue)
  return alpha > 16 && maximum - minimum > 24 && (maximum - minimum) * 4 > maximum
}

const pkg = JSON.parse(await readFile(packageJsonPath, 'utf8'))
const build = pkg.build ?? {}

if (!existsSync(macIconPath)) {
  fail(`macOS icon is missing at ${macIconPath}`)
}

if (!existsSync(windowsIconPath)) {
  fail(`Windows icon is missing at ${windowsIconPath}`)
} else {
  try {
    const entries = parseIcoEntries(await readFile(windowsIconPath))
    expectEqual(
      entries.map(({ width, height }) => `${width}x${height}`).join(','),
      '16x16,24x24,32x32,48x48,64x64,128x128,256x256',
      'Windows ICO representations',
    )
    const compactEntry = entries.find(({ width, height }) => width === 16 && height === 16)
    if (!compactEntry) throw new Error('missing 16x16 compact representation')
    const compactImage = decodeRgbaPng(compactEntry.image)
    expectEqual(
      pixelBounds(compactImage, isColoredPixel),
      '10x10',
      'Windows compact orange mark bounds',
    )
    const taskbarEntry = entries.find(({ width, height }) => width === 24 && height === 24)
    if (!taskbarEntry) throw new Error('missing 24x24 taskbar representation')
    const taskbarImage = decodeRgbaPng(taskbarEntry.image)
    expectEqual(
      pixelBounds(taskbarImage, (_red, _green, _blue, alpha) => alpha > 16),
      '22x22',
      'Windows taskbar icon board bounds',
    )
    expectEqual(
      pixelBounds(taskbarImage, isColoredPixel),
      '15x15',
      'Windows taskbar orange mark bounds',
    )
  } catch (error) {
    fail(`Windows icon could not be inspected: ${error instanceof Error ? error.message : error}`)
  }
}

expectEqual(build.mac?.icon, 'assets/icon.icns', 'build.mac.icon')
expectEqual(build.dmg?.icon, 'assets/icon.icns', 'build.dmg.icon')
expectEqual(build.win?.icon, 'assets/icon.ico', 'build.win.icon')
expectEqual(build.nsis?.installerIcon, 'assets/icon.ico', 'build.nsis.installerIcon')
expectEqual(build.nsis?.uninstallerIcon, 'assets/icon.ico', 'build.nsis.uninstallerIcon')

if (failures.length > 0) {
  console.error('OpenSquilla desktop icon verification failed:')
  for (const failure of failures) console.error(`- ${failure}`)
  process.exit(1)
}

console.log('OpenSquilla desktop icon verification passed.')
