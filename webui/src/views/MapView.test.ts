import { describe, expect, it, vi } from 'vitest'
import type { Frame } from '../types'
import {
  collectionForMapLayer,
  buildSurveyFeatureCollection,
  filterMapTracks,
  firstTargetEntityId,
  frameNavigationTarget,
  isVWorld2DMapMode,
  mapProviderForMode,
  relayMapOverlayShortcut,
  vworld2DBaseMapForMode,
} from './MapView'

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

describe('MapView map modes', () => {
  it('builds independent saved and draft field-survey lines', () => {
    const collection = buildSurveyFeatureCollection(
      [{
        id: 'survey-1',
        dataset_id: 'dataset-1',
        name: '현장조사 필요구간 1',
        color: '#f59e0b',
        geometry: { type: 'LineString', coordinates: [[127, 37], [127.1, 37.1]] },
        created_at: '2026-08-14T00:00:00Z',
        updated_at: '2026-08-14T00:00:00Z',
      }],
      [[128, 38], [128.1, 38.1]],
      '#22d3ee',
    )

    expect(collection.features).toHaveLength(2)
    expect(collection.features.map((feature) => feature.properties?.track_color)).toEqual([
      '#f59e0b',
      '#22d3ee',
    ])
    expect(collection.features[1].properties?.survey_draft).toBe(1)
  })

  it('filters route points and frames to independently visible tracks', () => {
    const visible = new Set(['track-a', 'track-c'])
    const items = [
      { track_id: 'track-a' },
      { track_id: 'track-b' },
      { track_id: 'track-c' },
    ]

    expect(filterMapTracks(items, visible).map((item) => item.track_id)).toEqual([
      'track-a',
      'track-c',
    ])
    expect(filterMapTracks(items, new Set())).toEqual([])
  })

  it('returns an empty collection when an independent map layer is hidden', () => {
    const collection = {
      type: 'FeatureCollection' as const,
      features: [{
        type: 'Feature' as const,
        properties: {},
        geometry: { type: 'Point' as const, coordinates: [127, 37] },
      }],
    }

    expect(collectionForMapLayer(collection, true)).toBe(collection)
    expect(collectionForMapLayer(collection, false)).toEqual({
      type: 'FeatureCollection',
      features: [],
    })
  })

  it('maps 2D and satellite to the shared flat-map engine and distinct official sources', () => {
    expect(isVWorld2DMapMode('2d')).toBe(true)
    expect(isVWorld2DMapMode('satellite')).toBe(true)
    expect(isVWorld2DMapMode('3d')).toBe(false)
    expect(vworld2DBaseMapForMode('2d')).toBe('base')
    expect(vworld2DBaseMapForMode('satellite')).toBe('satellite')
  })

  it('reports the active VWorld provider for diagnostics', () => {
    expect(mapProviderForMode('2d')).toBe('vworld-wmts-base-1.0.0')
    expect(mapProviderForMode('satellite')).toBe('vworld-wmts-satellite-1.0.0')
    expect(mapProviderForMode('3d')).toBe('vworld-webgl-3.0')
  })

  it('walks past non-interactive survey picks to the first mapped 3D entity', () => {
    const frameTargets = new Map([['frame:4', vi.fn()]])
    const overlayTargets = new Map([['overlay:2', vi.fn()]])

    expect(firstTargetEntityId(
      ['route:0', 'route:0:halo', 'frame:4', 'overlay:2'],
      [frameTargets, overlayTargets],
    )).toBe('frame:4')
    expect(firstTargetEntityId(['route:0', 'route:0:halo'], [frameTargets])).toBeNull()
  })
})

describe('MapView iframe shortcut relay', () => {
  it.each([
    ['a', 'KeyA', -1],
    ['ArrowLeft', 'ArrowLeft', -1],
    ['d', 'KeyD', 1],
    ['ArrowRight', 'ArrowRight', 1],
  ] as const)(
    'relays %s frame navigation after a map point-pick leaves focus inside the iframe',
    (key, code, expectedDirection) => {
      const directions: number[] = []
      const ownerWindow = {
        document,
        dispatchEvent: vi.fn((relayedEvent: Event) => {
          const keyboardEvent = relayedEvent as KeyboardEvent
          directions.push(
            keyboardEvent.code === 'KeyA' || keyboardEvent.key === 'ArrowLeft' ? -1 : 1,
          )
          relayedEvent.preventDefault()
          return false
        }),
      } as unknown as Window
      const iframeEvent = new KeyboardEvent('keydown', {
        key,
        code,
        cancelable: true,
      })

      expect(relayMapOverlayShortcut(iframeEvent, ownerWindow)).toBe(true)
      expect(iframeEvent.defaultPrevented).toBe(true)
      expect(directions).toEqual([expectedDirection])
    },
  )

  it('relays N once to the map owner window and preserves repeat state', () => {
    const dispatchEvent = vi.fn((relayedEvent: Event) => {
      relayedEvent.preventDefault()
      return false
    })
    const ownerWindow = {
      document,
      dispatchEvent,
    } as unknown as Window
    const event = new KeyboardEvent('keydown', {
      key: 'n',
      code: 'KeyN',
      repeat: true,
      cancelable: true,
    })

    expect(relayMapOverlayShortcut(event, ownerWindow)).toBe(true)
    expect(event.defaultPrevented).toBe(true)
    expect(dispatchEvent).toHaveBeenCalledOnce()
    expect(dispatchEvent.mock.calls[0][0]).toMatchObject({ code: 'KeyN', repeat: true })
  })

  it('relays P from the iframe after a feature mutation leaves map focus active', () => {
    const dispatchEvent = vi.fn((relayedEvent: Event) => {
      relayedEvent.preventDefault()
      return false
    })
    const ownerWindow = { document, dispatchEvent } as unknown as Window
    const event = new KeyboardEvent('keydown', {
      key: 'p',
      code: 'KeyP',
      cancelable: true,
    })

    expect(relayMapOverlayShortcut(event, ownerWindow)).toBe(true)
    expect(event.defaultPrevented).toBe(true)
    expect(dispatchEvent.mock.calls[0][0]).toMatchObject({ code: 'KeyP' })
  })

  it('does not relay modified or editable-target N keys', () => {
    const dispatchEvent = vi.fn((_event: Event) => true)
    const ownerWindow = {
      document,
      dispatchEvent,
    } as unknown as Window
    const input = document.createElement('input')
    const inputEvent = {
      key: 'n',
      code: 'KeyN',
      target: input,
      defaultPrevented: false,
      altKey: false,
      ctrlKey: false,
      metaKey: false,
      shiftKey: false,
      repeat: false,
      preventDefault: vi.fn(),
    } as unknown as KeyboardEvent

    expect(relayMapOverlayShortcut(inputEvent, ownerWindow)).toBe(false)
    expect(
      relayMapOverlayShortcut(
        new KeyboardEvent('keydown', { key: 'n', code: 'KeyN', ctrlKey: true }),
        ownerWindow,
      ),
    ).toBe(false)
    expect(dispatchEvent).not.toHaveBeenCalled()
  })

  it('does not consume an unhandled Escape from the iframe', () => {
    const dispatchEvent = vi.fn((_event: Event) => true)
    const ownerWindow = { document, dispatchEvent } as unknown as Window
    const event = new KeyboardEvent('keydown', {
      key: 'Escape',
      code: 'Escape',
      cancelable: true,
    })

    expect(relayMapOverlayShortcut(event, ownerWindow)).toBe(false)
    expect(event.defaultPrevented).toBe(false)
    expect(dispatchEvent).toHaveBeenCalledOnce()
  })
})
