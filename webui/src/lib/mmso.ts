import type { PanoramaPointPayload } from '../types'

const HEADER_BYTES = 40
const RECORD_BYTES = 15

export function parseMmso(buffer: ArrayBuffer): PanoramaPointPayload {
  if (buffer.byteLength < HEADER_BYTES) throw new Error('파노라마 포인트 헤더가 손상되었습니다.')
  const view = new DataView(buffer)
  const magic = String.fromCharCode(
    view.getUint8(0),
    view.getUint8(1),
    view.getUint8(2),
    view.getUint8(3),
  )
  if (magic !== 'MMSO') throw new Error('지원하지 않는 파노라마 포인트 형식입니다.')
  const version = view.getUint16(4, true)
  if (version !== 1) throw new Error(`지원하지 않는 MMSO 버전입니다. (${version})`)
  const flags = view.getUint16(6, true)
  if ((flags & 2) === 0) throw new Error('파노라마 좌표가 없는 포인트 데이터입니다.')
  const pointCount = view.getUint32(8, true)
  const expectedBytes = HEADER_BYTES + pointCount * RECORD_BYTES
  if (buffer.byteLength !== expectedBytes) throw new Error('파노라마 포인트 본문 길이가 올바르지 않습니다.')

  const coordinates = new Float32Array(pointCount * 3)
  const colors = (flags & 1) !== 0 ? new Uint8Array(pointCount * 3) : null
  let offset = HEADER_BYTES
  for (let index = 0; index < pointCount; index += 1) {
    const coordinateOffset = index * 3
    coordinates[coordinateOffset] = view.getFloat32(offset, true)
    coordinates[coordinateOffset + 1] = view.getFloat32(offset + 4, true)
    coordinates[coordinateOffset + 2] = view.getFloat32(offset + 8, true)
    if (colors) {
      colors[coordinateOffset] = view.getUint8(offset + 12)
      colors[coordinateOffset + 1] = view.getUint8(offset + 13)
      colors[coordinateOffset + 2] = view.getUint8(offset + 14)
    }
    offset += RECORD_BYTES
  }
  return { coordinates, colors, pointCount }
}

export function createDemoPanoramaPoints(count = 3_000): PanoramaPointPayload {
  const coordinates = new Float32Array(count * 3)
  const colors = new Uint8Array(count * 3)
  for (let index = 0; index < count; index += 1) {
    const offset = index * 3
    const column = index % 150
    const row = Math.floor(index / 150)
    coordinates[offset] = 0.32 + column / 420
    coordinates[offset + 1] = 0.55 + row / 145
    coordinates[offset + 2] = 8 + ((index * 17) % 280) / 10
    colors[offset] = 54 + (index % 45)
    colors[offset + 1] = 205 + (index % 45)
    colors[offset + 2] = 176 + (index % 62)
  }
  return { coordinates, colors, pointCount: count }
}
