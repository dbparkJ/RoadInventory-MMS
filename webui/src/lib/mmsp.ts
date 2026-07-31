import type { PointCloudPayload } from '../types'

const MAGIC = 0x50534d4d // "MMSP" in little endian
const HEADER_BYTES = 40

/**
 * MMSP v1 layout (little endian):
 * magic[4], version:u16, flags:u16, pointCount:u32,
 * bounds minXYZ/maxXYZ:f32x3, reserved:u32,
 * followed by interleaved relative xyz:f32x3 and optional rgb:u8x3.
 */
export function parseMmsp(buffer: ArrayBuffer): PointCloudPayload {
  if (buffer.byteLength < HEADER_BYTES) {
    throw new Error('포인트 데이터 헤더가 손상되었습니다.')
  }

  const view = new DataView(buffer)
  if (view.getUint32(0, true) !== MAGIC) {
    throw new Error('지원하지 않는 포인트 데이터 형식입니다.')
  }

  const version = view.getUint16(4, true)
  if (version !== 1) throw new Error(`지원하지 않는 MMSP 버전입니다: ${version}`)

  const flags = view.getUint16(6, true)
  const hasColor = (flags & 1) === 1
  const pointCount = view.getUint32(8, true)
  const bounds = {
    min: [view.getFloat32(12, true), view.getFloat32(16, true), view.getFloat32(20, true)] as [
      number,
      number,
      number,
    ],
    max: [view.getFloat32(24, true), view.getFloat32(28, true), view.getFloat32(32, true)] as [
      number,
      number,
      number,
    ],
  }
  const stride = 12 + (hasColor ? 3 : 0)
  const expectedBytes = HEADER_BYTES + pointCount * stride
  if (pointCount > 2_000_000 || expectedBytes > buffer.byteLength) {
    throw new Error('포인트 데이터 크기가 올바르지 않습니다.')
  }

  const positions = new Float32Array(pointCount * 3)
  const colors = hasColor ? new Uint8Array(pointCount * 3) : null
  let offset = HEADER_BYTES

  for (let index = 0; index < pointCount; index += 1) {
    const target = index * 3
    positions[target] = view.getFloat32(offset, true)
    positions[target + 1] = view.getFloat32(offset + 4, true)
    positions[target + 2] = view.getFloat32(offset + 8, true)
    offset += 12
    if (colors) {
      colors[target] = view.getUint8(offset)
      colors[target + 1] = view.getUint8(offset + 1)
      colors[target + 2] = view.getUint8(offset + 2)
      offset += 3
    }
  }

  return { positions, colors, bounds, pointCount }
}
