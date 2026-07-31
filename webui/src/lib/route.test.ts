import { describe, expect, it } from 'vitest'
import { buildRouteFeatureCollection } from './route'

describe('buildRouteFeatureCollection', () => {
  it('keeps tracks separate while preserving point order', () => {
    const collection = buildRouteFeatureCollection([
      { lon: 127, lat: 37, track_id: 'track-a' },
      { lon: 128, lat: 38, track_id: 'track-b' },
      { lon: 127.1, lat: 37.1, track_id: 'track-a' },
      { lon: 128.1, lat: 38.1, track_id: 'track-b' },
    ])

    expect(collection.features).toHaveLength(2)
    expect(collection.features[0]?.properties?.track_id).toBe('track-a')
    expect(collection.features[0]?.geometry.coordinates).toEqual([
      [127, 37],
      [127.1, 37.1],
    ])
    expect(collection.features[1]?.geometry.coordinates).toEqual([
      [128, 38],
      [128.1, 38.1],
    ])
  })

  it('does not draw a line for a single isolated point', () => {
    expect(
      buildRouteFeatureCollection([{ lon: 127, lat: 37, track_id: 'only' }]).features,
    ).toEqual([])
  })
})
