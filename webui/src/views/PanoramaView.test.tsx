import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../lib/api'
import type { Frame } from '../types'
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
  panoramaOverlayAtUv,
  panoramaRequestWidth,
  reconcilePanoramaDetectionBoxes,
  type RenderPanoramaDetectionBox,
  type RenderPanoramaOverlayPoint,
} from './PanoramaView'

const threeSpies = vi.hoisted(() => ({
  rendererConstructed: vi.fn(),
  rendererDisposed: vi.fn(),
  textureLoads: vi.fn(),
  textureDisposed: vi.fn(),
  canvasFillText: vi.fn(),
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
    load(source: string, onLoad?: (texture: FakeTexture) => void) {
      threeSpies.textureLoads(source)
      const texture = new FakeTexture()
      queueMicrotask(() => onLoad?.(texture))
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
  threeSpies.rendererConstructed.mockClear()
  threeSpies.rendererDisposed.mockClear()
  threeSpies.textureLoads.mockClear()
  threeSpies.textureDisposed.mockClear()
  threeSpies.canvasFillText.mockClear()
  vi.mocked(api.panorama).mockReset().mockImplementation(() => new Promise(() => undefined))
  vi.mocked(api.panoramaPoints).mockReset().mockImplementation(() => new Promise(() => undefined))
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
})

describe('panoramaForwardYaw', () => {
  it('resets to image-space forward with the operator correction', () => {
    expect(panoramaForwardYaw(0)).toBe(-180)
    expect(panoramaForwardYaw(7.5)).toBe(-172.5)
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
  it('navigates with the directional controls', () => {
    const onPreviousFrame = vi.fn()
    const onNextFrame = vi.fn()

    const { getByRole } = render(
      <PanoramaView
        datasetId="dataset-1"
        frame={FRAME}
        demoMode={false}
        onPreviousFrame={onPreviousFrame}
        onNextFrame={onNextFrame}
      />,
    )

    fireEvent.click(getByRole('button', { name: '이전 프레임으로 이동' }))
    fireEvent.click(getByRole('button', { name: '다음 프레임으로 이동' }))

    expect(onPreviousFrame).toHaveBeenCalledTimes(1)
    expect(onNextFrame).toHaveBeenCalledTimes(1)
  })

  it('moves to the adjacent frame in the current viewing direction', () => {
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
    const onFrameChange = vi.fn()
    const { container } = render(
      <PanoramaView
        datasetId="dataset-1"
        frame={{ ...FRAME, heading: 0 }}
        frames={[previous, { ...FRAME, heading: 0 }, forward]}
        onFrameChange={onFrameChange}
        demoMode={false}
      />,
    )

    fireEvent.click(screen.getByRole('button', {
      name: '바라보는 방향의 인접 프레임으로 이동',
    }))
    expect(container.querySelector('.panorama-location-bar')).toContainElement(
      screen.getByRole('button', { name: '바라보는 방향의 인접 프레임으로 이동' }),
    )
    expect(onFrameChange).toHaveBeenCalledWith(forward)
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

  it('uses paginated next-frame navigation when the forward neighbor is not loaded yet', () => {
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
    fireEvent.click(screen.getByRole('button', {
      name: '바라보는 방향의 인접 프레임으로 이동',
    }))
    expect(onNextFrame).toHaveBeenCalledOnce()
  })

  it('uses high quality by default and reloads with the fast quality budget', async () => {
    const { getByRole } = render(
      <PanoramaView datasetId="dataset-1" frame={FRAME} demoMode={false} />,
    )

    await waitFor(() => {
      expect(api.panorama).toHaveBeenLastCalledWith(
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
  })

  it('does not navigate beyond the available frame range', () => {
    const onPreviousFrame = vi.fn()
    const onNextFrame = vi.fn()

    const { getByRole } = render(
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

    const previous = getByRole('button', { name: '이전 프레임으로 이동' })
    const next = getByRole('button', { name: '다음 프레임으로 이동' })
    expect(previous).toBeDisabled()
    expect(next).toBeDisabled()
    fireEvent.click(previous)
    fireEvent.click(next)

    expect(onPreviousFrame).not.toHaveBeenCalled()
    expect(onNextFrame).not.toHaveBeenCalled()
  })
})

describe('PanoramaView media lifecycle', () => {
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
    vi.mocked(api.panorama).mockImplementation((_datasetId, frameId) => {
      return frameId === FRAME.id ? first.promise : second.promise
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

    unmount()
    expect(revokeObjectUrl).toHaveBeenCalledWith('blob:9')
  })
})
