import { act, cleanup, fireEvent, render, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../lib/api'
import type { Frame } from '../types'
import PanoramaView, {
  nearestPanoramaPointIndex,
  panoramaForwardYaw,
  panoramaRequestWidth,
} from './PanoramaView'

const threeSpies = vi.hoisted(() => ({
  rendererConstructed: vi.fn(),
  rendererDisposed: vi.fn(),
  textureLoads: vi.fn(),
  textureDisposed: vi.fn(),
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
  },
}))

const FRAME: Frame = {
  id: 'frame-12',
  index: 12,
  track_id: 'track-1',
  timestamp: '2026-08-03T09:30:00.000Z',
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
  vi.mocked(api.panorama).mockReset().mockImplementation(() => new Promise(() => undefined))
  vi.mocked(api.panoramaPoints).mockReset().mockImplementation(() => new Promise(() => undefined))
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
