import { describe, expect, it } from 'vitest'
import {
  buildRouteFeatureCollection,
  buildRouteRangeFeatureCollection,
  buildTrackColorMap,
  TRACK_COLORS,
} from './route'

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
    expect(collection.features[0]?.properties).toMatchObject({
      track_index: 0,
      track_color: TRACK_COLORS[0],
    })
    expect(collection.features[0]?.geometry.coordinates).toEqual([
      [127, 37],
      [127.1, 37.1],
    ])
    expect(collection.features[1]?.geometry.coordinates).toEqual([
      [128, 38],
      [128.1, 38.1],
    ])
    expect(collection.features[1]?.properties).toMatchObject({
      track_index: 1,
      track_color: TRACK_COLORS[1],
    })
  })

  it('does not draw a line for a single isolated point', () => {
    expect(
      buildRouteFeatureCollection([{ lon: 127, lat: 37, track_id: 'only' }]).features,
    ).toEqual([])
  })

  it('marks only the active track as selected', () => {
    const collection = buildRouteFeatureCollection(
      [
        { lon: 127, lat: 37, track_id: 'track-a' },
        { lon: 127.1, lat: 37.1, track_id: 'track-a' },
        { lon: 128, lat: 38, track_id: 'track-b' },
        { lon: 128.1, lat: 38.1, track_id: 'track-b' },
      ],
      undefined,
      'track-b',
    )

    expect(collection.features.map((feature) => feature.properties.selected)).toEqual([0, 1])
  })

  it('does not shift a later track color when an earlier track has one point', () => {
    const collection = buildRouteFeatureCollection([
      { lon: 127, lat: 37, track_id: 'single' },
      { lon: 128, lat: 38, track_id: 'drawn' },
      { lon: 128.1, lat: 38.1, track_id: 'drawn' },
    ])

    expect(collection.features[0]?.properties).toMatchObject({
      track_id: 'drawn',
      track_index: 1,
      track_color: TRACK_COLORS[1],
    })
  })

  it('assigns at least ten tracks distinct colors in first-seen order', () => {
    const route = Array.from({ length: 10 }, (_, index) => ({
      lon: 127 + index / 100,
      lat: 37,
      track_id: `track-${index}`,
    }))
    route.push({ lon: 128, lat: 38, track_id: 'track-0' })

    const colors = buildTrackColorMap(route)
    const assigned = [...colors.values()]

    expect(assigned).toEqual(TRACK_COLORS.slice(0, 10))
    expect(new Set(assigned)).toHaveLength(10)
  })

  it('cycles only after exhausting the full track palette', () => {
    const colors = buildTrackColorMap(
      Array.from({ length: TRACK_COLORS.length + 1 }, (_, index) => ({
        lon: 127,
        lat: 37,
        track_id: `track-${index}`,
      })),
    )

    expect(colors.get(`track-${TRACK_COLORS.length}`)).toBe(TRACK_COLORS[0])
  })

  it('keeps colors stable when hidden tracks are filtered out', () => {
    const route = [
      { lon: 127, lat: 37, track_id: 'sec-2' },
      { lon: 127.1, lat: 37.1, track_id: 'sec-2' },
    ]
    const colors = buildTrackColorMap(route, ['sec-1', 'sec-2', 'sec-5'])
    const collection = buildRouteFeatureCollection(route, colors)

    expect(colors.get('sec-2')).toBe(TRACK_COLORS[1])
    expect(collection.features[0]?.properties.track_color).toBe(TRACK_COLORS[1])
  })

  it('builds separate contiguous route segments inside the execution range', () => {
    const route = [
      { lon: 127, lat: 37, track_id: 'a', frame_id: 'a-0' },
      { lon: 127.1, lat: 37.1, track_id: 'a', frame_id: 'a-1' },
      { lon: 127.2, lat: 37.2, track_id: 'a', frame_id: 'a-2' },
      { lon: 127.3, lat: 37.3, track_id: 'a', frame_id: 'a-3' },
      { lon: 127.4, lat: 37.4, track_id: 'a', frame_id: 'a-4' },
    ]
    const indexes = new Map([
      ['a-0', 0],
      ['a-1', 1],
      ['a-2', 2],
      ['a-3', 3],
      ['a-4', 4],
    ])

    const collection = buildRouteRangeFeatureCollection(route, indexes, [1, 3])

    expect(collection.features).toHaveLength(1)
    expect(collection.features[0]?.properties.track_color).toBe(TRACK_COLORS[0])
    expect(collection.features[0]?.geometry.coordinates).toEqual([
      [127.1, 37.1],
      [127.2, 37.2],
      [127.3, 37.3],
    ])
  })

  it('omits range lines when the selection has fewer than two sampled points', () => {
    const collection = buildRouteRangeFeatureCollection(
      [
        { lon: 127, lat: 37, track_id: 'a', frame_id: 'a-0' },
        { lon: 127.1, lat: 37.1, track_id: 'a', frame_id: 'a-1' },
      ],
      new Map([
        ['a-0', 0],
        ['a-1', 1],
      ]),
      [1, 1],
    )

    expect(collection.features).toEqual([])
  })

  it('uses route ordinals to highlight a range even when those frames are not loaded in the UI', () => {
    const collection = buildRouteRangeFeatureCollection(
      [
        { lon: 127, lat: 37, track_id: 'a', frame_id: 'a-800', index: 800 },
        { lon: 127.1, lat: 37.1, track_id: 'a', frame_id: 'a-801', index: 801 },
        { lon: 127.2, lat: 37.2, track_id: 'a', frame_id: 'a-802', index: 802 },
      ],
      new Map(),
      [800, 801],
    )

    expect(collection.features[0]?.geometry.coordinates).toEqual([
      [127, 37],
      [127.1, 37.1],
    ])
  })
})
