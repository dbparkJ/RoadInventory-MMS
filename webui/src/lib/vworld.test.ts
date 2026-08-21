import { describe, expect, it, vi } from 'vitest'
import {
  MAP_SELECTED_FEATURE_COLOR,
  MAP_SELECTED_FRAME_COLOR,
} from './mapSelectionColors'
import {
  assertVWorldSdk,
  cameraTargetForSceneMode,
  cameraTargetForCoordinates,
  pickedEntityId,
  pickedEntityIdsAtPosition,
  renderVWorldOverlay,
  renderVWorldScene,
  selectedFrameDistanceScale,
  startVWorldMap,
  vworldCanvasWgs84Coordinate,
  type VWorldCustomDataSource,
  type VWorldRuntime,
} from './vworld'

class FakeCollection {
  values: Array<Record<string, unknown>> = []
  suspendCount = 0
  resumeCount = 0

  add(options: Record<string, unknown>) {
    this.values.push(options)
    return { id: String(options.id) }
  }

  removeAll() {
    this.values = []
  }

  suspendEvents() {
    this.suspendCount += 1
  }

  resumeEvents() {
    this.resumeCount += 1
  }
}

function fakeRuntime(collection = new FakeCollection()): {
  runtime: VWorldRuntime
  source: VWorldCustomDataSource
  collection: FakeCollection
} {
  class FakeColor {
    constructor(
      readonly value: string,
      readonly alpha = 1,
    ) {}

    withAlpha(alpha: number) {
      return new FakeColor(this.value, alpha)
    }
  }

  class FakePolygonHierarchy {
    constructor(
      readonly positions: unknown[],
      readonly holes: unknown[] = [],
    ) {}
  }

  class FakeNearFarScalar {
    constructor(
      readonly near: number,
      readonly nearValue: number,
      readonly far: number,
      readonly farValue: number,
    ) {}
  }

  const source = { entities: collection } as unknown as VWorldCustomDataSource
  const runtime = {
    Cesium: {
      Cartesian3: {
        fromDegrees: (lon: number, lat: number, height = 0) => ({ lon, lat, height }),
      },
      Color: {
        fromCssColorString: (value: string) => new FakeColor(value),
      },
      PolygonHierarchy: FakePolygonHierarchy,
      NearFarScalar: FakeNearFarScalar,
      HeightReference: { CLAMP_TO_GROUND: 'ground' },
      ClassificationType: { BOTH: 'both' },
    },
  } as unknown as VWorldRuntime
  return { runtime, source, collection }
}

describe('VWorld WebGL 3.0 adapter', () => {
  it('converts a 3D canvas pointer into WGS84 for the survey ghost line', () => {
    const runtime = {
      viewer: {
        scene: {
          pickPosition: vi.fn(() => ({ x: 1, y: 2, z: 3 })),
        },
      },
      Cesium: {
        Cartographic: {
          fromCartesian: vi.fn(() => ({ longitude: Math.PI / 2, latitude: Math.PI / 4 })),
        },
        Math: { toDegrees: (radians: number) => radians * 180 / Math.PI },
      },
    } as unknown as VWorldRuntime

    expect(vworldCanvasWgs84Coordinate(runtime, { x: 12, y: 34 })).toEqual([90, 45])
  })

  it('returns no survey preview coordinate when the pointer misses the globe', () => {
    const runtime = {
      viewer: { scene: { pickPosition: vi.fn(), globe: {} }, camera: {} },
      Cesium: {},
    } as unknown as VWorldRuntime

    expect(vworldCanvasWgs84Coordinate(runtime, { x: 12, y: 34 })).toBeNull()
  })

  it('derives safe north-up 2D and perspective 3D camera targets', () => {
    const target = { lon: 127, lat: 37, height: 420, heading: 35, tilt: -62 }

    expect(cameraTargetForSceneMode(target, '2d')).toEqual({
      lon: 127,
      lat: 37,
      height: 650,
      heading: 0,
      tilt: -90,
    })
    expect(cameraTargetForSceneMode({ ...target, tilt: -90 }, '3d')).toEqual({
      lon: 127,
      lat: 37,
      height: 420,
      heading: 35,
      tilt: -65,
    })
  })

  it('keeps the selected-frame distance scale compact at every camera distance', () => {
    const scale = selectedFrameDistanceScale()
    expect(scale).toEqual({ near: 60, nearValue: 0.9, far: 25_000, farValue: 0.3 })
    expect(15 * scale.nearValue).toBeLessThan(16)
    expect(15 * scale.farValue).toBeLessThan(5)
  })

  it('rejects an API key/domain mismatch with the official loader message', () => {
    expect(() =>
      assertVWorldSdk({
        vworldIsValid: 'false',
        vworldErrMsg: '등록하신 API Key와 URI가 일치하지 않습니다.',
      } as unknown as Window),
    ).toThrow('등록하신 API Key와 URI가 일치하지 않습니다.')
  })

  it('starts the map with the documented option sequence before resolving readiness', async () => {
    const calls: string[] = []
    const viewer = {
      scene: { canvas: document.createElement('canvas'), pick: vi.fn() },
      dataSources: { add: vi.fn(), remove: vi.fn() },
    }
    const sdk: Record<string, unknown> = {}

    class FakeMap {
      onClick = { addEventListener: vi.fn(), removeEventListener: vi.fn() }

      setOption() {
        calls.push('setOption')
      }

      setMapId() {
        calls.push('setMapId')
      }

      setInitPosition() {
        calls.push('setInitPosition')
      }

      setLogoVisible() {
        calls.push('setLogoVisible')
      }

      setNavigationZoomVisible() {
        calls.push('setNavigationZoomVisible')
      }

      start() {
        calls.push('start')
        queueMicrotask(() => (sdk.ws3dInitCallBack as (() => void) | undefined)?.())
      }

      moveTo() {}
      updateSize() {}
      clear() {}
    }

    Object.assign(sdk, {
      Map: FakeMap,
      CoordZ: class FakeCoordZ {},
      Direction: class FakeDirection {},
      CameraPosition: class FakeCameraPosition {},
    })
    const frameWindow = {
      vw: sdk,
      Cesium: {},
      ws3d: { viewer },
      vworldIsValid: 'true',
      setTimeout: window.setTimeout.bind(window),
      clearTimeout: window.clearTimeout.bind(window),
    } as unknown as Window

    const runtime = await startVWorldMap(frameWindow, 'vmap', {
      lon: 126.978,
      lat: 37.5665,
      height: 1_000,
    })

    expect(runtime.viewer).toBe(viewer)
    expect(calls).toEqual([
      'setOption',
      'setMapId',
      'setInitPosition',
      'setLogoVisible',
      'setNavigationZoomVisible',
      'start',
    ])
  })

  it('derives a finite VWorld camera target that contains the complete route', () => {
    const target = cameraTargetForCoordinates([
      [126.9, 37.4],
      [127.1, 37.6],
    ])

    expect(target.lon).toBeCloseTo(127)
    expect(target.lat).toBeCloseTo(37.5)
    expect(target.height).toBeGreaterThan(20_000)
    expect(target.height).toBeLessThanOrEqual(60_000)
    expect(target.tilt).toBe(-70)
  })

  it('renders route, frame, and polygon-with-hole entities and preserves click targets', () => {
    const { runtime, source, collection } = fakeRuntime()
    const onFrame = vi.fn()
    const onOverlay = vi.fn()

    const targets = renderVWorldScene(runtime, source, {
      route: {
        type: 'FeatureCollection',
        features: [
          {
            type: 'Feature',
            properties: { track_color: '#579cf2' },
            geometry: {
              type: 'LineString',
              coordinates: [
                [127, 37],
                [127.1, 37.1],
              ],
            },
          },
        ],
      },
      routeRange: { type: 'FeatureCollection', features: [] },
      frames: {
        type: 'FeatureCollection',
        features: [
          {
            type: 'Feature',
            id: 'frame-1',
            properties: { id: 'frame-1', selected: 1, track_color: '#579cf2' },
            geometry: { type: 'Point', coordinates: [127, 37] },
          },
        ],
      },
      overlay: {
        type: 'FeatureCollection',
        features: [
          {
            type: 'Feature',
            id: 'feature-7',
            properties: {
              __overlay_layer_id: 'layer-a',
              __overlay_feature_id: 'feature-7',
              __overlay_color: '#ffb84d',
              __overlay_selected: 1,
            },
            geometry: {
              type: 'Polygon',
              coordinates: [
                [
                  [127, 37],
                  [127.2, 37],
                  [127.2, 37.2],
                  [127, 37],
                ],
                [
                  [127.05, 37.05],
                  [127.1, 37.05],
                  [127.05, 37.05],
                ],
              ],
            },
          },
        ],
      },
      onFrame,
      onOverlay,
    })

    expect(collection.values.map((entity) => entity.id)).toEqual(
      expect.arrayContaining([
        'route:0:halo',
        'route:0',
        'frame:0:selected-halo',
        'frame:0',
        'overlay:0',
      ]),
    )
    expect(collection.suspendCount).toBe(1)
    expect(collection.resumeCount).toBe(1)
    const polygon = collection.values.find((entity) => entity.id === 'overlay:0')
    expect(
      (polygon?.polygon as { hierarchy: { holes: unknown[] } }).hierarchy.holes,
    ).toHaveLength(1)

    targets.get('frame:0')?.()
    targets.get('frame:0:selected-halo')?.()
    targets.get('overlay:0')?.()
    expect(onFrame).toHaveBeenCalledTimes(2)
    expect(onFrame).toHaveBeenCalledWith('frame-1')
    expect(onOverlay).toHaveBeenCalledWith('layer-a', 'feature-7')
    const selectedFrame = collection.values.find((entity) => entity.id === 'frame:0')
    const selectedHalo = collection.values.find((entity) => entity.id === 'frame:0:selected-halo')
    const selectedPolygon = collection.values.find((entity) => entity.id === 'overlay:0')
    expect((selectedFrame?.point as { pixelSize?: number }).pixelSize).toBe(8)
    expect((selectedHalo?.point as { pixelSize?: number }).pixelSize).toBe(15)
    expect(
      (selectedFrame?.point as { outlineColor?: { value?: string } }).outlineColor?.value,
    ).toBe(MAP_SELECTED_FRAME_COLOR)
    expect((selectedHalo?.point as { color?: { value?: string } }).color?.value).toBe(
      MAP_SELECTED_FRAME_COLOR,
    )
    expect(
      (selectedPolygon?.polygon as { material?: { value?: string } }).material?.value,
    ).toBe(MAP_SELECTED_FEATURE_COLOR)
    expect((selectedFrame?.point as { scaleByDistance?: unknown }).scaleByDistance).toMatchObject(
      selectedFrameDistanceScale(),
    )
  })

  it('maps every rendered SHP entity back to its hover properties', () => {
    const { runtime, source } = fakeRuntime()
    const hoverTargets = new Map()
    renderVWorldOverlay(
      runtime,
      source,
      {
        type: 'FeatureCollection',
        features: [{
          type: 'Feature',
          id: 'feature-7',
          properties: {
            __overlay_layer_id: 'layer-a',
            __overlay_feature_id: 'feature-7',
            NAME: '주의 표지',
          },
          geometry: {
            type: 'LineString',
            coordinates: [[127, 37], [127.1, 37.1]],
          },
        }],
      },
      vi.fn(),
      hoverTargets,
    )
    expect(hoverTargets.get('overlay:0')).toMatchObject({
      layerId: 'layer-a',
      featureId: 'feature-7',
      properties: { NAME: '주의 표지' },
    })
  })

  it('extracts Cesium entity ids from scene picks', () => {
    expect(pickedEntityId({ id: { id: 'frame:9' } })).toBe('frame:9')
    expect(pickedEntityId({ id: 'overlay:3' })).toBe('overlay:3')
    expect(pickedEntityId(undefined)).toBeNull()
  })

  it('drill-picks through route and survey lines in front-to-back order', () => {
    const scene = {
      pick: vi.fn(),
      drillPick: vi.fn(() => [
        { id: { id: 'route:0' } },
        { id: 'route:0:halo' },
        { id: { id: 'overlay:3' } },
        { id: { id: 'overlay:3' } },
      ]),
    }

    expect(pickedEntityIdsAtPosition(scene, { x: 10, y: 20 })).toEqual([
      'route:0',
      'route:0:halo',
      'overlay:3',
    ])
    expect(scene.drillPick).toHaveBeenCalledWith({ x: 10, y: 20 }, 32)
    expect(scene.pick).not.toHaveBeenCalled()
  })
})
