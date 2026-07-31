import { describe, expect, it } from 'vitest'
import { parseMmsp } from './mmsp'

function makeMmsp(): ArrayBuffer {
  const pointCount = 2
  const buffer = new ArrayBuffer(40 + pointCount * 15)
  const view = new DataView(buffer)
  view.setUint32(0, 0x50534d4d, true)
  view.setUint16(4, 1, true)
  view.setUint16(6, 1, true)
  view.setUint32(8, pointCount, true)
  ;[-1, -2, -3].forEach((value, index) => view.setFloat32(12 + index * 4, value, true))
  ;[4, 5, 6].forEach((value, index) => view.setFloat32(24 + index * 4, value, true))
  view.setUint32(36, 0, true)

  let offset = 40
  const records = [
    { position: [1.25, 2.5, 3.75], color: [12, 34, 56] },
    { position: [-4, 0.5, 6], color: [200, 210, 220] },
  ]
  records.forEach((record) => {
    record.position.forEach((value, axis) => view.setFloat32(offset + axis * 4, value, true))
    offset += 12
    record.color.forEach((value, channel) => view.setUint8(offset + channel, value))
    offset += 3
  })
  return buffer
}

describe('parseMmsp', () => {
  it('parses the compact v1 point payload without copying server bounds incorrectly', () => {
    const parsed = parseMmsp(makeMmsp())
    expect(parsed.pointCount).toBe(2)
    expect([...parsed.positions]).toEqual([1.25, 2.5, 3.75, -4, 0.5, 6])
    expect([...(parsed.colors ?? [])]).toEqual([12, 34, 56, 200, 210, 220])
    expect(parsed.bounds).toEqual({ min: [-1, -2, -3], max: [4, 5, 6] })
  })

  it('rejects malformed and unsupported input early', () => {
    expect(() => parseMmsp(new ArrayBuffer(8))).toThrow('헤더가 손상')
    const payload = makeMmsp()
    new DataView(payload).setUint16(4, 9, true)
    expect(() => parseMmsp(payload)).toThrow('지원하지 않는 MMSP 버전')
  })
})
