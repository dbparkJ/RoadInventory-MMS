import {
  ChevronLeft,
  ChevronRight,
  Crosshair,
  Expand,
  LoaderCircle,
  Maximize2,
  MapPin,
  Minus,
  MousePointer2,
  Plus,
  RefreshCcw,
  ScanLine,
} from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import * as THREE from 'three'
import demoPanorama from '../assets/demo-panorama.svg'
import { useOptionalOverlayWorkspace } from '../components/OverlayContext'
import { api, ApiError } from '../lib/api'
import { createDemoPanoramaPoints, parseMmso } from '../lib/mmso'
import type { PanoramaQuality } from '../lib/userSettings'
import type { Frame, PanoramaOverlayFeature, PanoramaPointPayload } from '../types'

export type { PanoramaQuality } from '../lib/userSettings'

export function panoramaForwardYaw(offsetDeg: number): number {
  // Leica equirectangular deliveries keep the vehicle forward direction at
  // the texture centre. Global GNSS heading belongs on the map, not in this
  // image-space reset, otherwise every frame turn makes the viewer look aside.
  return -180 + offsetDeg
}

export function panoramaRequestWidth(
  containerWidth: number,
  devicePixelRatio: number,
  quality: PanoramaQuality,
): number {
  const safeContainerWidth = containerWidth > 0 ? containerWidth : 1280
  const safePixelRatio = Math.min(2, Math.max(1, devicePixelRatio || 1))
  if (quality === 'ultra') return 8192
  // Fixed 4K derivatives stay cache-friendly and remain sharp when an overlay
  // is enlarged, detached to a second monitor, or switched to full screen.
  if (quality === 'high') return 4096
  const maximumWidth = 2048
  const minimumWidth = 960
  // Fast mode follows the current pane size to minimize transfer and decode time.
  const panoramaScale = 1.5
  return Math.min(
    maximumWidth,
    Math.max(minimumWidth, Math.round(safeContainerWidth * safePixelRatio * panoramaScale)),
  )
}

function createPanoramaPointTexture(
  ownerDocument: Document,
  payload: PanoramaPointPayload,
): THREE.CanvasTexture {
  const width = 2048
  const height = 1024
  const canvas = ownerDocument.createElement('canvas')
  canvas.width = width
  canvas.height = height
  const context = canvas.getContext('2d')
  if (!context) throw new Error('파노라마 포인트 캔버스를 만들 수 없습니다.')
  const image = context.createImageData(width, height)
  const pixels = image.data

  for (let index = 0; index < payload.pointCount; index += 1) {
    const offset = index * 3
    const u = payload.coordinates[offset]
    const v = payload.coordinates[offset + 1]
    const distance = payload.coordinates[offset + 2]
    if (!Number.isFinite(u) || !Number.isFinite(v) || !Number.isFinite(distance)) continue
    const centerX = Math.round((((u % 1) + 1) % 1) * (width - 1))
    const centerY = Math.round(Math.min(1, Math.max(0, v)) * (height - 1))
    const radius = distance < 12 ? 2 : 1
    const red = payload.colors?.[offset] ?? 62
    const green = payload.colors?.[offset + 1] ?? 226
    const blue = payload.colors?.[offset + 2] ?? 189
    for (let dy = -radius; dy <= radius; dy += 1) {
      const y = centerY + dy
      if (y < 0 || y >= height) continue
      for (let dx = -radius; dx <= radius; dx += 1) {
        if (dx * dx + dy * dy > radius * radius + 1) continue
        const x = (centerX + dx + width) % width
        const pixelOffset = (y * width + x) * 4
        pixels[pixelOffset] = red
        pixels[pixelOffset + 1] = green
        pixels[pixelOffset + 2] = blue
        pixels[pixelOffset + 3] = 255
      }
    }
  }
  context.putImageData(image, 0, 0)
  const texture = new THREE.CanvasTexture(canvas)
  texture.colorSpace = THREE.SRGBColorSpace
  texture.wrapS = THREE.RepeatWrapping
  texture.minFilter = THREE.LinearFilter
  texture.magFilter = THREE.LinearFilter
  texture.needsUpdate = true
  return texture
}

interface RenderPanoramaOverlayPoint extends PanoramaOverlayFeature {
  layerId: string
  color: string
  selected: boolean
}

function createPanoramaOverlayTexture(
  ownerDocument: Document,
  points: RenderPanoramaOverlayPoint[],
): THREE.CanvasTexture {
  const width = 2048
  const height = 1024
  const canvas = ownerDocument.createElement('canvas')
  canvas.width = width
  canvas.height = height
  const context = canvas.getContext('2d')
  if (!context) throw new Error('SHP 오버레이 캔버스를 만들 수 없습니다.')

  points.forEach((point) => {
    const x = (((point.u % 1) + 1) % 1) * width
    const y = Math.min(1, Math.max(0, point.v)) * height
    const radius = point.selected ? 11 : point.depth < 20 ? 8 : 6
    context.beginPath()
    context.arc(x, y, radius + 3, 0, Math.PI * 2)
    context.fillStyle = point.selected ? '#ffffff' : 'rgba(7, 17, 31, 0.9)'
    context.fill()
    context.beginPath()
    context.arc(x, y, radius, 0, Math.PI * 2)
    context.fillStyle = point.color
    context.fill()
    context.lineWidth = 2
    context.strokeStyle = '#ffffff'
    context.stroke()
  })

  const texture = new THREE.CanvasTexture(canvas)
  texture.colorSpace = THREE.SRGBColorSpace
  texture.wrapS = THREE.RepeatWrapping
  texture.minFilter = THREE.LinearFilter
  texture.magFilter = THREE.LinearFilter
  texture.needsUpdate = true
  return texture
}

function wrappedUVDistanceSquared(u: number, v: number, targetU: number, targetV: number): number {
  const directU = Math.abs(u - targetU)
  const wrappedU = Math.min(directU, 1 - Math.min(1, directU))
  return wrappedU * wrappedU + (v - targetV) * (v - targetV)
}

export function nearestPanoramaPointIndex(
  u: number,
  v: number,
  coordinates: Float32Array,
  pointCount: number,
  maximumDistance = 0.03,
): number | null {
  let nearestIndex: number | null = null
  let nearestDistance = maximumDistance * maximumDistance
  for (let index = 0; index < pointCount; index += 1) {
    const offset = index * 3
    const candidateU = coordinates[offset]
    const candidateV = coordinates[offset + 1]
    if (!Number.isFinite(candidateU) || !Number.isFinite(candidateV)) continue
    const distance = wrappedUVDistanceSquared(u, v, candidateU, candidateV)
    if (distance <= nearestDistance) {
      nearestDistance = distance
      nearestIndex = index
    }
  }
  return nearestIndex
}

interface PanoramaRuntime {
  scene: THREE.Scene
  camera: THREE.PerspectiveCamera
  renderer: THREE.WebGLRenderer
  panoramaMesh: THREE.Mesh
  panoramaMaterial: THREE.MeshBasicMaterial
}

export default function PanoramaView({
  datasetId,
  frame,
  demoMode,
  onPreviousFrame,
  onNextFrame,
  hasPreviousFrame = true,
  hasNextFrame = true,
  forwardOffsetDeg = 0,
  quality: controlledQuality,
  onQualityChange,
  pointOverlayEnabled: controlledPointOverlayEnabled,
  panoramaOpacity: controlledPanoramaOpacity,
  onPointOverlayEnabledChange,
  onPanoramaOpacityChange,
}: {
  datasetId: string
  frame: Frame | null
  demoMode: boolean
  onPreviousFrame?: () => void
  onNextFrame?: () => void
  hasPreviousFrame?: boolean
  hasNextFrame?: boolean
  forwardOffsetDeg?: number
  quality?: PanoramaQuality
  onQualityChange?: (quality: PanoramaQuality) => void
  pointOverlayEnabled?: boolean
  panoramaOpacity?: number
  onPointOverlayEnabledChange?: (enabled: boolean) => void
  onPanoramaOpacityChange?: (opacity: number) => void
}) {
  const overlay = useOptionalOverlayWorkspace()
  const stageRef = useRef<HTMLDivElement>(null)
  const [source, setSource] = useState<string | null>(demoMode ? demoPanorama : null)
  const [loading, setLoading] = useState(!demoMode)
  const [error, setError] = useState<string | null>(null)
  const [fov, setFov] = useState(72)
  const [yaw, setYaw] = useState(0)
  const [pitch, setPitch] = useState(0)
  const [localQuality, setLocalQuality] = useState<PanoramaQuality>('high')
  const [localPointOverlayEnabled, setLocalPointOverlayEnabled] = useState(false)
  const [localPanoramaOpacity, setLocalPanoramaOpacity] = useState(0.65)
  const [pointPayload, setPointPayload] = useState<PanoramaPointPayload | null>(null)
  const [pointLoading, setPointLoading] = useState(false)
  const [pointIndexing, setPointIndexing] = useState(false)
  const [pointError, setPointError] = useState<string | null>(null)
  const [pointReloadKey, setPointReloadKey] = useState(0)
  const [overlayProjection, setOverlayProjection] = useState<RenderPanoramaOverlayPoint[]>([])
  const [overlayLoading, setOverlayLoading] = useState(false)
  const [pickFeedback, setPickFeedback] = useState<string | null>(null)
  const runtimeRef = useRef<PanoramaRuntime | null>(null)
  const quality = controlledQuality ?? localQuality
  const pointOverlayEnabled = controlledPointOverlayEnabled ?? localPointOverlayEnabled
  const panoramaOpacity = controlledPanoramaOpacity ?? localPanoramaOpacity
  const pointPayloadRequired = pointOverlayEnabled || Boolean(overlay?.pickMode)
  const hasVisualOverlay = pointOverlayEnabled || overlayProjection.length > 0
  const effectivePanoramaOpacity = hasVisualOverlay ? panoramaOpacity : 1
  const panoramaOpacityRef = useRef(effectivePanoramaOpacity)
  panoramaOpacityRef.current = effectivePanoramaOpacity
  const viewRef = useRef({ fov, yaw, pitch })
  viewRef.current = { fov, yaw, pitch }
  const [dragStart, setDragStart] = useState<{ x: number; y: number; yaw: number; pitch: number } | null>(
    null,
  )
  const [reloadKey, setReloadKey] = useState(0)
  const overlayActionsRef = useRef({
    pickMode: false,
    selectFeature: (_selection: { layerId: string; featureId: string | number } | null) => {},
    applyPickedCoordinate: async (
      _coordinates: [number, number, number?],
      _coordinateSpace: 'dataset',
    ) => {},
  })
  overlayActionsRef.current = {
    pickMode: overlay?.pickMode ?? false,
    selectFeature: overlay?.selectFeature ?? (() => {}),
    applyPickedCoordinate: overlay?.applyPickedCoordinate ?? (async () => {}),
  }
  const visibleOverlayLayers = useMemo(
    () =>
      (overlay?.layers ?? []).filter((layer) => overlay?.visibleLayerIds.has(layer.id)),
    [overlay?.layers, overlay?.visibleLayerIds],
  )
  const selectedOverlayKey = overlay?.selected
    ? `${overlay.selected.layerId}:${overlay.selected.featureId}`
    : ''
  const overlayLayerColor = overlay?.layerColor
  const overlayProjectionRevisionKey = visibleOverlayLayers
    .map((layer) => `${layer.id}:${overlay?.features[layer.id]?.dataset?.revision ?? layer.revision}`)
    .join('|')

  useEffect(() => {
    setFov(72)
    setYaw(panoramaForwardYaw(forwardOffsetDeg))
    setPitch(0)
    setError(null)
  }, [forwardOffsetDeg, frame?.id])

  useEffect(() => {
    const host = stageRef.current
    if (!host) return

    const scene = new THREE.Scene()
    scene.background = new THREE.Color(0x07111f)
    const camera = new THREE.PerspectiveCamera(
      viewRef.current.fov,
      host.clientWidth / host.clientHeight,
      0.05,
      100,
    )
    let renderer: THREE.WebGLRenderer
    try {
      renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: 'high-performance' })
    } catch {
      setError('이 브라우저에서 WebGL 파노라마 뷰어를 시작할 수 없습니다.')
      return
    }
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 1.6))
    renderer.setSize(host.clientWidth, host.clientHeight)
    renderer.outputColorSpace = THREE.SRGBColorSpace
    renderer.domElement.className = 'panorama-canvas'
    host.prepend(renderer.domElement)

    const panoramaGeometry = new THREE.SphereGeometry(10, 64, 40)
    panoramaGeometry.scale(-1, 1, 1)
    const panoramaMaterial = new THREE.MeshBasicMaterial({
      color: 0xffffff,
      transparent: true,
      opacity: panoramaOpacityRef.current,
    })
    panoramaMaterial.visible = false
    const panoramaMesh = new THREE.Mesh(panoramaGeometry, panoramaMaterial)
    scene.add(panoramaMesh)
    runtimeRef.current = { scene, camera, renderer, panoramaMesh, panoramaMaterial }

    let raf = 0
    const draw = () => {
      const phi = THREE.MathUtils.degToRad(90 - viewRef.current.pitch)
      const theta = THREE.MathUtils.degToRad(viewRef.current.yaw)
      camera.fov = viewRef.current.fov
      camera.updateProjectionMatrix()
      camera.lookAt(
        10 * Math.sin(phi) * Math.cos(theta),
        10 * Math.cos(phi),
        10 * Math.sin(phi) * Math.sin(theta),
      )
      renderer.render(scene, camera)
      raf = requestAnimationFrame(draw)
    }
    draw()

    const observer = new ResizeObserver(() => {
      const width = host.clientWidth
      const height = host.clientHeight
      camera.aspect = width / height
      camera.updateProjectionMatrix()
      renderer.setSize(width, height)
    })
    observer.observe(host)
    return () => {
      cancelAnimationFrame(raf)
      observer.disconnect()
      if (runtimeRef.current?.scene === scene) runtimeRef.current = null
      panoramaGeometry.dispose()
      panoramaMaterial.dispose()
      renderer.dispose()
      renderer.domElement.remove()
    }
  }, [])

  useEffect(() => {
    if (!frame) {
      setSource(null)
      setLoading(false)
      return
    }
    if (demoMode) {
      setSource(demoPanorama)
      setLoading(false)
      return
    }

    const controller = new AbortController()
    let active = true
    let objectUrl: string | undefined
    setLoading(true)
    setSource(null)
    setError(null)
    const containerWidth = stageRef.current?.clientWidth ?? 1280
    // Request a bounded viewport-sized derivative, never the multi-gigapixel source image.
    const width = panoramaRequestWidth(containerWidth, window.devicePixelRatio, quality)
    void api
      .panorama(datasetId, frame.id, width, controller.signal)
      .then((result) => {
        if (!active || controller.signal.aborted) return
        if (result.kind === 'url') {
          setSource(result.value)
        } else {
          const nextObjectUrl = URL.createObjectURL(result.value)
          if (!active || controller.signal.aborted) {
            URL.revokeObjectURL(nextObjectUrl)
            return
          }
          objectUrl = nextObjectUrl
          setSource(nextObjectUrl)
        }
      })
      .catch((reason: unknown) => {
        if (active && !controller.signal.aborted) {
          setError(reason instanceof Error ? reason.message : '파노라마를 불러오지 못했습니다.')
        }
      })
      .finally(() => {
        if (active && !controller.signal.aborted) setLoading(false)
      })
    return () => {
      active = false
      controller.abort()
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [datasetId, demoMode, frame, quality, reloadKey])

  useEffect(() => {
    const runtime = runtimeRef.current
    if (!runtime) return

    runtime.panoramaMaterial.map = null
    runtime.panoramaMaterial.visible = false
    runtime.panoramaMaterial.needsUpdate = true
    if (!source) return

    let active = true
    const texture = new THREE.TextureLoader().load(
      source,
      (readyTexture) => {
        if (!active || runtimeRef.current !== runtime) {
          readyTexture.dispose()
          return
        }
        readyTexture.colorSpace = THREE.SRGBColorSpace
        readyTexture.needsUpdate = true
        runtime.panoramaMaterial.map = readyTexture
        runtime.panoramaMaterial.visible = true
        runtime.panoramaMaterial.needsUpdate = true
      },
      undefined,
      () => {
        if (active && runtimeRef.current === runtime) {
          setError('파노라마 텍스처를 디코딩하지 못했습니다.')
        }
      },
    )
    texture.colorSpace = THREE.SRGBColorSpace

    return () => {
      active = false
      if (runtime.panoramaMaterial.map === texture) {
        runtime.panoramaMaterial.map = null
        runtime.panoramaMaterial.visible = false
        runtime.panoramaMaterial.needsUpdate = true
      }
      texture.dispose()
    }
  }, [source])

  useEffect(() => {
    if (!frame || demoMode || !visibleOverlayLayers.length) {
      setOverlayProjection([])
      setOverlayLoading(false)
      return
    }
    const controller = new AbortController()
    setOverlayProjection([])
    setPickFeedback(null)
    setOverlayLoading(true)
    const loadProjection = async () => {
      const groups: RenderPanoramaOverlayPoint[][] = []
      const errors: unknown[] = []
      let nextIndex = 0
      const worker = async () => {
        while (!controller.signal.aborted) {
          const index = nextIndex
          nextIndex += 1
          const layer = visibleOverlayLayers[index]
          if (!layer) return
          try {
            const response = await api.panoramaOverlayProjection(
              datasetId,
              layer.id,
              frame.id,
              controller.signal,
            )
            const color = overlayLayerColor?.(layer.id) ?? '#ffb84d'
            groups[index] = response.items.map((item) => ({
              ...item,
              layerId: layer.id,
              color,
              selected: false,
            }))
          } catch (reason) {
            if (!controller.signal.aborted) errors.push(reason)
            groups[index] = []
          }
        }
      }
      await Promise.all(
        Array.from({ length: Math.min(4, visibleOverlayLayers.length) }, () => worker()),
      )
      if (controller.signal.aborted) return
      setOverlayProjection(groups.flat())
      if (errors.length) {
        setPickFeedback(
          errors.length === visibleOverlayLayers.length
            ? errors[0] instanceof Error
              ? errors[0].message
              : '파노라마 SHP 위치를 불러오지 못했습니다.'
            : `일부 SHP 레이어(${errors.length}개)를 파노라마에 맞추지 못했습니다.`,
        )
      }
    }
    void loadProjection()
      .finally(() => {
        if (!controller.signal.aborted) setOverlayLoading(false)
      })
    return () => controller.abort()
  }, [
    datasetId,
    demoMode,
    frame,
    overlayLayerColor,
    overlayProjectionRevisionKey,
    visibleOverlayLayers,
  ])

  const renderedOverlayProjection = useMemo(
    () =>
      overlayProjection.map((point) => ({
        ...point,
        selected: `${point.layerId}:${point.feature_id}` === selectedOverlayKey,
      })),
    [overlayProjection, selectedOverlayKey],
  )

  useEffect(() => {
    const runtime = runtimeRef.current
    const host = stageRef.current
    if (!runtime || !host || !renderedOverlayProjection.length) return
    const geometry = new THREE.SphereGeometry(9.92, 64, 40)
    geometry.scale(-1, 1, 1)
    const texture = createPanoramaOverlayTexture(host.ownerDocument, renderedOverlayProjection)
    const material = new THREE.MeshBasicMaterial({
      map: texture,
      transparent: true,
      opacity: 1,
      depthTest: false,
      depthWrite: false,
      toneMapped: false,
    })
    const mesh = new THREE.Mesh(geometry, material)
    mesh.renderOrder = 3
    runtime.scene.add(mesh)
    return () => {
      runtime.scene.remove(mesh)
      geometry.dispose()
      material.dispose()
      texture.dispose()
    }
  }, [renderedOverlayProjection])

  useEffect(() => {
    if (!frame || !pointPayloadRequired) {
      setPointPayload(null)
      setPointLoading(false)
      setPointIndexing(false)
      setPointError(null)
      return
    }
    if (demoMode) {
      setPointPayload(createDemoPanoramaPoints())
      setPointLoading(false)
      setPointIndexing(false)
      setPointError(null)
      return
    }

    const controller = new AbortController()
    let retryTimer: number | undefined
    let attempts = 0
    setPointPayload(null)
    setPointLoading(true)
    setPointIndexing(false)
    setPointError(null)
    const load = async () => {
      try {
        const payload = parseMmso(
          await api.panoramaPoints(datasetId, frame.id, 30_000, 30, controller.signal),
        )
        if (!controller.signal.aborted) {
          setPointPayload(payload)
          setPointLoading(false)
          setPointIndexing(false)
        }
      } catch (reason) {
        if (controller.signal.aborted) return
        if (reason instanceof ApiError && reason.status === 202 && attempts < 8) {
          attempts += 1
          setPointIndexing(true)
          retryTimer = window.setTimeout(load, Math.min(8_000, attempts * 1_200))
          return
        }
        setPointError(
          reason instanceof Error ? reason.message : '파노라마 포인트를 불러오지 못했습니다.',
        )
        setPointLoading(false)
      }
    }
    void load()
    return () => {
      controller.abort()
      if (retryTimer) window.clearTimeout(retryTimer)
    }
  }, [datasetId, demoMode, frame, pointPayloadRequired, pointReloadKey])

  useEffect(() => {
    const runtime = runtimeRef.current
    const host = stageRef.current
    if (!runtime || !host || !pointPayload || !pointOverlayEnabled) return

    const overlayGeometry = new THREE.SphereGeometry(9.96, 64, 40)
    overlayGeometry.scale(-1, 1, 1)
    const overlayTexture = createPanoramaPointTexture(host.ownerDocument, pointPayload)
    const overlayMaterial = new THREE.MeshBasicMaterial({
      map: overlayTexture,
      transparent: true,
      opacity: 1,
      depthTest: false,
      depthWrite: false,
      toneMapped: false,
    })
    const overlay = new THREE.Mesh(overlayGeometry, overlayMaterial)
    overlay.renderOrder = 2
    runtime.scene.add(overlay)

    return () => {
      runtime.scene.remove(overlay)
      overlayGeometry.dispose()
      overlayMaterial.dispose()
      overlayTexture.dispose()
    }
  }, [pointOverlayEnabled, pointPayload])

  useEffect(() => {
    if (!runtimeRef.current) return
    runtimeRef.current.panoramaMaterial.opacity = effectivePanoramaOpacity
    runtimeRef.current.panoramaMaterial.needsUpdate = true
  }, [effectivePanoramaOpacity])

  const changeZoom = (delta: number) => {
    setFov((current) => Math.min(95, Math.max(28, current + delta)))
  }

  const toggleFullscreen = async () => {
    if (!stageRef.current) return
    const ownerDocument = stageRef.current.ownerDocument
    if (ownerDocument.fullscreenElement) await ownerDocument.exitFullscreen()
    else await stageRef.current.requestFullscreen()
  }

  const canGoPrevious = Boolean(frame && onPreviousFrame && hasPreviousFrame)
  const canGoNext = Boolean(frame && onNextFrame && hasNextFrame)

  const goToPreviousFrame = () => {
    if (canGoPrevious) onPreviousFrame?.()
  }

  const goToNextFrame = () => {
    if (canGoNext) onNextFrame?.()
  }

  const changeQuality = (nextQuality: PanoramaQuality) => {
    if (controlledQuality === undefined) setLocalQuality(nextQuality)
    onQualityChange?.(nextQuality)
  }

  const changePointOverlay = (enabled: boolean) => {
    if (controlledPointOverlayEnabled === undefined) setLocalPointOverlayEnabled(enabled)
    onPointOverlayEnabledChange?.(enabled)
  }

  const changePanoramaOpacity = (opacity: number) => {
    if (controlledPanoramaOpacity === undefined) setLocalPanoramaOpacity(opacity)
    onPanoramaOpacityChange?.(opacity)
  }

  const selectAtPointer = async (clientX: number, clientY: number) => {
    const runtime = runtimeRef.current
    if (!runtime || !frame) return
    const bounds = runtime.renderer.domElement.getBoundingClientRect()
    if (!bounds.width || !bounds.height) return
    const pointer = new THREE.Vector2(
      ((clientX - bounds.left) / bounds.width) * 2 - 1,
      -((clientY - bounds.top) / bounds.height) * 2 + 1,
    )
    const raycaster = new THREE.Raycaster()
    raycaster.setFromCamera(pointer, runtime.camera)
    const intersection = raycaster.intersectObject(runtime.panoramaMesh, false)[0]
    if (!intersection?.uv) return
    const u = ((intersection.uv.x % 1) + 1) % 1
    const v = 1 - intersection.uv.y
    const actions = overlayActionsRef.current

    if (actions.pickMode) {
      if (!pointPayload) {
        setPickFeedback('좌표 선택용 MMS 포인트를 아직 불러오는 중입니다.')
        return
      }
      const index = nearestPanoramaPointIndex(
        u,
        v,
        pointPayload.coordinates,
        pointPayload.pointCount,
      )
      if (index === null) {
        setPickFeedback('클릭한 위치 가까이에 깊이 포인트가 없습니다. 포인트가 보이는 곳을 선택해 주세요.')
        return
      }
      const offset = index * 3
      setPickFeedback('선택한 MMS 포인트의 실제 좌표를 계산하고 있습니다.')
      try {
        const result = await api.panoramaPick(datasetId, frame.id, {
          u: pointPayload.coordinates[offset],
          v: pointPayload.coordinates[offset + 1],
          depth: pointPayload.coordinates[offset + 2],
        })
        await actions.applyPickedCoordinate(result.dataset_position, 'dataset')
        setPickFeedback(null)
      } catch (reason) {
        setPickFeedback(reason instanceof Error ? reason.message : '선택 좌표를 적용하지 못했습니다.')
      }
      return
    }

    let nearest: RenderPanoramaOverlayPoint | null = null
    let nearestDistance = 0.025 ** 2
    renderedOverlayProjection.forEach((point) => {
      const distance = wrappedUVDistanceSquared(u, v, point.u, point.v)
      if (distance <= nearestDistance) {
        nearestDistance = distance
        nearest = point
      }
    })
    if (nearest) {
      actions.selectFeature({
        layerId: (nearest as RenderPanoramaOverlayPoint).layerId,
        featureId: (nearest as RenderPanoramaOverlayPoint).feature_id,
      })
    }
  }

  return (
    <div
      ref={stageRef}
      className={`panorama-view ${dragStart ? 'dragging' : ''}`}
      tabIndex={0}
      role="region"
      aria-label="파노라마 뷰어"
      data-frame-id={frame?.id ?? ''}
      data-yaw={yaw}
      data-forward-offset={forwardOffsetDeg}
      data-point-count={pointPayload?.pointCount ?? 0}
      data-shp-point-count={renderedOverlayProjection.length}
      data-panorama-opacity={effectivePanoramaOpacity}
      onPointerDown={(event) => {
        if (!source) return
        event.currentTarget.focus({ preventScroll: true })
        event.currentTarget.setPointerCapture(event.pointerId)
        setDragStart({ x: event.clientX, y: event.clientY, yaw, pitch })
      }}
      onPointerMove={(event) => {
        if (!dragStart) return
        setYaw(dragStart.yaw - (event.clientX - dragStart.x) * 0.12)
        setPitch(
          Math.max(-78, Math.min(78, dragStart.pitch + (event.clientY - dragStart.y) * 0.1)),
        )
      }}
      onPointerUp={(event) => {
        const clicked = Boolean(
          dragStart && Math.hypot(event.clientX - dragStart.x, event.clientY - dragStart.y) <= 5,
        )
        setDragStart(null)
        if (clicked) void selectAtPointer(event.clientX, event.clientY)
      }}
      onPointerCancel={() => setDragStart(null)}
      onWheel={(event) => {
        event.preventDefault()
        changeZoom(event.deltaY > 0 ? 4 : -4)
      }}
    >
      {frame && onPreviousFrame && (
        <button
          type="button"
          className="panorama-step-zone previous"
          aria-label="이전 프레임으로 이동"
          title="이전 프레임 (←)"
          disabled={!canGoPrevious}
          onPointerDown={(event) => event.stopPropagation()}
          onClick={goToPreviousFrame}
        >
          <ChevronLeft aria-hidden="true" />
          <span>이전</span>
        </button>
      )}
      {frame && onNextFrame && (
        <button
          type="button"
          className="panorama-step-zone next"
          aria-label="다음 프레임으로 이동"
          title="다음 프레임 (→)"
          disabled={!canGoNext}
          onPointerDown={(event) => event.stopPropagation()}
          onClick={goToNextFrame}
        >
          <span>다음</span>
          <ChevronRight aria-hidden="true" />
        </button>
      )}
      {loading && (
        <div className="viewer-loading floating">
          <LoaderCircle className="spin" size={25} />
          <strong>파노라마 미리보기 생성 중</strong>
          <small>화면 크기에 맞춘 경량 이미지를 요청했습니다.</small>
        </div>
      )}
      {error && (
        <div className="viewer-error">
          <RefreshCcw size={25} />
          <strong>이미지를 표시할 수 없습니다</strong>
          <p>{error}</p>
          <button type="button" className="button secondary" onClick={() => setReloadKey((value) => value + 1)}>
            다시 불러오기
          </button>
        </div>
      )}
      {!frame && (
        <div className="viewer-error neutral">
          <Maximize2 size={26} />
          <strong>프레임을 선택해 주세요</strong>
          <p>왼쪽 목록에서 파노라마가 있는 프레임을 선택하세요.</p>
        </div>
      )}
      {pointPayloadRequired && (pointLoading || pointError) && (
        <div className={`panorama-point-status ${pointError ? 'error' : ''}`}>
          {pointLoading ? <LoaderCircle size={13} className="spin" /> : <ScanLine size={13} />}
          <span>
            {pointError
              ? pointError
              : pointIndexing
                ? '포인트 인덱싱 중'
                : '파노라마 포인트 갱신 중'}
          </span>
          {pointError && (
            <button
              type="button"
              onPointerDown={(event) => event.stopPropagation()}
              onClick={() => setPointReloadKey((value) => value + 1)}
            >
              재시도
            </button>
          )}
        </div>
      )}
      {(overlayLoading || pickFeedback) && (
        <div className={`panorama-point-status shp-status ${pickFeedback ? 'error' : ''}`}>
          {overlayLoading ? <LoaderCircle size={13} className="spin" /> : <Crosshair size={13} />}
          <span>{pickFeedback ?? 'SHP 포인트를 파노라마에 맞추는 중'}</span>
        </div>
      )}
      <div
        className="viewer-toolbar panorama-toolbar"
        onPointerDown={(event) => event.stopPropagation()}
      >
        <span>
          <MousePointer2 size={14} />
          드래그하여 둘러보기
        </span>
        <i />
        <label className="panorama-quality-control">
          <span>화질</span>
          <select
            aria-label="파노라마 화질"
            value={quality}
            onChange={(event) => changeQuality(event.target.value as PanoramaQuality)}
          >
            <option value="fast">빠름</option>
            <option value="high">고화질 · 4K</option>
            <option value="ultra">최고화질 · 8K</option>
          </select>
        </label>
        <label className="panorama-point-toggle" title="선택 프레임의 MMS 포인트를 파노라마에 겹쳐 표시">
          <input
            type="checkbox"
            aria-label="파노라마 포인트 오버레이 표시"
            checked={pointOverlayEnabled}
            onChange={(event) => changePointOverlay(event.target.checked)}
          />
          <ScanLine size={14} />
          포인트
        </label>
        <label className="panorama-opacity-control">
          <span>영상 투명도</span>
          <input
            type="range"
            min={0}
            max={1}
            step={0.05}
            value={panoramaOpacity}
            disabled={!hasVisualOverlay}
            aria-label="파노라마 영상 투명도"
            onChange={(event) => changePanoramaOpacity(Number(event.target.value))}
          />
        </label>
        {renderedOverlayProjection.length > 0 && (
          <span title="현재 파노라마에 투영된 SHP 포인트">
            <MapPin size={14} /> SHP {renderedOverlayProjection.length.toLocaleString('ko-KR')}
          </span>
        )}
        {overlay?.pickMode && (
          <strong className="viewer-pick-indicator">
            <Crosshair size={14} /> 포인트를 클릭해 좌표 적용
          </strong>
        )}
        <button type="button" onClick={() => changeZoom(6)} aria-label="축소">
          <Minus size={15} />
        </button>
        <strong>{Math.round((72 / fov) * 100)}%</strong>
        <button type="button" onClick={() => changeZoom(-6)} aria-label="확대">
          <Plus size={15} />
        </button>
        <button type="button" onClick={toggleFullscreen} aria-label="전체 화면">
          <Expand size={15} />
        </button>
      </div>
      {frame && (
        <div className="viewer-data-card">
          <span>CAM · 360°</span>
          <strong>{frame.timestamp.replace('T', ' ').slice(0, 23)}</strong>
          <small>
            {frame.coordinate
              ? `${frame.coordinate.lat.toFixed(6)}, ${frame.coordinate.lon.toFixed(6)}`
              : '좌표 없음'}
          </small>
        </div>
      )}
    </div>
  )
}
