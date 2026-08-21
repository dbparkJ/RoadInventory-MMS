import { describe, expect, it } from 'vitest'
import type { Frame } from '../types'
import {
  coordinateBearing,
  directionalPanoramaTarget,
  panoramaViewRelativeBearing,
  panoramaViewWorldBearing,
} from './panoramaNavigation'

function frame(id: string, index: number, lat: number, lon: number): Frame {
  return {
    id,
    index,
    track_id: 'track-a',
    timestamp: `2026-01-01T00:00:${String(index).padStart(2, '0')}Z`,
    coordinate: { lat, lon },
    heading: 0,
    has_panorama: true,
    has_points: true,
  }
}

describe('panorama direction navigation', () => {
  const previous = frame('previous', 9, 36.999, 127)
  const current = frame('current', 10, 37, 127)
  const next = frame('next', 11, 37.001, 127)

  it('maps the image-space view to a clockwise world bearing', () => {
    expect(panoramaViewRelativeBearing(-180, -180)).toBe(0)
    expect(panoramaViewRelativeBearing(-90, -180)).toBe(90)
    expect(panoramaViewWorldBearing(350, -160, -180)).toBe(10)
  })

  it('calculates WGS84 initial bearings', () => {
    expect(coordinateBearing(current, next)).toBeCloseTo(0, 3)
    expect(coordinateBearing(current, previous)).toBeCloseTo(180, 3)
  })

  it('moves toward the adjacent frame nearest the current view', () => {
    expect(directionalPanoramaTarget(current, [previous, current, next], -180, -180)?.frame.id)
      .toBe('next')
    expect(directionalPanoramaTarget(current, [previous, current, next], 0, -180)?.frame.id)
      .toBe('previous')
  })

  it('uses adjacent coordinate bearings when heading is null at runtime', () => {
    const missingHeading: Frame = { ...current, heading: null }
    expect(
      directionalPanoramaTarget(missingHeading, [previous, missingHeading, next], -180, -180)
        ?.frame.id,
    ).toBe('next')
    expect(
      directionalPanoramaTarget(missingHeading, [previous, missingHeading, next], 0, -180)
        ?.frame.id,
    ).toBe('previous')
  })

  it('never jumps to a distant or different-track frame', () => {
    const distant = frame('distant', 50, 37.01, 127)
    const otherTrack = { ...frame('other', 11, 37.0005, 127), track_id: 'track-b' }
    expect(
      directionalPanoramaTarget(current, [previous, current, next, distant, otherTrack], -180, -180)
        ?.frame.id,
    ).toBe('next')
  })

  it('does not send a forward-facing operator to the only loaded frame behind them', () => {
    expect(directionalPanoramaTarget(current, [previous, current], -180, -180)).toBeNull()
  })

  it('requires a coordinate-backed candidate within the approximately 100 degree total cone', () => {
    const coordinateLess = { ...next, coordinate: null }
    expect(directionalPanoramaTarget(current, [current, coordinateLess], -180, -180)).toBeNull()
    expect(directionalPanoramaTarget(current, [current, next], -130, -180)?.frame.id).toBe('next')
    expect(directionalPanoramaTarget(current, [current, next], -129, -180)).toBeNull()
    expect(directionalPanoramaTarget(current, [current, next], 0, -180)).toBeNull()
  })
})
