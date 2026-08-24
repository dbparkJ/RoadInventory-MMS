import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as THREE from 'three'
import { api } from '../lib/api'
import type { Frame, PanoramaProjectionMetadata } from '../types'
import PanoramaView, {
  deduplicatePanoramaDetectionBoxes,
  nearestPanoramaPointIndex,
  panoramaDetectionBox,
  panoramaDetectionBoxContainsUv,
  panoramaDetectionPointRadius,
  panoramaDetectionModelKey,
  panoramaDetectionModels,
  panoramaDetectionStrokeWidth,
  panoramaForwardYaw,
  panoramaFovAfterWheel,
  panoramaOverlayAtUv,
  panoramaPoleBaseMarkerRadiusPx,
  panoramaPoleBasePreviewProjection,
  panoramaRayYaw,
  panoramaRequestWidth,
  panoramaProgressiveWidths,
  panoramaSceneNavigationTarget,
  panoramaUvToScreenPosition,
  reconcilePanoramaDetectionBoxes,
  isPanoramaSceneClick,
  isPanoramaSceneControlTarget,
  type RenderPanoramaDetectionBox,
  type RenderPanoramaOverlayPoint,
} from './PanoramaView'

const threeSpies = vi.hoisted(() => ({
  rendererConstructed: vi.fn(),
  rendererDisposed: vi.fn(),
  textureLoads: vi.fn(),
  textureDisposed: vi.fn(),
  canvasFillText: vi.fn(),
  manualTextureLoads: false,
  pendingTextureLoads: [] as Array<{
    source: string
    succeed: () => void
    fail: () => void
  }>,
}))

const overlayWorkspaceMock = vi.hoisted(() => ({ current: null as unknown }))

vi.mock('../components/OverlayContext', () => ({
  useOptionalOverlayWorkspace: () => overlayWorkspaceMock.current,
}))

vi.mock('three', async (importOriginal) => {
  const actual = await importOriginal<typeof import('three')>()

  class FakeTexture {
    colorSpace: unknown
    needsUpdate = false
    wrapS: unknown
    minFilter: unknown
    magFilter: unknown

    dispose() {
      threeSpies.textureDisposed()
    }
  }

  class FakeCanvasTexture extends FakeTexture {
    constructor(_canvas: HTMLCanvasElement) {
      super()
    }
  }

  class FakeTextureLoader {
    load(
      source: string,
      onLoad?: (texture: FakeTexture) => void,
      _onProgress?: unknown,
      onError?: (reason: unknown) => void,
    ) {
      threeSpies.textureLoads(source)
      const texture = new FakeTexture()
      if (threeSpies.manualTextureLoads) {
        threeSpies.pendingTextureLoads.push({
          source,
          succeed: () => onLoad?.(texture),
          fail: () => onError?.(new Error(`Could not decode ${source}`)),
        })
      } else {
        queueMicrotask(() => onLoad?.(texture))
      }
      return texture
    }
  }

  class FakeScene {
    background: unknown
    children: unknown[] = []

    add(...objects: unknown[]) {
      this.children.push(...objects)
    }

    remove(object: unknown) {
      this.children = this.children.filter((candidate) => candidate !== object)
    }
  }

  class FakePerspectiveCamera {
    fov: number
    aspect: number

    constructor(fov: number, aspect: number) {
      this.fov = fov
      this.aspect = aspect
    }

    updateProjectionMatrix() {}
    lookAt() {}
  }

  class FakeRenderer {
    domElement = document.createElement('canvas')
    outputColorSpace: unknown

    constructor() {
      threeSpies.rendererConstructed()
    }

    setPixelRatio() {}
    setSize() {}
    render() {}

    dispose() {
      threeSpies.rendererDisposed()
    }
  }

  class FakeGeometry {
    scale() {}
    dispose() {}
  }

  class FakeMaterial {
    map: FakeTexture | null = null
    visible = true
    opacity = 1
    needsUpdate = false

    constructor(parameters: Record<string, unknown> = {}) {
      Object.assign(this, parameters)
    }

    dispose() {}
  }

  class FakeMesh {
    renderOrder = 0

    constructor(
      readonly geometry: unknown,
      readonly material: unknown,
    ) {}
  }

  return {
    ...actual,
    CanvasTexture: FakeCanvasTexture,
    Color: class {},
    Mesh: FakeMesh,
    MeshBasicMaterial: FakeMaterial,
    PerspectiveCamera: FakePerspectiveCamera,
    Scene: FakeScene,
    SphereGeometry: FakeGeometry,
    TextureLoader: FakeTextureLoader,
    WebGLRenderer: FakeRenderer,
  }
})

vi.mock('../lib/api', () => ({
  ApiError: class ApiError extends Error {
    constructor(
      message: string,
      readonly status = 0,
    ) {
      super(message)
    }
  },
  api: {
    panorama: vi.fn(() => new Promise(() => undefined)),
    panoramaPoints: vi.fn(() => new Promise(() => undefined)),
    panoramaProjectionMetadata: vi.fn(() => Promise.resolve({
      frame_id: 'frame-12',
      coordinate_space: 'dataset',
      projection: 'normalized_equirectangular',
      origin: [0, 0, 0],
      forward: [1, 0, 0],
      right: [0, 1, 0],
      up: [0, 0, 1],
      yaw_offset_deg: 0,
      pitch_offset_deg: 0,
    })),
    frameDetections: vi.fn(() => Promise.resolve({
      dataset_id: 'dataset-1',
      frame_id: 'frame-12',
      coordinate_space: 'panorama_equirectangular_pixels',
      projection: 'equirectangular',
      items: [],
      count: 0,
      model_count: 0,
      truncated: false,
    })),
    frameAddress: vi.fn(() => Promise.resolve({
      dataset_id: 'dataset-1',
      frame_id: 'frame-12',
      coordinate: { lon: 126.978, lat: 37.5665 },
      address: '서울특별시 중구 세종대로 110',
      address_type: 'road',
      zipcode: '04524',
      source: 'vworld',
    })),
  },
}))

const FRAME: Frame = {
  id: 'frame-12',
  index: 12,
  track_id: 'track-1',
  timestamp: '2026-08-03T09:30:00.000Z',
  image_name: 'frame-12.jpg',
  coordinate: { lon: 126.978, lat: 37.5665 },
  has_panorama: true,
  has_points: true,
}

const NEXT_FRAME: Frame = {
  ...FRAME,
  id: 'frame-13',
  index: 13,
  timestamp: '2026-08-03T09:30:01.000Z',
}

const PROJECTION_METADATA: PanoramaProjectionMetadata = {
  frame_id: FRAME.id,
  coordinate_space: 'dataset',
  projection: 'normalized_equirectangular',
  origin: [0, 0, 0],
  forward: [1, 0, 0],
  right: [0, 1, 0],
  up: [0, 0, 1],
  yaw_offset_deg: 0,
  pitch_offset_deg: 0,
}

type PanoramaResult =
  | { kind: 'url'; value: string }
  | { kind: 'blob'; value: Blob }

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

function mmsoPayload(): ArrayBuffer {
  const buffer = new ArrayBuffer(55)
  const bytes = new Uint8Array(buffer)
  bytes.set([77, 77, 83, 79])
  const view = new DataView(buffer)
  view.setUint16(4, 1, true)
  view.setUint16(6, 3, true)
  view.setUint32(8, 1, true)
  view.setFloat32(40, 0.5, true)
  view.setFloat32(44, 0.5, true)
  view.setFloat32(48, 10, true)
  bytes.set([10, 20, 30], 52)
  return buffer
}

beforeEach(() => {
  overlayWorkspaceMock.current = null
  threeSpies.rendererConstructed.mockClear()
  threeSpies.rendererDisposed.mockClear()
  threeSpies.textureLoads.mockClear()
  threeSpies.textureDisposed.mockClear()
  threeSpies.canvasFillText.mockClear()
  threeSpies.manualTextureLoads = false
  threeSpies.pendingTextureLoads.length = 0
  vi.mocked(api.panorama).mockReset().mockImplementation(() => new Promise(() => undefined))
  vi.mocked(api.panoramaPoints).mockReset().mockImplementation(() => new Promise(() => undefined))
  vi.mocked(api.panoramaProjectionMetadata).mockReset().mockResolvedValue(PROJECTION_METADATA)
  vi.mocked(api.frameDetections).mockReset().mockResolvedValue({
    dataset_id: 'dataset-1',
    frame_id: 'frame-12',
    coordinate_space: 'panorama_equirectangular_pixels',
    projection: 'equirectangular',
    items: [],
    count: 0,
    model_count: 0,
    truncated: false,
  })
  vi.mocked(api.frameAddress).mockReset().mockResolvedValue({
    dataset_id: 'dataset-1',
    frame_id: 'frame-12',
    coordinate: { lon: 126.978, lat: 37.5665 },
    address: '서울특별시 중구 세종대로 110',
    address_type: 'road',
    zipcode: '04524',
    source: 'vworld',
  })
  vi.stubGlobal('requestAnimationFrame', vi.fn(() => 1))
  vi.stubGlobal('cancelAnimationFrame', vi.fn())
  Object.defineProperty(URL, 'createObjectURL', {
    configurable: true,
    value: vi.fn((_blob: Blob) => 'blob:test'),
  })
  Object.defineProperty(URL, 'revokeObjectURL', {
    configurable: true,
    value: vi.fn(),
  })
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(
    ((contextId: string) => {
      if (contextId !== '2d') return null
      return {
        createImageData: () => ({ data: new Uint8ClampedArray(4) }),
        putImageData: vi.fn(),
        beginPath: vi.fn(),
        arc: vi.fn(),
        fill: vi.fn(),
        stroke: vi.fn(),
        strokeRect: vi.fn(),
        fillRect: vi.fn(),
        fillText: threeSpies.canvasFillText,
        measureText: vi.fn(() => ({ width: 50 })),
      } as unknown as CanvasRenderingContext2D
    }) as HTMLCanvasElement['getContext'],
  )
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('panoramaRequestWidth', () => {
  it('scales with the viewport while enforcing each quality budget', () => {
    expect(panoramaRequestWidth(640, 1, 'high')).toBe(4096)
    expect(panoramaRequestWidth(900, 1, 'high')).toBe(4096)
    expect(panoramaRequestWidth(1920, 2, 'high')).toBe(4096)
    expect(panoramaRequestWidth(640, 1, 'ultra')).toBe(8192)
    expect(panoramaRequestWidth(1280, 1, 'fast')).toBe(1920)
    expect(panoramaRequestWidth(1920, 2, 'fast')).toBe(2048)
  })

  it('loads a lightweight panorama before the selected quality', () => {
    expect(panoramaProgressiveWidths(4096)).toEqual([768, 4096])
    expect(panoramaProgressiveWidths(512)).toEqual([512])
  })
})

describe('panoramaForwardYaw', () => {
  it('resets to image-space forward with the operator correction', () => {
    expect(panoramaForwardYaw(0)).toBe(-180)
    expect(panoramaForwardYaw(7.5)).toBe(-172.5)
  })

  it('maps wheel direction to the bounded panorama FOV', () => {
    expect(panoramaFovAfterWheel(72, -1)).toBe(68)
    expect(panoramaFovAfterWheel(72, 1)).toBe(76)
    expect(panoramaFovAfterWheel(28, -1)).toBe(28)
    expect(panoramaFovAfterWheel(95, 1)).toBe(95)
    expect(panoramaFovAfterWheel(54, 0)).toBe(54)
  })
})

describe('panorama pole-base preview projection', () => {
  it('projects the dataset base with the current frame calibration', () => {
    expect(panoramaPoleBasePreviewProjection({
      datasetPosition: [10, 0, 0],
      proposalFrameId: FRAME.id,
      currentFrameId: FRAME.id,
      metadata: PROJECTION_METADATA,
      color: '#FF00AA',
      sizeM: 0.08,
    })).toMatchObject({
      frameId: FRAME.id,
      u: 0.5,
      v: 0.5,
      depth: 10,
      color: '#ff00aa',
      sizeM: 0.08,
    })
  })

  it('rejects proposal and calibration frame mismatches', () => {
    expect(panoramaPoleBasePreviewProjection({
      datasetPosition: [10, 0, 0],
      proposalFrameId: NEXT_FRAME.id,
      currentFrameId: FRAME.id,
      metadata: PROJECTION_METADATA,
    })).toBeNull()
    expect(panoramaPoleBasePreviewProjection({
      datasetPosition: [10, 0, 0],
      proposalFrameId: FRAME.id,
      currentFrameId: FRAME.id,
      metadata: { ...PROJECTION_METADATA, frame_id: NEXT_FRAME.id },
    })).toBeNull()
  })

  it('scales physical marker radius by distance and clamps tiny screen markers', () => {
    expect(panoramaPoleBaseMarkerRadiusPx(0.08, 10)).toBeCloseTo(5.21, 1)
    expect(panoramaPoleBaseMarkerRadiusPx(0.16, 10)).toBeGreaterThan(
      panoramaPoleBaseMarkerRadiusPx(0.08, 10),
    )
    expect(panoramaPoleBaseMarkerRadiusPx(0.08, 1_000)).toBe(3)
  })
})

describe('panorama scene click navigation', () => {
  const previous: Frame = {
    ...FRAME,
    id: 'frame-11',
    index: 11,
    coordinate: { lon: 126.978, lat: 37.5655 },
  }
  const current: Frame = { ...FRAME, heading: 0 }
  const next: Frame = {
    ...NEXT_FRAME,
    coordinate: { lon: 126.978, lat: 37.5675 },
  }

  it('resolves the clicked ray only to a loaded neighbor with a coordinate bearing', () => {
    expect(panoramaSceneNavigationTarget(
      current,
      [previous, current, next],
      -180,
      -180,
    )?.target?.frame.id).toBe('frame-13')
    expect(panoramaSceneNavigationTarget(
      current,
      [previous, current, next],
      0,
      -180,
    )?.target?.frame.id).toBe('frame-11')
    expect(panoramaSceneNavigationTarget(
      current,
      [],
      -180,
      -180,
    )).toBeNull()
    expect(panoramaSceneNavigationTarget(
      current,
      [{ ...next, coordinate: null }, current],
      -180,
      -180,
    )).toBeNull()
  })

  it('separates a short click from a panorama drag and derives yaw from the popup ray', () => {
    expect(isPanoramaSceneClick({ x: 10, y: 10 }, { x: 13, y: 14 })).toBe(true)
    expect(isPanoramaSceneClick({ x: 10, y: 10 }, { x: 16, y: 10 })).toBe(false)
    expect(panoramaRayYaw({ x: -1, z: 0 }, 25)).toBe(180)
    expect(panoramaRayYaw({ x: 0, z: -1 }, 25)).toBe(-90)
    expect(panoramaRayYaw({ x: 0, z: 0 }, 25)).toBe(25)

    const popupDocument = document.implementation.createHTMLDocument('panorama-popup')
    const popupButton = popupDocument.createElement('button')
    const popupCanvas = popupDocument.createElement('canvas')
    expect(isPanoramaSceneControlTarget(popupButton)).toBe(true)
    expect(isPanoramaSceneControlTarget(popupCanvas)).toBe(false)
  })
})

describe('nearestPanoramaPointIndex', () => {
  it('selects a depth sample across the equirectangular seam', () => {
    const coordinates = new Float32Array([
      0.99, 0.5, 12,
      0.4, 0.5, 8,
    ])
    expect(nearestPanoramaPointIndex(0.01, 0.5, coordinates, 2, 0.03)).toBe(0)
    expect(nearestPanoramaPointIndex(0.2, 0.2, coordinates, 2, 0.03)).toBeNull()
  })
})

describe('panoramaDetectionBox', () => {
  it('builds an image-matched class box and ignores detections from other frames', () => {
    const properties = {
      IMG_NAME: 'captures/frame-12.jpg',
      CLASS_NM: 'traffic_sign',
      CONF: 0.876,
      BBOX_L: 120,
      BBOX_T: 80,
      BBOX_R: 260,
      BBOX_B: 220,
      PANO_W: 8192,
      PANO_H: 4096,
    }

    expect(panoramaDetectionBox(properties, 'frame-12.jpg')).toEqual({
      left: 120,
      top: 80,
      right: 260,
      bottom: 220,
      panoramaWidth: 8192,
      panoramaHeight: 4096,
      label: 'traffic_sign\nconf 88%',
    })
    expect(panoramaDetectionBox(properties, 'frame-13.jpg')).toBeNull()
  })

  it('accepts pipeline and uploaded bbox aliases plus extension-only image differences', () => {
    expect(panoramaDetectionBox({
      IMAGE_PATH: 'captures/frame-12.jpeg',
      BBOX_XYXY: '[120, 80, 260, 220]',
      IMAGE_WIDTH: 8192,
      IMAGE_HEIGHT: 4096,
      CLASS_NAME: 'traffic_light',
      CONFIDENCE: 91.2,
    }, 'FRAME-12.JPG')).toEqual({
      left: 120,
      top: 80,
      right: 260,
      bottom: 220,
      panoramaWidth: 8192,
      panoramaHeight: 4096,
      label: 'traffic_light\nconf 91%',
    })
  })

  it('hit-tests a detection box across the panorama seam', () => {
    const box = {
      left: 790,
      top: 100,
      right: 830,
      bottom: 180,
      panoramaWidth: 800,
      panoramaHeight: 400,
      label: 'sign',
    }
    expect(panoramaDetectionBoxContainsUv(box, 0.01, 0.35)).toBe(true)
    expect(panoramaDetectionBoxContainsUv(box, 0.5, 0.35)).toBe(false)
  })

  it('uses smaller panorama detection markers while preserving selection emphasis', () => {
    expect(panoramaDetectionPointRadius(false, 10)).toBe(2)
    expect(panoramaDetectionPointRadius(false, 30)).toBe(1.5)
    expect(panoramaDetectionPointRadius(true, 30)).toBe(3.5)
  })

  it('hit-tests boxless SHP points in a fixed 9px screen radius across zoom and viewport sizes', () => {
    const point: RenderPanoramaOverlayPoint = {
      feature_id: 'feature-1',
      layerId: 'layer-1',
      layerName: '시설물',
      u: 0.5,
      v: 0.5,
      depth: 12,
      dataset_position: [1, 2, 3],
      properties: { class_nm: '지주' },
      color: '#22c55e',
      selected: false,
    }
    const projections = [
      { viewportWidth: 1200, viewportHeight: 600, verticalFovDeg: 35, yawDeg: -180, pitchDeg: 0 },
      { viewportWidth: 360, viewportHeight: 640, verticalFovDeg: 95, yawDeg: -180, pitchDeg: 0 },
    ]
    projections.forEach((projection) => {
      const center = panoramaUvToScreenPosition(point.u, point.v, projection)
      expect(center).not.toBeNull()
      expect(panoramaOverlayAtUv([point], point.u, point.v, [], {
        ...projection,
        pointerX: center!.x + 9,
        pointerY: center!.y,
      })).toMatchObject({ featureId: 'feature-1' })
      expect(panoramaOverlayAtUv([point], point.u, point.v, [], {
        ...projection,
        pointerX: center!.x + 10,
        pointerY: center!.y,
      })).toBeNull()
    })

    const wideProjection = projections[0]
    const oldUvRadiusClick = panoramaUvToScreenPosition(0.525, 0.5, wideProjection)
    expect(oldUvRadiusClick).not.toBeNull()
    expect(panoramaOverlayAtUv([point], 0.525, 0.5, [], {
      ...wideProjection,
      pointerX: oldUvRadiusClick!.x,
      pointerY: oldUvRadiusClick!.y,
    })).toBeNull()
  })

  it('uses thin box strokes', () => {
    expect(panoramaDetectionStrokeWidth(false)).toBe(1.5)
    expect(panoramaDetectionStrokeWidth(true)).toBe(3)
  })

  it('builds stable model options and retains empty models reported by the API', () => {
    expect(panoramaDetectionModelKey('run-specific-id', 'stable-model-id')).toBe('stable-model-id')
    expect(panoramaDetectionModels([
      { source_id: 'source-a', model_id: 'model-id-a', source_name: 'best.pt', count: 3 },
      { source_id: 'source-empty', model_id: 'model-id-empty', source_name: 'best.pt', count: 0 },
    ], [])).toMatchObject([
      { key: 'model-id-a', name: 'best.pt', count: 3 },
      { key: 'model-id-empty', name: 'best.pt', count: 0 },
    ])
  })

  it('deduplicates raw observations and lets unlinked boxes expose a preview hit', () => {
    const box: RenderPanoramaDetectionBox = {
      sourceId: 'source-model-a',
      observationId: 'det-1',
      layerId: 'layer-1',
      layerName: '검출 결과',
      properties: { class_nm: 'traffic_sign', conf: 0.91 },
      color: '#ffb84d',
      selected: false,
      detectionBox: {
        left: 400,
        top: 150,
        right: 500,
        bottom: 250,
        panoramaWidth: 1000,
        panoramaHeight: 500,
        label: 'traffic_sign\nconf 91%',
      },
    }
    expect(deduplicatePanoramaDetectionBoxes([
      box,
      { ...box, layerId: 'duplicate-import' },
    ])).toEqual([box])
    expect(deduplicatePanoramaDetectionBoxes([
      box,
      { ...box, sourceId: 'source-model-b', layerId: 'different-model' },
    ])).toHaveLength(2)
    expect(panoramaOverlayAtUv([], 0.45, 0.4, [box])).toEqual({
      layerName: '검출 결과',
      featureId: 'det-1',
      properties: { class_nm: 'traffic_sign', conf: 0.91 },
      color: '#ffb84d',
    })

    const linked = { ...box, featureId: 'feature-7' }
    expect(panoramaOverlayAtUv([] as RenderPanoramaOverlayPoint[], 0.45, 0.4, [linked])).toMatchObject({
      layerId: 'layer-1',
      featureId: 'feature-7',
    })
  })

  it('reconciles a raw box with its visible SHP representative for Details', () => {
    const detectionBox = {
      left: 400,
      top: 150,
      right: 500,
      bottom: 250,
      panoramaWidth: 1000,
      panoramaHeight: 500,
      label: 'traffic_sign\nconf 91%',
    }
    const raw: RenderPanoramaDetectionBox = {
      sourceId: 'source-model-a',
      observationId: 'det-1',
      layerName: 'YOLO · model-a.pt',
      properties: {
        det_id: 'det-1',
        model_nm: 'model-a.pt',
        img_name: 'frame-12.jpg',
        class_nm: 'traffic_sign',
      },
      color: '#ffb84d',
      selected: false,
      detectionBox,
    }
    const representative: RenderPanoramaOverlayPoint = {
      feature_id: 'feature-7',
      layerId: 'layer-1',
      layerName: 'Detected signs',
      u: 0.45,
      v: 0.4,
      depth: 12,
      dataset_position: [1, 2, 3],
      properties: {
        det_id: 'DET-1',
        model_nm: 'model-a.pt',
        img_name: 'frame-12.jpg',
        class_nm: 'traffic_sign',
        asset_id: 'asset-7',
      },
      color: '#22c55e',
      selected: false,
      detectionBox: { ...detectionBox, left: 400.0001 },
    }
    const unrelated = {
      ...raw,
      observationId: 'det-2',
      properties: { ...raw.properties, det_id: 'det-2' },
    }

    const reconciled = reconcilePanoramaDetectionBoxes(
      [raw, unrelated],
      [representative],
    )
    expect(reconciled[0]).toMatchObject({
      layerId: 'layer-1',
      layerName: 'Detected signs',
      featureId: 'feature-7',
      tooltipLayerColor: '#22c55e',
      properties: { asset_id: 'asset-7' },
    })
    expect(reconciled[1].layerId).toBeUndefined()
    expect(reconciled[1].featureId).toBeUndefined()
    expect(panoramaOverlayAtUv([], 0.45, 0.4, reconciled)).toMatchObject({
      layerId: 'layer-1',
      featureId: 'feature-7',
      color: '#22c55e',
    })
  })
})

describe('PanoramaView frame navigation', () => {
  it('shows a ready pole-base proposal only on its calibrated source frame', async () => {
    overlayWorkspaceMock.current = {
      poleBaseProposal: {
        status: 'ready',
        target: { kind: 'pole-base-create', layerId: 'poles', continuous: false },
        frameId: FRAME.id,
        seed: [10, 0, 2],
        result: {
          status: 'auto',
          base_position: [10, 0, 0],
        },
      },
    }
    const { rerender } = render(
      <PanoramaView
        datasetId="dataset-1"
        frame={FRAME}
        demoMode={false}
        poleBaseMarkerColor="#ff00aa"
        poleBaseMarkerSizeM={0.12}
      />,
    )
    const stage = screen.getByRole('region', { name: '파노라마 뷰어' })

    await waitFor(() => expect(stage).toHaveAttribute('data-pole-base-preview', 'true'))
    expect(api.panoramaProjectionMetadata).toHaveBeenCalledWith(
      'dataset-1',
      FRAME.id,
      expect.any(AbortSignal),
    )
    expect(stage).toHaveAttribute('data-pole-base-marker-color', '#ff00aa')
    expect(stage).toHaveAttribute('data-pole-base-marker-size-m', '0.12')
    expect(screen.getByText('임시 바닥점')).toBeInTheDocument()

    rerender(
      <PanoramaView
        datasetId="dataset-1"
        frame={NEXT_FRAME}
        demoMode={false}
        poleBaseMarkerColor="#ff00aa"
        poleBaseMarkerSizeM={0.12}
      />,
    )
    expect(stage).toHaveAttribute('data-pole-base-preview', 'false')
    expect(screen.queryByText('임시 바닥점')).not.toBeInTheDocument()
  })

  it('removes the directional side controls and bottom direction action', () => {
    render(
      <PanoramaView
        datasetId="dataset-1"
        frame={FRAME}
        demoMode={false}
        onPreviousFrame={vi.fn()}
        onNextFrame={vi.fn()}
      />,
    )

    expect(screen.queryByRole('button', { name: '이전 프레임으로 이동' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '다음 프레임으로 이동' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', {
      name: '바라보는 방향의 인접 프레임으로 이동',
    })).not.toBeInTheDocument()
  })

  it('moves on a short blank-scene click, shows its target hint, and ignores a drag', () => {
    class PointerEventMock extends MouseEvent {
      readonly isPrimary: boolean
      readonly pointerId: number
      readonly pointerType: string

      constructor(type: string, init: PointerEventInit = {}) {
        super(type, init)
        this.isPrimary = init.isPrimary ?? true
        this.pointerId = init.pointerId ?? 1
        this.pointerType = init.pointerType ?? 'mouse'
      }
    }
    vi.stubGlobal('PointerEvent', PointerEventMock)
    const previous = {
      ...FRAME,
      id: 'frame-11',
      index: 11,
      coordinate: { lon: 126.978, lat: 37.5655 },
    }
    const forward = {
      ...NEXT_FRAME,
      coordinate: { lon: 126.978, lat: 37.5675 },
    }
    const current = { ...FRAME, heading: 0 }
    const onFrameChange = vi.fn()
    vi.spyOn(THREE.Raycaster.prototype, 'setFromCamera').mockImplementation(function (
      this: THREE.Raycaster,
    ) {
      this.ray.direction.set(-1, 0, 0)
    })
    vi.spyOn(THREE.Raycaster.prototype, 'intersectObject').mockReturnValue([{
      uv: new THREE.Vector2(0.5, 0.5),
    }] as never)

    const { container } = render(
      <PanoramaView
        datasetId="dataset-1"
        frame={current}
        frames={[previous, current, forward]}
        onFrameChange={onFrameChange}
        demoMode
      />,
    )
    const stage = screen.getByRole('region', { name: '파노라마 뷰어' })
    const canvas = container.querySelector<HTMLCanvasElement>('.panorama-canvas')
    expect(canvas).not.toBeNull()
    vi.spyOn(canvas!, 'getBoundingClientRect').mockReturnValue({
      bottom: 500,
      height: 500,
      left: 0,
      right: 1000,
      top: 0,
      width: 1000,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    })

    fireEvent.pointerMove(canvas!, { clientX: 500, clientY: 250 })
    const hoverFrame = vi.mocked(window.requestAnimationFrame).mock.calls.at(-1)?.[0]
    act(() => hoverFrame?.(0))
    expect(container.querySelector('.panorama-scene-navigation-hint')).not.toBeInTheDocument()

    fireEvent.pointerDown(canvas!, { button: 0, clientX: 500, clientY: 250 })
    fireEvent.pointerUp(canvas!, { button: 0, clientX: 503, clientY: 254 })
    expect(onFrameChange).toHaveBeenCalledWith(forward)
    expect(container.querySelector('.panorama-navigation-pulse')).toHaveAttribute(
      'data-target-frame-index',
      '13',
    )
    expect(screen.getByRole('status')).toHaveTextContent('Frame 14로 이동합니다')

    onFrameChange.mockClear()
    fireEvent.pointerDown(canvas!, { button: 0, clientX: 500, clientY: 250 })
    fireEvent.pointerMove(canvas!, { clientX: 512, clientY: 250 })
    expect(stage).toHaveClass('dragging')
    fireEvent.pointerUp(canvas!, { button: 0, clientX: 503, clientY: 254 })
    expect(onFrameChange).not.toHaveBeenCalled()
    expect(stage).not.toHaveClass('dragging')
  })

  it('pins a YOLO hit instead of navigating the scene behind it', async () => {
    class PointerEventMock extends MouseEvent {
      readonly isPrimary: boolean
      readonly pointerId: number
      readonly pointerType: string

      constructor(type: string, init: PointerEventInit = {}) {
        super(type, init)
        this.isPrimary = init.isPrimary ?? true
        this.pointerId = init.pointerId ?? 1
        this.pointerType = init.pointerType ?? 'mouse'
      }
    }
    vi.stubGlobal('PointerEvent', PointerEventMock)
    vi.mocked(api.panorama).mockResolvedValueOnce({ kind: 'url', value: '/pano.jpg' })
    vi.mocked(api.frameDetections).mockResolvedValueOnce({
      dataset_id: 'dataset-1',
      frame_id: FRAME.id,
      coordinate_space: 'panorama_equirectangular_pixels',
      projection: 'equirectangular',
      items: [{
        source_id: 'model-a',
        source_name: 'traffic-sign.pt',
        observation_id: 'det-1',
        properties: {
          img_name: FRAME.image_name,
          class_nm: 'traffic_sign',
          conf: 0.91,
          bbox_l: 400,
          bbox_t: 180,
          bbox_r: 600,
          bbox_b: 320,
          pano_w: 1000,
          pano_h: 500,
        },
      }],
      count: 1,
      model_count: 1,
      models: [{ source_id: 'model-a', source_name: 'traffic-sign.pt', count: 1 }],
      truncated: false,
    })
    vi.spyOn(THREE.Raycaster.prototype, 'setFromCamera').mockImplementation(function (
      this: THREE.Raycaster,
    ) {
      this.ray.direction.set(-1, 0, 0)
    })
    vi.spyOn(THREE.Raycaster.prototype, 'intersectObject').mockReturnValue([{
      uv: new THREE.Vector2(0.5, 0.5),
    }] as never)
    const forward = {
      ...NEXT_FRAME,
      coordinate: { lon: 126.978, lat: 37.5675 },
    }
    const current = { ...FRAME, heading: 0 }
    const onFrameChange = vi.fn()
    const { container } = render(
      <PanoramaView
        datasetId="dataset-1"
        frame={current}
        frames={[current, forward]}
        onFrameChange={onFrameChange}
        demoMode={false}
      />,
    )
    const stage = screen.getByRole('region', { name: '파노라마 뷰어' })
    await waitFor(() => {
      expect(container.querySelector('[data-yolo-box-count="1"]')).toBeInTheDocument()
      expect(threeSpies.textureLoads).toHaveBeenCalledWith('/pano.jpg')
      // A scene click is intentionally disabled until TextureLoader has decoded
      // the current frame; merely starting the load is not sufficient.
      expect(stage).toHaveAttribute('data-rendered-frame-key', 'dataset-1:frame-12')
    })
    const canvas = container.querySelector<HTMLCanvasElement>('.panorama-canvas')!
    vi.spyOn(canvas, 'getBoundingClientRect').mockReturnValue({
      bottom: 500,
      height: 500,
      left: 0,
      right: 1000,
      top: 0,
      width: 1000,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    })

    fireEvent.pointerDown(canvas, { button: 0, clientX: 500, clientY: 250 })
    fireEvent.pointerUp(canvas, { button: 0, clientX: 500, clientY: 250 })

    expect(onFrameChange).not.toHaveBeenCalled()
    expect(screen.getByRole('dialog')).toHaveTextContent('traffic_sign')
    expect(container.querySelector('.panorama-scene-navigation-hint')).not.toBeInTheDocument()
  })

  it('shows the reverse-geocoded address while retaining frame coordinates', async () => {
    render(<PanoramaView datasetId="dataset-1" frame={FRAME} demoMode={false} />)
    expect(screen.getByText('37.566500, 126.978000')).toBeInTheDocument()
    await waitFor(() => {
      expect(screen.getByText('서울특별시 중구 세종대로 110')).toBeInTheDocument()
    })
    expect(api.frameAddress).toHaveBeenCalledWith(
      'dataset-1',
      'frame-12',
      expect.any(AbortSignal),
    )
  })

  it('keeps a coordinate-only location pill when no address or direction target exists', async () => {
    vi.mocked(api.frameAddress).mockResolvedValueOnce({
      dataset_id: 'dataset-1',
      frame_id: 'frame-12',
      coordinate: { lon: 126.978, lat: 37.5665 },
      address: null,
      address_type: null,
      zipcode: null,
      source: 'coordinate_fallback',
    })
    const { container } = render(
      <PanoramaView
        datasetId="dataset-1"
        frame={FRAME}
        frames={[FRAME]}
        onFrameChange={vi.fn()}
        demoMode={false}
      />,
    )
    await waitFor(() => {
      expect(container.querySelector('.panorama-location-bar')).toHaveTextContent(
        '37.566500, 126.978000',
      )
    })
    expect(screen.queryByRole('button', {
      name: '바라보는 방향의 인접 프레임으로 이동',
    })).not.toBeInTheDocument()
  })

  it('does not use paginated navigation when no forward coordinate candidate is loaded', () => {
    const previous = {
      ...FRAME,
      id: 'frame-11',
      index: 11,
      coordinate: { lon: 126.978, lat: 37.5655 },
    }
    const onNextFrame = vi.fn()
    render(
      <PanoramaView
        datasetId="dataset-1"
        frame={{ ...FRAME, heading: 0 }}
        frames={[previous, { ...FRAME, heading: 0 }]}
        onFrameChange={vi.fn()}
        onNextFrame={onNextFrame}
        hasNextFrame
        demoMode={false}
      />,
    )
    expect(panoramaSceneNavigationTarget(
      { ...FRAME, heading: 0 },
      [previous, { ...FRAME, heading: 0 }],
      -180,
      -180,
    )).toBeNull()
    expect(onNextFrame).not.toHaveBeenCalled()
  })

  it('uses high quality by default and reloads with the fast quality budget', async () => {
    vi.mocked(api.panorama).mockImplementation((_datasetId, _frameId, width) => (
      Promise.resolve({ kind: 'url', value: `/panorama-${width}.webp` })
    ))
    const { getByRole } = render(
      <PanoramaView datasetId="dataset-1" frame={FRAME} demoMode={false} />,
    )

    await waitFor(() => {
      expect(api.panorama).toHaveBeenNthCalledWith(
        1,
        'dataset-1',
        'frame-12',
        768,
        expect.any(AbortSignal),
      )
      expect(api.panorama).toHaveBeenNthCalledWith(
        2,
        'dataset-1',
        'frame-12',
        4096,
        expect.any(AbortSignal),
      )
    })

    fireEvent.change(getByRole('combobox', { name: '파노라마 화질' }), {
      target: { value: 'fast' },
    })

    await waitFor(() => {
      expect(api.panorama).toHaveBeenLastCalledWith(
        'dataset-1',
        'frame-12',
        1920,
        expect.any(AbortSignal),
      )
    })
    expect(api.panorama).toHaveBeenCalledTimes(3)
  })

  it('uses a non-passive native wheel listener in a popup document and cleans it up', () => {
    const popup = document.createElement('iframe')
    document.body.append(popup)
    const popupWindow = popup.contentWindow as Window & typeof globalThis
    const popupDocument = popup.contentDocument!
    const addListener = vi.spyOn(popupWindow.HTMLElement.prototype, 'addEventListener')
    const removeListener = vi.spyOn(popupWindow.HTMLElement.prototype, 'removeEventListener')
    const popupContainer = popupDocument.createElement('div')
    popupDocument.body.append(popupContainer)

    const { unmount } = render(
      <PanoramaView datasetId="dataset-1" frame={FRAME} demoMode />,
      { container: popupContainer },
    )
    const stage = popupContainer.querySelector<HTMLElement>('[role="region"]')!
    expect(stage.ownerDocument).toBe(popupDocument)
    const addIndex = addListener.mock.calls.findIndex((call, index) => (
      addListener.mock.contexts[index] === stage
      && call[0] === 'wheel'
      && typeof call[2] === 'object'
      && (call[2] as AddEventListenerOptions)?.passive === false
    ))
    expect(addIndex).toBeGreaterThanOrEqual(0)
    const wheelHandler = addListener.mock.calls[addIndex][1]

    const wheel = new popupWindow.WheelEvent('wheel', {
      bubbles: true,
      cancelable: true,
      deltaY: -1,
    })
    let dispatchResult = true
    act(() => {
      dispatchResult = stage.dispatchEvent(wheel)
    })
    expect(dispatchResult).toBe(false)
    expect(wheel.defaultPrevented).toBe(true)
    expect(stage).toHaveAttribute('data-fov', '68')

    unmount()
    const removed = removeListener.mock.calls.some((call, index) => (
      removeListener.mock.contexts[index] === stage
      && call[0] === 'wheel'
      && call[1] === wheelHandler
      && typeof call[2] === 'object'
      && (call[2] as AddEventListenerOptions)?.passive === false
    ))
    expect(removed).toBe(true)
    popup.remove()
  })

  it('preserves yaw, pitch, and zoom while moving to another frame', () => {
    class PointerEventMock extends MouseEvent {
      readonly isPrimary: boolean
      readonly pointerId: number
      readonly pointerType: string

      constructor(type: string, init: PointerEventInit = {}) {
        super(type, init)
        this.isPrimary = init.isPrimary ?? true
        this.pointerId = init.pointerId ?? 1
        this.pointerType = init.pointerType ?? 'mouse'
      }
    }
    vi.stubGlobal('PointerEvent', PointerEventMock)
    const { rerender } = render(
      <PanoramaView datasetId="dataset-1" frame={FRAME} demoMode />,
    )
    const stage = screen.getByRole('region', { name: '파노라마 뷰어' })

    fireEvent.pointerDown(stage, { button: 0, clientX: 400, clientY: 220 })
    fireEvent.pointerMove(stage, { clientX: 500, clientY: 270 })
    fireEvent.pointerUp(stage, { clientX: 500, clientY: 270 })
    fireEvent.wheel(stage, { deltaY: -1 })

    expect(stage).toHaveAttribute('data-yaw', '-192')
    expect(stage).toHaveAttribute('data-pitch', '5')
    expect(stage).toHaveAttribute('data-fov', '68')

    rerender(<PanoramaView datasetId="dataset-1" frame={NEXT_FRAME} demoMode />)

    expect(stage).toHaveAttribute('data-frame-id', 'frame-13')
    expect(stage).toHaveAttribute('data-yaw', '-192')
    expect(stage).toHaveAttribute('data-pitch', '5')
    expect(stage).toHaveAttribute('data-fov', '68')
  })

  it('does not render removed controls at the available frame range boundary', () => {
    const onPreviousFrame = vi.fn()
    const onNextFrame = vi.fn()

    render(
      <PanoramaView
        datasetId="dataset-1"
        frame={FRAME}
        demoMode={false}
        onPreviousFrame={onPreviousFrame}
        onNextFrame={onNextFrame}
        hasPreviousFrame={false}
        hasNextFrame={false}
      />,
    )

    expect(screen.queryByRole('button', { name: '이전 프레임으로 이동' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '다음 프레임으로 이동' })).not.toBeInTheDocument()
    expect(onPreviousFrame).not.toHaveBeenCalled()
    expect(onNextFrame).not.toHaveBeenCalled()
  })
})

describe('PanoramaView media lifecycle', () => {
  it('shows the lightweight texture before upgrading it without blanking the panorama', async () => {
    const preview = deferred<PanoramaResult>()
    const full = deferred<PanoramaResult>()
    vi.mocked(api.panorama).mockImplementation((_datasetId, _frameId, width) => (
      width === 768 ? preview.promise : full.promise
    ))
    const { container } = render(
      <PanoramaView datasetId="dataset-1" frame={FRAME} demoMode={false} />,
    )
    const stage = screen.getByRole('region', { name: '파노라마 뷰어' })

    expect(api.panorama).toHaveBeenCalledWith(
      'dataset-1',
      'frame-12',
      768,
      expect.any(AbortSignal),
    )
    await act(async () => {
      preview.resolve({ kind: 'url', value: '/panorama-preview.webp' })
      await preview.promise
    })
    await waitFor(() => {
      expect(threeSpies.textureLoads).toHaveBeenCalledWith('/panorama-preview.webp')
      expect(api.panorama).toHaveBeenCalledWith(
        'dataset-1',
        'frame-12',
        4096,
        expect.any(AbortSignal),
      )
    })
    expect(stage).toHaveAttribute('data-media-stage', 'enhancing')
    expect(screen.getByRole('status')).toHaveTextContent('고화질로 선명하게 전환 중')

    await act(async () => {
      full.resolve({ kind: 'url', value: '/panorama-full.webp' })
      await full.promise
    })
    await waitFor(() => {
      expect(threeSpies.textureLoads).toHaveBeenCalledWith('/panorama-full.webp')
      expect(stage).toHaveAttribute('data-media-stage', 'ready')
    })
    expect(threeSpies.textureDisposed).toHaveBeenCalledTimes(1)
    expect(container.querySelector('.viewer-loading')).not.toBeInTheDocument()
  })

  it('loads and renders frame YOLO boxes without any SHP layer', async () => {
    vi.mocked(api.frameDetections).mockResolvedValue({
      dataset_id: 'dataset-1',
      frame_id: 'frame-12',
      coordinate_space: 'panorama_equirectangular_pixels',
      projection: 'equirectangular',
      items: [{
        source_id: 'det-src_model-a',
        source_name: 'traffic-sign.pt',
        observation_id: 'det-1',
        properties: {
          img_name: 'frame-12.jpg',
          class_nm: 'traffic_sign',
          bbox_l: 400,
          bbox_t: 150,
          bbox_r: 500,
          bbox_b: 250,
          pano_w: 1000,
          pano_h: 500,
        },
      }],
      count: 1,
      model_count: 1,
      models: [{ source_id: 'det-src_model-a', source_name: 'traffic-sign.pt', count: 1 }],
      truncated: false,
    })

    const { container } = render(
      <PanoramaView datasetId="dataset-1" frame={FRAME} demoMode={false} />,
    )

    await waitFor(() => {
      expect(api.frameDetections).toHaveBeenCalledWith(
        'dataset-1',
        'frame-12',
        expect.any(AbortSignal),
      )
      expect(container.querySelector('[data-yolo-box-count="1"]')).toBeInTheDocument()
    })
    expect(threeSpies.canvasFillText).not.toHaveBeenCalled()
  })

  it('filters boxes and counts by model while preserving choices across frames', async () => {
    const detection = (sourceId: string, modelId: string, sourceName: string, left: number) => ({
      source_id: sourceId,
      model_id: modelId,
      source_name: sourceName,
      observation_id: `${sourceId}-det`,
      properties: {
        img_name: 'frame-12.jpg',
        class_nm: 'traffic_sign',
        conf: 0.91,
        bbox_l: left,
        bbox_t: 150,
        bbox_r: left + 100,
        bbox_b: 250,
        pano_w: 1000,
        pano_h: 500,
      },
    })
    vi.mocked(api.frameDetections).mockImplementation((_datasetId, frameId) => {
      const suffix = frameId === FRAME.id ? 'first' : 'next'
      const items = [
        detection(`source-a-${suffix}`, 'model-id-a', 'model-a.pt', 100),
        detection(`source-b-${suffix}`, 'model-id-b', 'model-b.pt', 300),
      ]
      return Promise.resolve({
        dataset_id: 'dataset-1',
        frame_id: frameId,
        coordinate_space: 'panorama_equirectangular_pixels',
        projection: 'equirectangular',
        items,
        models: [
          { source_id: `source-a-${suffix}`, model_id: 'model-id-a', source_name: 'model-a.pt', count: 1 },
          { source_id: `source-b-${suffix}`, model_id: 'model-id-b', source_name: 'model-b.pt', count: 1 },
          { source_id: `source-empty-${suffix}`, model_id: 'model-id-empty', source_name: 'model-empty.pt', count: 0 },
        ],
        count: 2,
        model_count: 3,
        truncated: false,
      })
    })

    const { container, rerender } = render(
      <PanoramaView datasetId="dataset-1" frame={FRAME} demoMode={false} />,
    )
    await waitFor(() => {
      expect(container.querySelector('[data-yolo-box-count="2"]')).toBeInTheDocument()
      expect(screen.getByLabelText('model-empty.pt 검출 표시')).toBeChecked()
    })

    fireEvent.click(screen.getByLabelText('model-a.pt 검출 표시'))
    await waitFor(() => {
      expect(container.querySelector('[data-yolo-box-count="1"]')).toBeInTheDocument()
    })
    fireEvent.click(screen.getByLabelText('model-b.pt 검출 표시'))
    fireEvent.click(screen.getByLabelText('model-empty.pt 검출 표시'))
    await waitFor(() => {
      expect(container.querySelector('[data-yolo-box-count="0"]')).toBeInTheDocument()
      expect(screen.getByText('0/3')).toBeInTheDocument()
    })

    rerender(<PanoramaView datasetId="dataset-1" frame={NEXT_FRAME} demoMode={false} />)
    await waitFor(() => expect(api.frameDetections).toHaveBeenCalledTimes(2))
    expect(screen.getByLabelText('model-a.pt 검출 표시')).not.toBeChecked()
    expect(container.querySelector('[data-yolo-box-count="0"]')).toBeInTheDocument()

    fireEvent.click(screen.getByLabelText('model-a.pt 검출 표시'))
    await waitFor(() => {
      expect(container.querySelector('[data-yolo-box-count="1"]')).toBeInTheDocument()
    })
  })

  it('reloads detections for the same frame when a run completes', async () => {
    const { rerender } = render(
      <PanoramaView
        datasetId="dataset-1"
        frame={FRAME}
        demoMode={false}
        detectionRevisionKey="run-1:running"
      />,
    )

    await waitFor(() => expect(api.frameDetections).toHaveBeenCalledTimes(1))
    rerender(
      <PanoramaView
        datasetId="dataset-1"
        frame={FRAME}
        demoMode={false}
        detectionRevisionKey="run-1:completed"
      />,
    )

    await waitFor(() => expect(api.frameDetections).toHaveBeenCalledTimes(2))
    expect(vi.mocked(api.frameDetections).mock.calls[1]?.slice(0, 2)).toEqual([
      'dataset-1',
      'frame-12',
    ])
  })

  it('keeps one renderer and panorama texture while overlay data and image opacity change', async () => {
    vi.mocked(api.panorama).mockResolvedValue({ kind: 'url', value: '/panorama/frame-12.webp' })
    vi.mocked(api.panoramaPoints).mockResolvedValue(mmsoPayload())

    const { container, rerender } = render(
      <PanoramaView
        datasetId="dataset-1"
        frame={FRAME}
        demoMode={false}
        pointOverlayEnabled
        panoramaOpacity={0.65}
      />,
    )

    await waitFor(() => {
      expect(container.querySelector('[data-point-count="1"]')).toBeInTheDocument()
      expect(threeSpies.textureLoads).toHaveBeenCalledWith('/panorama/frame-12.webp')
    })
    expect(threeSpies.rendererConstructed).toHaveBeenCalledTimes(1)
    expect(threeSpies.textureLoads).toHaveBeenCalledTimes(1)

    rerender(
      <PanoramaView
        datasetId="dataset-1"
        frame={FRAME}
        demoMode={false}
        pointOverlayEnabled
        panoramaOpacity={0.2}
      />,
    )
    rerender(
      <PanoramaView
        datasetId="dataset-1"
        frame={FRAME}
        demoMode={false}
        pointOverlayEnabled={false}
        panoramaOpacity={0.2}
      />,
    )
    rerender(
      <PanoramaView
        datasetId="dataset-1"
        frame={FRAME}
        demoMode={false}
        pointOverlayEnabled
        panoramaOpacity={0.2}
      />,
    )

    await waitFor(() => expect(api.panoramaPoints).toHaveBeenCalledTimes(2))
    expect(container.querySelector('[data-panorama-opacity="0.2"]')).toBeInTheDocument()
    expect(threeSpies.rendererConstructed).toHaveBeenCalledTimes(1)
    expect(threeSpies.textureLoads).toHaveBeenCalledTimes(1)
  })

  it('ignores a previous frame response and revokes only the active blob URL', async () => {
    const first = deferred<PanoramaResult>()
    const second = deferred<PanoramaResult>()
    vi.mocked(api.panorama).mockImplementation((_datasetId, frameId, width) => {
      if (frameId === FRAME.id) return first.promise
      if (width === 768) return second.promise
      return Promise.resolve({ kind: 'url', value: '/new-frame-full.webp' })
    })
    const createObjectUrl = vi.mocked(URL.createObjectURL).mockImplementation(
      (blob) => `blob:${blob instanceof Blob ? blob.size : 0}`,
    )
    const revokeObjectUrl = vi.mocked(URL.revokeObjectURL)

    const { rerender, unmount } = render(
      <PanoramaView datasetId="dataset-1" frame={FRAME} demoMode={false} />,
    )
    await waitFor(() => expect(api.panorama).toHaveBeenCalledTimes(1))
    const firstSignal = vi.mocked(api.panorama).mock.calls[0][3]

    rerender(<PanoramaView datasetId="dataset-1" frame={NEXT_FRAME} demoMode={false} />)
    await waitFor(() => expect(api.panorama).toHaveBeenCalledTimes(2))
    expect(firstSignal?.aborted).toBe(true)

    await act(async () => {
      first.resolve({ kind: 'blob', value: new Blob(['old-frame']) })
      await first.promise
    })
    expect(createObjectUrl).not.toHaveBeenCalled()
    expect(threeSpies.textureLoads).not.toHaveBeenCalled()

    await act(async () => {
      second.resolve({ kind: 'blob', value: new Blob(['new-frame']) })
      await second.promise
    })
    await waitFor(() => expect(threeSpies.textureLoads).toHaveBeenCalledWith('blob:9'))
    expect(createObjectUrl).toHaveBeenCalledTimes(1)
    await waitFor(() => {
      expect(threeSpies.textureLoads).toHaveBeenCalledWith('/new-frame-full.webp')
    })

    unmount()
    expect(revokeObjectUrl).toHaveBeenCalledWith('blob:9')
  })

  it('discards a late decoded texture when the active frame has already changed and failed', async () => {
    threeSpies.manualTextureLoads = true
    vi.mocked(api.panorama).mockImplementation((_datasetId, frameId, width) => {
      if (frameId === FRAME.id && width === 768) {
        return Promise.resolve({ kind: 'url', value: '/frame-a-preview.webp' })
      }
      if (frameId === FRAME.id) return new Promise(() => undefined)
      return Promise.reject(new Error('B preview failed'))
    })

    const { rerender } = render(
      <PanoramaView datasetId="dataset-1" frame={FRAME} demoMode={false} />,
    )
    const stage = screen.getByRole('region', { name: '파노라마 뷰어' })
    await waitFor(() => {
      expect(threeSpies.pendingTextureLoads.some(
        (request) => request.source === '/frame-a-preview.webp',
      )).toBe(true)
    })
    const lateFrameA = threeSpies.pendingTextureLoads.find(
      (request) => request.source === '/frame-a-preview.webp',
    )!

    rerender(<PanoramaView datasetId="dataset-1" frame={NEXT_FRAME} demoMode={false} />)
    await waitFor(() => {
      expect(screen.getByText('B preview failed')).toBeInTheDocument()
      expect(stage).toHaveAttribute('data-frame-id', NEXT_FRAME.id)
    })

    act(() => lateFrameA.succeed())

    expect(threeSpies.textureDisposed).toHaveBeenCalledTimes(1)
    expect(stage).toHaveAttribute('data-rendered-frame-key', '')
    expect(screen.getByText('B preview failed')).toBeInTheDocument()
  })
})
