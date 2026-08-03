import { describe, expect, it } from 'vitest'
import { parseMmso } from './mmso'

function payload() {
  const buffer = new ArrayBuffer(55)
  const bytes = new Uint8Array(buffer)
  bytes.set([77, 77, 83, 79])
  const view = new DataView(buffer)
  view.setUint16(4, 1, true)
  view.setUint16(6, 3, true)
  view.setUint32(8, 1, true)
  view.setFloat32(40, 0.5, true)
  view.setFloat32(44, 0.25, true)
  view.setFloat32(48, 12.5, true)
  bytes.set([10, 20, 30], 52)
  return buffer
}

describe('parseMmso', () => {
  it('parses normalized panorama coordinates and colors', () => {
    const parsed = parseMmso(payload())
    expect(parsed.pointCount).toBe(1)
    expect([...parsed.coordinates]).toEqual([0.5, 0.25, 12.5])
    expect([...(parsed.colors ?? [])]).toEqual([10, 20, 30])
  })

  it('rejects truncated records', () => {
    expect(() => parseMmso(payload().slice(0, 54))).toThrow('본문 길이')
  })
})
