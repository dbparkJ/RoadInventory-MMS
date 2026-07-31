import { describe, expect, it } from 'vitest'
import { formatBytes, formatDistance, formatFrameTimestamp, joinPath } from './format'

describe('operator formatting helpers', () => {
  it('formats storage and route sizes for compact panels', () => {
    expect(formatBytes(1_572_864)).toBe('1.5 MB')
    expect(formatDistance(4_820)).toBe('4.8 km')
  })

  it('normalizes browser paths without leaking duplicate separators', () => {
    expect(joinPath('/capture/', 'track-a', '/frames')).toBe('capture/track-a/frames')
  })

  it('preserves non-ISO GPS seconds-of-week timestamps', () => {
    expect(formatFrameTimestamp('GPS_SOW:281430.869000')).toBe(
      'GPS_SOW:281430.869000',
    )
  })
})
