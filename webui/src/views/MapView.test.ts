import { cleanup, fireEvent, render, waitFor } from '@testing-library/react'
import { createElement } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Frame, RoutePoint } from '../types'

const vworldLifecycleMocks = vi.hoisted(() => ({
  createDataSource: vi.fn(),
  removeDataSource: vi.fn(),
  renderFrames: vi.fn(),
  renderOverlay: vi.fn(),
  renderRoute: vi.fn(),
  renderRouteRange: vi.fn(),
  resizeMap: vi.fn(),
  setSceneMode: vi.fn(),
  startMap: vi.fn(),
}))

const vworld2DLifecycleMocks = vi.hoisted(() => ({
  createDataSource: vi.fn(),
  destroyMap: vi.fn(),
  fitMap: vi.fn(),
  removeDataSource: vi.fn(),
  renderCollection: vi.fn(),
  setBaseMap: vi.fn(),
  startMap: vi.fn(),
}))

vi.mock('../lib/vworld', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/vworld')>()
  return {
    ...actual,
    createVWorldDataSource: vworldLifecycleMocks.createDataSource,
    removeVWorldDataSource: vworldLifecycleMocks.removeDataSource,
    renderVWorldFrames: vworldLifecycleMocks.renderFrames,
    renderVWorldOverlay: vworldLifecycleMocks.renderOverlay,
    renderVWorldRoute: vworldLifecycleMocks.renderRoute,
    renderVWorldRouteRange: vworldLifecycleMocks.renderRouteRange,
    resizeVWorldMap: vworldLifecycleMocks.resizeMap,
    setVWorldSceneMode: vworldLifecycleMocks.setSceneMode,
    startVWorldMap: vworldLifecycleMocks.startMap,
  }
})

vi.mock('../lib/vworld2d', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/vworld2d')>()
  return {
    ...actual,
    createVWorld2DDataSource: vworld2DLifecycleMocks.createDataSource,
    destroyVWorld2DMap: vworld2DLifecycleMocks.destroyMap,
    fitVWorld2DMap: vworld2DLifecycleMocks.fitMap,
    removeVWorld2DDataSource: vworld2DLifecycleMocks.removeDataSource,
    renderVWorld2DCollection: vworld2DLifecycleMocks.renderCollection,
    setVWorld2DBaseMap: vworld2DLifecycleMocks.setBaseMap,
    startVWorld2DMap: vworld2DLifecycleMocks.startMap,
  }
})
import {
  collectionForMapLayer,
  buildSurveyFeatureCollection,
  filterMapTracks,
  firstTargetEntityId,
  frameNavigationTarget,
  isVWorld2DMapMode,
  MapView,
  mapProviderForMode,
  relayMapOverlayShortcut,
  vworld2DBaseMapForMode,
  vworld2DPointerWgs84Coordinate,
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

const ROUTE: RoutePoint[] = [
  { frame_id: 'frame-1', track_id: 'track-a', index: 0, lon: 127.1, lat: 37.4 },
  { frame_id: 'frame-2', track_id: 'track-a', index: 1, lon: 127.2, lat: 37.5 },
]

function mapViewProps(mapMode: '2d' | 'satellite' | '3d') {
  return {
    route: ROUTE,
    frames: [FRAME],
    selectedFrame: FRAME,
    activeTrackId: 'track-a',
    loading: false,
    mapMode,
    onSelectFrame: vi.fn(),
  }
}

function installIframeContainer(
  container: HTMLElement,
  containerId: string,
): HTMLIFrameElement {
  const iframe = container.querySelector('iframe')
  if (!(iframe instanceof HTMLIFrameElement)) throw new Error('Map iframe was not rendered.')
  const frameWindow = iframe.contentWindow
  const frameDocument = iframe.contentDocument
  if (!frameWindow || !frameDocument) throw new Error('Map iframe document is unavailable.')

  const mapContainer = frameDocument.createElement('div')
  mapContainer.id = containerId
  const documentElement = frameDocument.documentElement
    ?? frameDocument.appendChild(frameDocument.createElement('html'))
  const body = frameDocument.body ?? documentElement.appendChild(frameDocument.createElement('body'))
  body.replaceChildren(mapContainer)
  Object.defineProperty(frameWindow, 'requestAnimationFrame', {
    value: vi.fn(() => 1),
    configurable: true,
  })
  Object.defineProperty(frameWindow, 'cancelAnimationFrame', {
    value: vi.fn(),
    configurable: true,
  })
  fireEvent.load(iframe)
  return iframe
}

beforeEach(() => {
  vi.clearAllMocks()

  const canvas = document.createElement('canvas')
  const runtime3D = {
    map: {
      clear: vi.fn(),
      onClick: {
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      },
    },
    viewer: { scene: { canvas } },
  }
  const runtime2D = {
    map: {
      on: vi.fn(),
      un: vi.fn(),
      updateSize: vi.fn(),
    },
    ol: { proj: { toLonLat: vi.fn() } },
  }

  vworldLifecycleMocks.startMap.mockResolvedValue(runtime3D)
  vworldLifecycleMocks.createDataSource.mockImplementation(
    async (_runtime: unknown, name: string) => ({ name }),
  )
  vworldLifecycleMocks.renderFrames.mockReturnValue(new Map())
  vworldLifecycleMocks.renderOverlay.mockReturnValue(new Map())
  vworldLifecycleMocks.renderRoute.mockReturnValue(new Map())
  vworld2DLifecycleMocks.startMap.mockResolvedValue(runtime2D)
  vworld2DLifecycleMocks.createDataSource.mockImplementation(() => ({}))
})

afterEach(() => cleanup())

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
  it('keeps the mode switch on the map and reports map control clicks', () => {
    const onMapModeChange = vi.fn()
    const view = render(createElement(MapView, {
      ...mapViewProps('2d'),
      onMapModeChange,
    }))

    const modeSwitch = view.getByRole('group', { name: '지도 모드 선택' })
    expect(modeSwitch.closest('.map-tools')).not.toBeNull()
    expect(view.getByRole('button', { name: '2D' })).toHaveAttribute('aria-pressed', 'true')

    fireEvent.click(view.getByRole('button', { name: '위성지도' }))
    fireEvent.click(view.getByRole('button', { name: '3D' }))
    expect(onMapModeChange.mock.calls).toEqual([['satellite'], ['3d']])
  })

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
      [128.2, 38.2],
    )

    expect(collection.features).toHaveLength(2)
    expect(collection.features.map((feature) => feature.properties?.track_color)).toEqual([
      '#f59e0b',
      '#22d3ee',
    ])
    expect(collection.features[1].properties?.survey_draft).toBe(1)
    expect(collection.features[1].geometry).toEqual({
      type: 'LineString',
      coordinates: [[128, 38], [128.1, 38.1], [128.2, 38.2]],
    })
  })

  it('shows a ghost line after the first survey point and removes it on pointer leave', () => {
    const preview = buildSurveyFeatureCollection([], [[127, 37]], '#f59e0b', [127.1, 37.1])
    const cleared = buildSurveyFeatureCollection([], [[127, 37]], '#f59e0b', null)

    expect(preview.features[0]?.geometry).toEqual({
      type: 'LineString',
      coordinates: [[127, 37], [127.1, 37.1]],
    })
    expect(cleared.features).toEqual([])
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

  it('converts a 2D pointer coordinate to WGS84 for the survey ghost line', () => {
    const runtime = {
      ol: { proj: { toLonLat: vi.fn(() => [127.25, 37.5]) } },
    } as never

    expect(vworld2DPointerWgs84Coordinate(runtime, [14_000_000, 4_500_000])).toEqual([
      127.25,
      37.5,
    ])
    expect(vworld2DPointerWgs84Coordinate(runtime, undefined)).toBeNull()
  })

  it('reports the active VWorld provider for diagnostics', () => {
    expect(mapProviderForMode('2d')).toBe('vworld-wmts-base-1.0.0')
    expect(mapProviderForMode('satellite')).toBe('vworld-wmts-satellite-1.0.0')
    expect(mapProviderForMode('3d')).toBe('vworld-webgl-3.0')
  })

  it('uses a mapped route as a 3D track target and skips only unmapped guide lines', () => {
    const frameTargets = new Map([['frame:4', vi.fn()]])
    const overlayTargets = new Map([['overlay:2', vi.fn()]])
    const routeTargets = new Map([
      ['route:0', vi.fn()],
      ['route:0:halo', vi.fn()],
    ])

    expect(firstTargetEntityId(
      ['route:0', 'route:0:halo', 'frame:4', 'overlay:2'],
      [frameTargets, overlayTargets, routeTargets],
    )).toBe('route:0')
    expect(firstTargetEntityId(
      ['survey:0', 'frame:4', 'overlay:2'],
      [frameTargets, overlayTargets, routeTargets],
    )).toBe('frame:4')
    expect(firstTargetEntityId(['route:0', 'route:0:halo'], [frameTargets])).toBeNull()
  })
})

describe('MapView controlled mode lifecycle', () => {
  it('forwards a rendered 3D route target to the work-track selector', async () => {
    const onSelectTrack = vi.fn()
    const rendered = render(createElement(MapView, {
      ...mapViewProps('3d'),
      onSelectTrack,
    }))
    installIframeContainer(rendered.container, 'vmap')

    await waitFor(() => expect(vworldLifecycleMocks.renderRoute).toHaveBeenCalled())
    const routeCall = vworldLifecycleMocks.renderRoute.mock.calls.find(
      (call) => typeof call[3] === 'function',
    )
    const onTrack = routeCall?.[3] as
      | ((trackId: string) => void)
      | undefined
    expect(onTrack).toEqual(expect.any(Function))

    onTrack?.('track-b')
    expect(onSelectTrack).toHaveBeenCalledWith('track-b')
  })

  it('switches 2D and satellite base maps in place and refits the visible route', async () => {
    const rendered = render(createElement(MapView, mapViewProps('2d')))
    const iframe2D = installIframeContainer(rendered.container, 'vmap')

    await waitFor(() => {
      expect(vworld2DLifecycleMocks.startMap).toHaveBeenCalledOnce()
      expect(vworld2DLifecycleMocks.fitMap).toHaveBeenCalledOnce()
      expect(vworld2DLifecycleMocks.setBaseMap).toHaveBeenLastCalledWith(
        expect.anything(),
        'base',
      )
    })
    expect(vworld2DLifecycleMocks.fitMap).toHaveBeenLastCalledWith(
      expect.anything(),
      [[127.1, 37.4], [127.2, 37.5]],
    )

    rendered.rerender(createElement(MapView, mapViewProps('satellite')))

    await waitFor(() => {
      expect(vworld2DLifecycleMocks.setBaseMap).toHaveBeenLastCalledWith(
        expect.anything(),
        'satellite',
      )
      expect(vworld2DLifecycleMocks.fitMap).toHaveBeenCalledTimes(2)
    })
    expect(rendered.container.querySelector('iframe')).toBe(iframe2D)
    expect(vworld2DLifecycleMocks.startMap).toHaveBeenCalledOnce()
    expect(vworld2DLifecycleMocks.destroyMap).not.toHaveBeenCalled()
    expect(vworldLifecycleMocks.startMap).not.toHaveBeenCalled()

    rendered.rerender(createElement(MapView, mapViewProps('2d')))

    await waitFor(() => {
      expect(vworld2DLifecycleMocks.setBaseMap).toHaveBeenLastCalledWith(
        expect.anything(),
        'base',
      )
      expect(vworld2DLifecycleMocks.fitMap).toHaveBeenCalledTimes(3)
    })
    expect(rendered.container.querySelector('iframe')).toBe(iframe2D)
    expect(vworld2DLifecycleMocks.startMap).toHaveBeenCalledOnce()
  })

  it('selects the matching engine and refits the route across a 2D to 3D round trip', async () => {
    const rendered = render(createElement(MapView, mapViewProps('2d')))
    const initial2DIframe = installIframeContainer(rendered.container, 'vmap')

    await waitFor(() => {
      expect(vworld2DLifecycleMocks.startMap).toHaveBeenCalledOnce()
      expect(vworld2DLifecycleMocks.fitMap).toHaveBeenCalledOnce()
    })

    rendered.rerender(createElement(MapView, mapViewProps('3d')))

    await waitFor(() => {
      expect(vworld2DLifecycleMocks.destroyMap).toHaveBeenCalledOnce()
    })
    const iframe3D = installIframeContainer(rendered.container, 'vmap')
    expect(iframe3D).not.toBe(initial2DIframe)

    await waitFor(() => {
      expect(vworldLifecycleMocks.startMap).toHaveBeenCalledOnce()
      expect(vworldLifecycleMocks.setSceneMode).toHaveBeenCalledWith(
        expect.anything(),
        '3d',
        {
          lon: 127.15,
          lat: 37.45,
          height: expect.any(Number),
          heading: 0,
          tilt: -70,
        },
      )
    })
    expect(vworld2DLifecycleMocks.fitMap).toHaveBeenCalledOnce()

    rendered.rerender(createElement(MapView, mapViewProps('satellite')))

    await waitFor(() => {
      expect(vworldLifecycleMocks.removeDataSource).toHaveBeenCalledTimes(5)
    })
    const next2DIframe = installIframeContainer(rendered.container, 'vmap')
    expect(next2DIframe).not.toBe(iframe3D)

    await waitFor(() => {
      expect(vworld2DLifecycleMocks.startMap).toHaveBeenCalledTimes(2)
      expect(vworld2DLifecycleMocks.startMap).toHaveBeenLastCalledWith(
        expect.anything(),
        'vmap',
        expect.anything(),
        15_000,
        'satellite',
      )
      expect(vworld2DLifecycleMocks.fitMap).toHaveBeenCalledTimes(2)
    })
    expect(vworldLifecycleMocks.startMap).toHaveBeenCalledOnce()
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

  it.each([
    ['b', 'KeyB'],
    ['r', 'KeyR'],
    ['m', 'KeyM'],
    ['Enter', 'Enter'],
  ] as const)('relays the %s workspace edit shortcut from the map iframe', (key, code) => {
    const dispatchEvent = vi.fn((relayedEvent: Event) => {
      relayedEvent.preventDefault()
      return false
    })
    const ownerWindow = { document, dispatchEvent } as unknown as Window
    const event = new KeyboardEvent('keydown', { key, code, cancelable: true })

    expect(relayMapOverlayShortcut(event, ownerWindow)).toBe(true)
    expect(event.defaultPrevented).toBe(true)
    expect(dispatchEvent).toHaveBeenCalledOnce()
    expect(dispatchEvent.mock.calls[0][0]).toMatchObject({ key, code, shiftKey: false })
  })

  it('preserves Shift+Enter while blocking other shifted workspace keys', () => {
    const dispatchEvent = vi.fn((relayedEvent: Event) => {
      relayedEvent.preventDefault()
      return false
    })
    const ownerWindow = { document, dispatchEvent } as unknown as Window
    const saveAndNext = new KeyboardEvent('keydown', {
      key: 'Enter',
      code: 'Enter',
      shiftKey: true,
      cancelable: true,
    })

    expect(relayMapOverlayShortcut(saveAndNext, ownerWindow)).toBe(true)
    expect(dispatchEvent.mock.calls[0][0]).toMatchObject({ key: 'Enter', shiftKey: true })
    expect(
      relayMapOverlayShortcut(
        new KeyboardEvent('keydown', { key: 'b', code: 'KeyB', shiftKey: true }),
        ownerWindow,
      ),
    ).toBe(false)
    expect(dispatchEvent).toHaveBeenCalledOnce()
  })

  it('does not relay modified or editable-target workspace keys', () => {
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
