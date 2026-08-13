import { describe, expect, it, vi } from 'vitest'
import {
  assertVWorld2DSdk,
  createVWorld2DDataSource,
  fitVWorld2DMap,
  handleVWorld2DClick,
  renderVWorld2DScene,
  selectedFrameRadiusForResolution,
  startVWorld2DMap,
  VWORLD_2D_BASE_WMTS_URL,
  vworld2DOverlayHoverTarget,
} from './vworld2d'

class FakeFeature {
  values = new Map<string, unknown>()
  style: unknown

  constructor(options: Record<string, unknown> = {}) {
    Object.entries(options).forEach(([key, value]) => this.values.set(key, value))
  }

  get(key: string) {
    return this.values.get(key)
  }

  set(key: string, value: unknown) {
    this.values.set(key, value)
  }

  setStyle(style: unknown) {
    this.style = style
  }
}

function fakeSdk() {
  const options: Record<string, unknown>[] = []
  const tileSourceOptions: Record<string, unknown>[] = []
  const source = {
    features: [] as FakeFeature[],
    addFeatures(features: FakeFeature[]) {
      this.features.push(...features)
    },
    clear() {
      this.features = []
    },
  }
  const view = {
    fit: vi.fn(),
    setCenter: vi.fn(),
    setZoom: vi.fn(),
  }
  const map = {
    picked: null as FakeFeature | null,
    addLayer: vi.fn(),
    removeLayer: vi.fn(),
    getView: () => view,
    getSize: () => [800, 600],
    on: vi.fn(),
    un: vi.fn(),
    forEachFeatureAtPixel(_pixel: unknown, callback: (feature: FakeFeature) => unknown) {
      return this.picked ? callback(this.picked) : undefined
    },
    updateSize: vi.fn(),
    setTarget: vi.fn(),
  }

  class FakeMap {
    constructor(mapOptions: Record<string, unknown>) {
      options.push(mapOptions)
      return map
    }
  }
  class FakeView {
    constructor() {
      return view
    }
  }
  class FakeVectorSource {
    constructor() {
      return source
    }
  }
  class FakeVectorLayer {}
  class FakeTileLayer {
    constructor(readonly value: unknown) {}
  }
  class FakeXYZSource {
    constructor(sourceOptions: Record<string, unknown>) {
      tileSourceOptions.push(sourceOptions)
    }
  }
  class FakeGeometry {
    constructor(readonly value: unknown) {}
  }
  class FakeStyle {
    constructor(readonly value: unknown) {}
  }

  const ol = {
    Map: FakeMap,
    View: FakeView,
    Feature: FakeFeature,
    geom: {
      Point: FakeGeometry,
      MultiPoint: FakeGeometry,
      LineString: FakeGeometry,
      MultiLineString: FakeGeometry,
      Polygon: FakeGeometry,
      MultiPolygon: FakeGeometry,
      GeometryCollection: FakeGeometry,
    },
    layer: { Vector: FakeVectorLayer, Tile: FakeTileLayer },
    source: { Vector: FakeVectorSource, XYZ: FakeXYZSource },
    style: {
      Style: FakeStyle,
      Stroke: FakeStyle,
      Fill: FakeStyle,
      Circle: FakeStyle,
    },
    proj: {
      fromLonLat: ([lon, lat]: number[]) => [lon * 10, lat * 10],
      toLonLat: ([x, y]: number[]) => [x / 10, y / 10],
    },
    extent: { boundingExtent: (coordinates: unknown[]) => coordinates },
  }
  const frameWindow = {
    ol,
    vworldIsValid: 'true',
    setTimeout: window.setTimeout.bind(window),
  } as unknown as Window

  return { frameWindow, map, options, source, tileSourceOptions, view }
}

describe('VWorld 2D general-map adapter', () => {
  it('rejects an API key/domain mismatch', () => {
    expect(() =>
      assertVWorld2DSdk({
        vworldIsValid: 'false',
        vworldErrMsg: '등록하신 API Key와 URI가 일치하지 않습니다.',
      } as unknown as Window),
    ).toThrow('등록하신 API Key와 URI가 일치하지 않습니다.')
  })

  it('starts OpenLayers with only the official VWorld Base WMTS layer', async () => {
    const { frameWindow, map, options, tileSourceOptions } = fakeSdk()
    const runtime = await startVWorld2DMap(frameWindow, 'vmap', {
      lon: 127,
      lat: 37,
      height: 1_000,
    })

    expect(runtime.map).toBe(map)
    expect(options).toHaveLength(1)
    expect(options[0]).toMatchObject({
      target: 'vmap',
      layers: [expect.anything()],
      controls: [],
      logo: false,
    })
    expect(tileSourceOptions).toEqual([{
      url: VWORLD_2D_BASE_WMTS_URL,
      crossOrigin: 'anonymous',
      minZoom: 6,
      maxZoom: 19,
    }])
    expect(JSON.stringify({ options, tileSourceOptions })).not.toContain('openstreetmap.org')
    expect(map.updateSize).toHaveBeenCalled()
  })

  it('renders route, frame, and SHP features and dispatches map clicks', async () => {
    const sdk = fakeSdk()
    const runtime = await startVWorld2DMap(sdk.frameWindow, 'vmap', {
      lon: 127,
      lat: 37,
      height: 1_000,
    })
    const input = {
      route: {
        type: 'FeatureCollection' as const,
        features: [{
          type: 'Feature' as const,
          properties: { track_color: '#579cf2' },
          geometry: { type: 'LineString' as const, coordinates: [[127, 37], [127.1, 37.1]] },
        }],
      },
      routeRange: { type: 'FeatureCollection' as const, features: [] },
      frames: {
        type: 'FeatureCollection' as const,
        features: [{
          type: 'Feature' as const,
          id: 'frame-1',
          properties: { id: 'frame-1', selected: 1 },
          geometry: { type: 'Point' as const, coordinates: [127, 37] },
        }],
      },
      overlay: {
        type: 'FeatureCollection' as const,
        features: [{
          type: 'Feature' as const,
          id: 'feature-7',
          properties: {
            __overlay_layer_id: 'layer-a',
            __overlay_feature_id: 'feature-7',
            __overlay_color: '#ffb84d',
            NAME: '주의 표지',
          },
          geometry: { type: 'Point' as const, coordinates: [127.2, 37.2] },
        }],
      },
      onFrame: vi.fn(),
      onOverlay: vi.fn(),
    }

    const dataSource = createVWorld2DDataSource(runtime)
    renderVWorld2DScene(runtime, dataSource, input)
    expect(sdk.source.features).toHaveLength(3)

    sdk.map.picked = sdk.source.features.find((feature) => feature.get('frame_id') === 'frame-1') ?? null
    handleVWorld2DClick(runtime, { pixel: [1, 1], coordinate: [1270, 370] }, input)
    expect(input.onFrame).toHaveBeenCalledWith('frame-1')

    sdk.map.picked = sdk.source.features.find(
      (feature) => feature.get('overlay_feature_id') === 'feature-7',
    ) ?? null
    expect(vworld2DOverlayHoverTarget(runtime, { pixel: [12, 24] })).toMatchObject({
      layerId: 'layer-a',
      featureId: 'feature-7',
      pixel: [12, 24],
      properties: { NAME: '주의 표지' },
    })

    sdk.map.picked = null
    const onCoordinate = vi.fn()
    handleVWorld2DClick(runtime, { pixel: [2, 2], coordinate: [1270, 370] }, input, onCoordinate)
    expect(onCoordinate).toHaveBeenCalledWith([127, 37, 0])
  })

  it('skips invalid WGS84 geometry before projecting it', async () => {
    const sdk = fakeSdk()
    const runtime = await startVWorld2DMap(sdk.frameWindow, 'vmap', {
      lon: 127,
      lat: 37,
      height: 1_000,
    })
    const dataSource = createVWorld2DDataSource(runtime)

    renderVWorld2DScene(runtime, dataSource, {
      route: { type: 'FeatureCollection', features: [] },
      routeRange: { type: 'FeatureCollection', features: [] },
      frames: {
        type: 'FeatureCollection',
        features: [{
          type: 'Feature',
          properties: { id: 'invalid' },
          geometry: { type: 'Point', coordinates: [Number.NaN, 37] },
        }],
      },
      overlay: { type: 'FeatureCollection', features: [] },
      onFrame: vi.fn(),
      onOverlay: vi.fn(),
    })

    expect(sdk.source.features).toHaveLength(0)
  })

  it('uses the OpenLayers 3 fit signature', async () => {
    const sdk = fakeSdk()
    const runtime = await startVWorld2DMap(sdk.frameWindow, 'vmap', {
      lon: 127,
      lat: 37,
      height: 1_000,
    })

    fitVWorld2DMap(runtime, [[127, 37], [127.1, 37.1]])

    expect(sdk.view.fit).toHaveBeenCalledWith(
      [[1270, 370], [1271, 371]],
      [800, 600],
      { padding: [72, 72, 72, 72], maxZoom: 18 },
    )
  })

  it('scales the selected frame marker with map resolution', () => {
    expect(selectedFrameRadiusForResolution(40)).toBe(3)
    expect(selectedFrameRadiusForResolution(8)).toBe(4)
    expect(selectedFrameRadiusForResolution(1)).toBe(5)
    expect(selectedFrameRadiusForResolution(0.1)).toBe(6)
    expect(selectedFrameRadiusForResolution(Number.NaN)).toBe(5)
  })
})
