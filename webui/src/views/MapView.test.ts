import { describe, expect, it } from 'vitest'
import type { Frame } from '../types'
import { frameNavigationTarget } from './MapView'

const FRAME: Frame = {
  id: 'frame-2',
  index: 1,
  track_id: 'track-a',
  timestamp: '2026-01-01T00:00:01Z',
  coordinate: { lon: 127.123, lat: 37.456, altitude: 31 },
  dataset_position: [100, 200, 30],
  heading: 27,
  has_panorama: true,
  has_points: true,
}

describe('MapView frame navigation policy', () => {
  it('does not produce a camera move when selection changes', () => {
    expect(frameNavigationTarget(FRAME, 'selection-change')).toBeNull()
  })

  it('moves only for the explicit current-frame button', () => {
    expect(frameNavigationTarget(FRAME, 'current-frame-button')).toEqual({
      lon: 127.123,
      lat: 37.456,
      height: 280,
      heading: -27,
      tilt: -62,
    })
  })

  it('does not navigate to a frame without a map coordinate', () => {
    expect(
      frameNavigationTarget({ ...FRAME, coordinate: null }, 'current-frame-button'),
    ).toBeNull()
  })
})
