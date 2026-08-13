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
import {
  openOverlayFeatureDetails,
  OverlayHoverTooltip,
  type OverlayHoverState,
} from '../components/OverlayHoverTooltip'
import { useOptionalOverlayWorkspace } from '../components/OverlayContext'
import { api, ApiError } from '../lib/api'
import { createDemoPanoramaPoints, parseMmso } from '../lib/mmso'
import { panoramaUvToSpherePosition, type PanoramaHoverProjection } from '../lib/panoramaProjection'
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

export interface RenderPanoramaOverlayPoint extends PanoramaOverlayFeature {
  layerId: string
  layerName: string
  color: string
  selected: boolean
  detectionBox?: PanoramaDetectionBox | null
}

export interface RenderPanoramaDetectionBox {
  sourceId: string
  observationId: string
  featureId?: string | number
  layerId?: string
  layerName: string
  properties: Record<string, unknown>
  color: string
  selected: boolean
  detectionBox: PanoramaDetectionBox
}

const DETECTION_SOURCE_COLORS = ['#ffb84d', '#4dd9ff', '#ff6f91', '#7ee787', '#c59cff']

export function panoramaDetectionSourceColor(sourceId: string): string {
  let hash = 0
  for (let index = 0; index < sourceId.length; index += 1) {
    hash = ((hash * 31) + sourceId.charCodeAt(index)) >>> 0
  }
  return DETECTION_SOURCE_COLORS[hash % DETECTION_SOURCE_COLORS.length]
}

export interface PanoramaOverlayHit {
  layerId?: string
  layerName: string
  featureId: string | number
  properties: Record<string, unknown>
}

export function panoramaDetectionPointRadius(selected: boolean, depth: number): number {
  if (selected) return 3.5
  return depth < 20 ? 2 : 1.5
}

export interface PanoramaDetectionBox {
  left: number
  top: number
  right: number
  bottom: number
  panoramaWidth: number
  panoramaHeight: number
  label: string
}

function propertyValue(
  properties: Record<string, unknown> | undefined,
  ...names: string[]
): unknown {
  if (!properties) return undefined
  const normalized = new Map(
    Object.entries(properties).map(([key, value]) => [key.toLocaleLowerCase('en-US'), value]),
  )
  for (const name of names) {
    const value = normalized.get(name.toLocaleLowerCase('en-US'))
    if (value !== undefined && value !== null && value !== '') return value
  }
  return undefined
}

function finiteProperty(
  properties: Record<string, unknown> | undefined,
  ...names: string[]
): number | null {
  const value = Number(propertyValue(properties, ...names))
  return Number.isFinite(value) ? value : null
}

function baseImageName(value: unknown): string {
  return String(value ?? '')
    .trim()
    .split(/[?#]/, 1)[0]
    .split(/[\\/]/)
    .at(-1)
    ?.normalize('NFC')
    .toLocaleLowerCase('en-US') ?? ''
}

function imageNamesMatch(source: unknown, current: unknown): boolean {
  const sourceName = baseImageName(source)
  const currentName = baseImageName(current)
  if (!sourceName || !currentName) return false
  if (sourceName === currentName) return true
  const sourceStem = sourceName.replace(/\.[^.]+$/, '')
  const currentStem = currentName.replace(/\.[^.]+$/, '')
  return sourceStem === currentStem
}

function finiteTuple(value: unknown): [number, number, number, number] | null {
  let candidate = value
  if (typeof candidate === 'string') {
    const text = candidate.trim()
    if (!text) return null
    try {
      candidate = JSON.parse(text)
    } catch {
      candidate = text.split(/[\s,;]+/)
    }
  }
  if (!Array.isArray(candidate) || candidate.length !== 4) return null
  const tuple = candidate.map(Number)
  return tuple.every(Number.isFinite)
    ? tuple as [number, number, number, number]
    : null
}

export function panoramaDetectionBox(
  properties: Record<string, unknown> | undefined,
  currentImageName?: string,
): PanoramaDetectionBox | null {
  const sourceImage = baseImageName(
    propertyValue(
      properties,
      'img_name',
      'image_name',
      'image',
      'filename',
      'image_path',
      'img_path',
      'source_image',
    ),
  )
  if (currentImageName && sourceImage && !imageNamesMatch(sourceImage, currentImageName)) return null
  const tuple = finiteTuple(propertyValue(properties, 'bbox_xyxy', 'bbox', 'box_xyxy'))
  const left = tuple?.[0] ?? finiteProperty(properties, 'bbox_l', 'bbox_left', 'x1', 'xmin')
  const top = tuple?.[1] ?? finiteProperty(properties, 'bbox_t', 'bbox_top', 'y1', 'ymin')
  const right = tuple?.[2] ?? finiteProperty(properties, 'bbox_r', 'bbox_right', 'x2', 'xmax')
  const bottom = tuple?.[3] ?? finiteProperty(properties, 'bbox_b', 'bbox_bottom', 'y2', 'ymax')
  const panoramaWidth = finiteProperty(
    properties,
    'pano_w',
    'panorama_width',
    'image_width',
    'img_width',
    'img_w',
    'orig_w',
    'width_px',
  )
  const panoramaHeight = finiteProperty(
    properties,
    'pano_h',
    'panorama_height',
    'image_height',
    'img_height',
    'img_h',
    'orig_h',
    'height_px',
  )
  if (
    left === null ||
    top === null ||
    right === null ||
    bottom === null ||
    panoramaWidth === null ||
    panoramaHeight === null ||
    panoramaWidth <= 0 ||
    panoramaHeight <= 0 ||
    right <= left ||
    bottom <= top
  ) {
    return null
  }
  const className = String(
    propertyValue(properties, 'class_nm', 'class_name', 'class', 'label') ?? '검출 객체',
  )
  const confidence = finiteProperty(properties, 'conf', 'confidence')
  const confidencePercent = confidence === null
    ? null
    : confidence <= 1
      ? confidence * 100
      : confidence
  return {
    left,
    top,
    right,
    bottom,
    panoramaWidth,
    panoramaHeight,
    label: confidencePercent === null ? className : `${className} ${Math.round(confidencePercent)}%`,
  }
}

function createPanoramaOverlayTexture(
  ownerDocument: Document,
  points: RenderPanoramaOverlayPoint[],
  detectionBoxes: RenderPanoramaDetectionBox[],
): THREE.CanvasTexture {
  const width = 2048
  const height = 1024
  const canvas = ownerDocument.createElement('canvas')
  canvas.width = width
  canvas.height = height
  const context = canvas.getContext('2d')
  if (!context) throw new Error('SHP 오버레이 캔버스를 만들 수 없습니다.')

  const boxes = [
    ...points.filter((point) => point.detectionBox),
    ...detectionBoxes,
  ]
  boxes.forEach((point) => {
    const box = point.detectionBox
    if (!box) return
    const left = (box.left / box.panoramaWidth) * width
    const top = (box.top / box.panoramaHeight) * height
    const boxWidth = ((box.right - box.left) / box.panoramaWidth) * width
    const boxHeight = ((box.bottom - box.top) / box.panoramaHeight) * height
    if (boxWidth <= 0 || boxHeight <= 0 || boxWidth > width * 0.75) return
    context.lineWidth = point.selected ? 5 : 3
    context.strokeStyle = point.selected ? '#ffffff' : point.color
    context.font = `700 ${point.selected ? 18 : 15}px Pretendard, sans-serif`
    context.textBaseline = 'bottom'
    ;[-width, 0, width].forEach((shift) => {
      const x = left + shift
      if (x + boxWidth < 0 || x > width) return
      context.strokeRect(x, top, boxWidth, boxHeight)
      const labelWidth = Math.min(boxWidth, context.measureText(box.label).width + 12)
      const labelTop = Math.max(0, top - 24)
      context.fillStyle = point.selected ? '#ffffff' : point.color
      context.fillRect(x, labelTop, labelWidth, 24)
      context.fillStyle = '#061018'
      context.fillText(box.label, x + 6, labelTop + 20, Math.max(0, labelWidth - 10))
    })
  })

  points.forEach((point) => {
    const x = (((point.u % 1) + 1) % 1) * width
    const y = Math.min(1, Math.max(0, point.v)) * height
    const radius = panoramaDetectionPointRadius(point.selected, point.depth)
    context.beginPath()
    context.arc(x, y, radius + 1.25, 0, Math.PI * 2)
    context.fillStyle = point.selected ? '#ffffff' : 'rgba(7, 17, 31, 0.9)'
    context.fill()
    context.beginPath()
    context.arc(x, y, radius, 0, Math.PI * 2)
    context.fillStyle = point.color
    context.fill()
    context.lineWidth = 1
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

export function panoramaDetectionBoxContainsUv(
  box: PanoramaDetectionBox,
  u: number,
  v: number,
): boolean {
  const x = (((u % 1) + 1) % 1) * box.panoramaWidth
  const y = Math.min(1, Math.max(0, v)) * box.panoramaHeight
  if (y < box.top || y > box.bottom) return false
  return [-box.panoramaWidth, 0, box.panoramaWidth].some((shift) => {
    const shiftedX = x + shift
    return shiftedX >= box.left && shiftedX <= box.right
  })
}

export function panoramaOverlayAtUv(
  points: RenderPanoramaOverlayPoint[],
  u: number,
  v: number,
  detectionBoxes: RenderPanoramaDetectionBox[] = [],
): PanoramaOverlayHit | null {
  let boxed: PanoramaOverlayHit | null = null
  let boxedArea = Number.POSITIVE_INFINITY
  detectionBoxes.forEach((observation) => {
    if (!panoramaDetectionBoxContainsUv(observation.detectionBox, u, v)) return
    const area = (
      (observation.detectionBox.right - observation.detectionBox.left)
      * (observation.detectionBox.bottom - observation.detectionBox.top)
    )
    if (area < boxedArea) {
      boxed = {
        ...(observation.featureId === undefined ? {} : { layerId: observation.layerId }),
        layerName: observation.layerName,
        featureId: observation.featureId ?? observation.observationId,
        properties: observation.properties,
      }
      boxedArea = area
    }
  })
  points.forEach((point) => {
    const box = point.detectionBox
    if (!box || !panoramaDetectionBoxContainsUv(box, u, v)) return
    const area = (box.right - box.left) * (box.bottom - box.top)
    if (area < boxedArea) {
      boxed = {
        layerId: point.layerId,
        layerName: point.layerName,
        featureId: point.feature_id,
        properties: point.properties ?? {},
      }
      boxedArea = area
    }
  })
  if (boxed) return boxed

  let nearest: PanoramaOverlayHit | null = null
  let nearestDistance = 0.025 ** 2
  points.forEach((point) => {
    const distance = wrappedUVDistanceSquared(u, v, point.u, point.v)
    if (distance <= nearestDistance) {
      nearestDistance = distance
      nearest = {
        layerId: point.layerId,
        layerName: point.layerName,
        featureId: point.feature_id,
        properties: point.properties ?? {},
      }
    }
  })
  return nearest
}

export function deduplicatePanoramaDetectionBoxes(
  boxes: RenderPanoramaDetectionBox[],
): RenderPanoramaDetectionBox[] {
  const seen = new Set<string>()
  return boxes.filter((box) => {
    const { left, top, right, bottom, panoramaWidth, panoramaHeight, label } = box.detectionBox
    // The same run/model result SHP can be opened more than once. Collapse
    // identical observations across those layers, while retaining coincident
    // detections produced by a different model or run.
    const key = [
      box.sourceId,
      box.observationId,
      left,
      top,
      right,
      bottom,
      panoramaWidth,
      panoramaHeight,
      label,
    ].join(':')
    if (seen.has(key)) return false
    seen.add(key)
    return true
  })
}

function normalizedPropertyText(
  properties: Record<string, unknown> | undefined,
  ...names: string[]
): string {
  return String(propertyValue(properties, ...names) ?? '')
    .trim()
    .normalize('NFC')
    .toLocaleLowerCase('en-US')
}

function optionalPropertiesMatch(
  left: Record<string, unknown> | undefined,
  right: Record<string, unknown> | undefined,
  ...names: string[]
): boolean {
  const leftValue = normalizedPropertyText(left, ...names)
  const rightValue = normalizedPropertyText(right, ...names)
  return !leftValue || !rightValue || leftValue === rightValue
}

function panoramaDetectionBoxesMatch(
  left: PanoramaDetectionBox,
  right: PanoramaDetectionBox,
): boolean {
  const width = Math.max(left.panoramaWidth, right.panoramaWidth)
  const height = Math.max(left.panoramaHeight, right.panoramaHeight)
  if (
    Math.abs(left.panoramaWidth - right.panoramaWidth) > Math.max(1, width * 0.0001)
    || Math.abs(left.panoramaHeight - right.panoramaHeight) > Math.max(1, height * 0.0001)
  ) return false
  const tolerance = Math.max(1, width * 0.0001, height * 0.0001)
  if (
    Math.abs(left.top - right.top) > tolerance
    || Math.abs(left.bottom - right.bottom) > tolerance
  ) return false
  return [-width, 0, width].some((shift) => (
    Math.abs(left.left + shift - right.left) <= tolerance
    && Math.abs(left.right + shift - right.right) <= tolerance
  ))
}

/**
 * Attach a raw per-model observation to its representative visible SHP
 * feature. The raw box remains the sole visual rectangle, but hit-testing can
 * still open the layer's feature details. Coincident observations from another
 * model are not linked unless their identity/metadata is compatible.
 */
export function reconcilePanoramaDetectionBoxes(
  boxes: RenderPanoramaDetectionBox[],
  points: RenderPanoramaOverlayPoint[],
): RenderPanoramaDetectionBox[] {
  return boxes.map((box) => {
    const detectionId = normalizedPropertyText(box.properties, 'det_id', 'detection_id')
    const representative = points.find((point) => {
      const properties = point.properties
      const pointDetectionId = normalizedPropertyText(properties, 'det_id', 'detection_id')
      if (detectionId && pointDetectionId && detectionId !== pointDetectionId) return false
      if (!optionalPropertiesMatch(box.properties, properties, 'model_nm', 'model_name')) return false
      if (!optionalPropertiesMatch(box.properties, properties, 'img_name', 'image_name')) return false
      if (!optionalPropertiesMatch(box.properties, properties, 'class_nm', 'class_name')) return false

      const boxesMatch = point.detectionBox
        ? panoramaDetectionBoxesMatch(box.detectionBox, point.detectionBox)
        : false
      if (detectionId && pointDetectionId) {
        return point.detectionBox ? boxesMatch : true
      }
      return boxesMatch
    })
    if (!representative) return box
    return {
      ...box,
      featureId: representative.feature_id,
      layerId: representative.layerId,
      layerName: representative.layerName,
      properties: {
        ...box.properties,
        ...(representative.properties ?? {}),
      },
    }
  })
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
  detectionRevisionKey = '',
  onPreviousFrame,
  onNextFrame,
  hasPreviousFrame = true,
  hasNextFrame = true,
  forwardOffsetDeg = 0,
  quality: controlledQuality,
  onQualityChange,
  pointOverlayEnabled: controlledPointOverlayEnabled,
  panoramaOpacity: controlledPanoramaOpacity,
  maxOverlayDistanceM = 45,
  linkedHoverPoint = null,
  onPointOverlayEnabledChange,
  onPanoramaOpacityChange,
}: {
  datasetId: string
  frame: Frame | null
  demoMode: boolean
  detectionRevisionKey?: string
  onPreviousFrame?: () => void
  onNextFrame?: () => void
  hasPreviousFrame?: boolean
  hasNextFrame?: boolean
  forwardOffsetDeg?: number
  quality?: PanoramaQuality
  onQualityChange?: (quality: PanoramaQuality) => void
  pointOverlayEnabled?: boolean
  panoramaOpacity?: number
  maxOverlayDistanceM?: number
  linkedHoverPoint?: PanoramaHoverProjection | null
  onPointOverlayEnabledChange?: (enabled: boolean) => void
  onPanoramaOpacityChange?: (opacity: number) => void
}) {
  const overlay = useOptionalOverlayWorkspace()
  const stageRef = useRef<HTMLDivElement>(null)
  const linkedPointMarkerRef = useRef<HTMLDivElement>(null)
  const linkedHoverPointRef = useRef(linkedHoverPoint)
  const currentFrameIdRef = useRef(frame?.id)
  const hoverFrameRef = useRef(0)
  const pendingHoverRef = useRef<{ x: number; y: number } | null>(null)
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
  const [detectionBoxes, setDetectionBoxes] = useState<RenderPanoramaDetectionBox[]>([])
  const [detectionLoading, setDetectionLoading] = useState(false)
  const [detectionError, setDetectionError] = useState<string | null>(null)
  const [overlayLoading, setOverlayLoading] = useState(false)
  const [pickFeedback, setPickFeedback] = useState<string | null>(null)
  const [overlayHover, setOverlayHover] = useState<OverlayHoverState | null>(null)
  const [pinnedOverlayHover, setPinnedOverlayHover] = useState<OverlayHoverState | null>(null)
  const runtimeRef = useRef<PanoramaRuntime | null>(null)
  const quality = controlledQuality ?? localQuality
  const pointOverlayEnabled = controlledPointOverlayEnabled ?? localPointOverlayEnabled
  const panoramaOpacity = controlledPanoramaOpacity ?? localPanoramaOpacity
  const pointPayloadRequired = pointOverlayEnabled || Boolean(overlay?.pickMode)
  const hasVisualOverlay = pointOverlayEnabled || overlayProjection.length > 0 || detectionBoxes.length > 0
  // SHP markers are already composited with a transparent texture. Only the
  // dense point-cloud overlay may dim the camera image at the user's request.
  const effectivePanoramaOpacity = pointOverlayEnabled ? panoramaOpacity : 1
  const panoramaOpacityRef = useRef(effectivePanoramaOpacity)
  panoramaOpacityRef.current = effectivePanoramaOpacity
  linkedHoverPointRef.current = linkedHoverPoint
  currentFrameIdRef.current = frame?.id
  const viewRef = useRef({ fov, yaw, pitch })
  viewRef.current = { fov, yaw, pitch }
  const [dragStart, setDragStart] = useState<{ x: number; y: number; yaw: number; pitch: number } | null>(
    null,
  )
  const [reloadKey, setReloadKey] = useState(0)
  const overlayActionsRef = useRef({
    pickMode: false,
    selectFeature: (
      _selection: { layerId: string; featureId: string | number } | null,
      _options?: { navigate?: boolean },
    ) => {},
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
    const ownerWindow = stageRef.current?.ownerDocument.defaultView
    if (hoverFrameRef.current && ownerWindow) ownerWindow.cancelAnimationFrame(hoverFrameRef.current)
    hoverFrameRef.current = 0
    pendingHoverRef.current = null
    setOverlayHover(null)
    setPinnedOverlayHover(null)
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

    const ownerWindow = host.ownerDocument.defaultView ?? window
    const markerPosition = new THREE.Vector3()
    const cameraForward = new THREE.Vector3()
    let raf = 0
    const draw = () => {
      const phi = THREE.MathUtils.degToRad(90 - viewRef.current.pitch)
      const theta = THREE.MathUtils.degToRad(viewRef.current.yaw)
      camera.fov = viewRef.current.fov
      camera.updateProjectionMatrix()
      cameraForward.set(
        Math.sin(phi) * Math.cos(theta),
        Math.cos(phi),
        Math.sin(phi) * Math.sin(theta),
      )
      camera.lookAt(cameraForward.x * 10, cameraForward.y * 10, cameraForward.z * 10)
      renderer.render(scene, camera)
      const marker = linkedPointMarkerRef.current
      const linked = linkedHoverPointRef.current
      const spherePosition = linked && linked.frameId === currentFrameIdRef.current
        ? panoramaUvToSpherePosition(linked.u, linked.v)
        : null
      if (marker && spherePosition) {
        markerPosition.set(...spherePosition)
        const inFront = markerPosition.dot(cameraForward) > 0
        markerPosition.project(camera)
        const visible =
          inFront &&
          markerPosition.z >= -1 &&
          markerPosition.z <= 1 &&
          Math.abs(markerPosition.x) <= 1.05 &&
          Math.abs(markerPosition.y) <= 1.05
        marker.hidden = !visible
        if (visible) {
          marker.style.left = `${(markerPosition.x * 0.5 + 0.5) * renderer.domElement.clientWidth}px`
          marker.style.top = `${(-markerPosition.y * 0.5 + 0.5) * renderer.domElement.clientHeight}px`
        }
      } else if (marker) {
        marker.hidden = true
      }
      raf = ownerWindow.requestAnimationFrame(draw)
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
      ownerWindow.cancelAnimationFrame(raf)
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
              maxOverlayDistanceM,
            )
            const color = overlayLayerColor?.(layer.id) ?? '#ffb84d'
            groups[index] = response.items.map((item) => ({
              ...item,
              layerId: layer.id,
              layerName: layer.name,
              color,
              selected: false,
              detectionBox: panoramaDetectionBox(item.properties, frame.image_name),
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
    maxOverlayDistanceM,
    visibleOverlayLayers,
  ])

  useEffect(() => {
    if (!frame || demoMode) {
      setDetectionBoxes([])
      setDetectionLoading(false)
      setDetectionError(null)
      return
    }
    const controller = new AbortController()
    setDetectionBoxes([])
    setDetectionLoading(true)
    setDetectionError(null)
    void api.frameDetections(datasetId, frame.id, controller.signal)
      .then((response) => {
        if (controller.signal.aborted) return
        const boxes = response.items.flatMap((observation) => {
          const detectionBox = panoramaDetectionBox(observation.properties, frame.image_name)
          if (!detectionBox) return []
          return [{
            sourceId: observation.source_id,
            observationId: observation.observation_id,
            featureId: observation.feature_id,
            layerName: observation.source_name
              ? `YOLO · ${observation.source_name}`
              : 'YOLO 검출',
            properties: observation.properties,
            color: panoramaDetectionSourceColor(observation.source_id),
            selected: false,
            detectionBox,
          } satisfies RenderPanoramaDetectionBox]
        })
        setDetectionBoxes(deduplicatePanoramaDetectionBoxes(boxes))
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) {
          setDetectionError(
            reason instanceof Error ? reason.message : 'YOLO 검출 박스를 불러오지 못했습니다.',
          )
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setDetectionLoading(false)
      })
    return () => controller.abort()
  }, [datasetId, demoMode, detectionRevisionKey, frame])

  const reconciledDetectionBoxes = useMemo(
    () => reconcilePanoramaDetectionBoxes(detectionBoxes, overlayProjection),
    [detectionBoxes, overlayProjection],
  )

  const renderedOverlayProjection = useMemo(
    () =>
      overlayProjection.map((point) => ({
        ...point,
        selected: `${point.layerId}:${point.feature_id}` === selectedOverlayKey,
        // The frame endpoint contains every raw model observation. Retain a
        // representative SHP bbox only as a fallback for external/legacy data.
        detectionBox: detectionBoxes.length ? null : point.detectionBox,
      })),
    [detectionBoxes.length, overlayProjection, selectedOverlayKey],
  )

  const renderedDetectionBoxes = useMemo(
    () => reconciledDetectionBoxes.map((box) => ({
      ...box,
      selected: box.layerId !== undefined && box.featureId !== undefined
        && `${box.layerId}:${box.featureId}` === selectedOverlayKey,
    })),
    [reconciledDetectionBoxes, selectedOverlayKey],
  )

  useEffect(() => {
    const runtime = runtimeRef.current
    const host = stageRef.current
    if (
      !runtime
      || !host
      || (!renderedOverlayProjection.length && !renderedDetectionBoxes.length)
    ) return
    const geometry = new THREE.SphereGeometry(9.92, 64, 40)
    geometry.scale(-1, 1, 1)
    const texture = createPanoramaOverlayTexture(
      host.ownerDocument,
      renderedOverlayProjection,
      renderedDetectionBoxes,
    )
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
  }, [renderedDetectionBoxes, renderedOverlayProjection])

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

  const panoramaUvAtPointer = (clientX: number, clientY: number) => {
    const runtime = runtimeRef.current
    if (!runtime) return null
    const bounds = runtime.renderer.domElement.getBoundingClientRect()
    if (!bounds.width || !bounds.height) return null
    const pointer = new THREE.Vector2(
      ((clientX - bounds.left) / bounds.width) * 2 - 1,
      -((clientY - bounds.top) / bounds.height) * 2 + 1,
    )
    const raycaster = new THREE.Raycaster()
    raycaster.setFromCamera(pointer, runtime.camera)
    const intersection = raycaster.intersectObject(runtime.panoramaMesh, false)[0]
    if (!intersection?.uv) return null
    return {
      u: ((intersection.uv.x % 1) + 1) % 1,
      v: 1 - intersection.uv.y,
      bounds,
    }
  }

  const clearOverlayHover = () => {
    const ownerWindow = stageRef.current?.ownerDocument.defaultView
    if (hoverFrameRef.current && ownerWindow) ownerWindow.cancelAnimationFrame(hoverFrameRef.current)
    hoverFrameRef.current = 0
    pendingHoverRef.current = null
    setOverlayHover(null)
  }

  const scheduleOverlayHover = (clientX: number, clientY: number) => {
    const ownerWindow = stageRef.current?.ownerDocument.defaultView
    if (!ownerWindow) return
    pendingHoverRef.current = { x: clientX, y: clientY }
    if (hoverFrameRef.current) return
    hoverFrameRef.current = ownerWindow.requestAnimationFrame(() => {
      hoverFrameRef.current = 0
      const pending = pendingHoverRef.current
      pendingHoverRef.current = null
      if (!pending) return
      const projected = panoramaUvAtPointer(pending.x, pending.y)
      if (!projected) {
        setOverlayHover(null)
        return
      }
      const entry = panoramaOverlayAtUv(
        renderedOverlayProjection,
        projected.u,
        projected.v,
        renderedDetectionBoxes,
      )
      setOverlayHover(entry ? {
        layerId: entry.layerId,
        layerName: entry.layerName,
        featureId: entry.featureId,
        properties: entry.properties ?? {},
        x: pending.x - projected.bounds.left,
        y: pending.y - projected.bounds.top,
        viewportWidth: projected.bounds.width,
        viewportHeight: projected.bounds.height,
      } : null)
    })
  }

  const selectAtPointer = async (clientX: number, clientY: number) => {
    if (!frame) return
    const projected = panoramaUvAtPointer(clientX, clientY)
    if (!projected) return
    const { u, v } = projected
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

    const nearest = panoramaOverlayAtUv(renderedOverlayProjection, u, v, renderedDetectionBoxes)
    if (nearest) {
      setOverlayHover(null)
      setPinnedOverlayHover({
        layerId: nearest.layerId,
        layerName: nearest.layerName,
        featureId: nearest.featureId,
        properties: nearest.properties ?? {},
        x: clientX - projected.bounds.left,
        y: clientY - projected.bounds.top,
        viewportWidth: projected.bounds.width,
        viewportHeight: projected.bounds.height,
      })
    } else {
      setPinnedOverlayHover(null)
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
      data-yolo-box-count={renderedDetectionBoxes.length}
      data-panorama-opacity={effectivePanoramaOpacity}
      onPointerDown={(event) => {
        if (!source) return
        clearOverlayHover()
        event.currentTarget.focus({ preventScroll: true })
        event.currentTarget.setPointerCapture(event.pointerId)
        setDragStart({ x: event.clientX, y: event.clientY, yaw, pitch })
      }}
      onPointerMove={(event) => {
        if (dragStart) {
          setYaw(dragStart.yaw - (event.clientX - dragStart.x) * 0.12)
          setPitch(
            Math.max(-78, Math.min(78, dragStart.pitch + (event.clientY - dragStart.y) * 0.1)),
          )
        } else {
          scheduleOverlayHover(event.clientX, event.clientY)
        }
      }}
      onPointerUp={(event) => {
        const clicked = Boolean(
          dragStart && Math.hypot(event.clientX - dragStart.x, event.clientY - dragStart.y) <= 5,
        )
        setDragStart(null)
        if (clicked) void selectAtPointer(event.clientX, event.clientY)
      }}
      onPointerCancel={() => {
        setDragStart(null)
        clearOverlayHover()
      }}
      onPointerLeave={clearOverlayHover}
      onWheel={(event) => {
        event.preventDefault()
        changeZoom(event.deltaY > 0 ? 4 : -4)
      }}
    >
      <div
        ref={linkedPointMarkerRef}
        className="panorama-linked-point"
        aria-hidden="true"
        hidden
      />
      <OverlayHoverTooltip
        hover={pinnedOverlayHover ?? overlayHover}
        pinned={Boolean(pinnedOverlayHover)}
        onClose={() => {
          setPinnedOverlayHover(null)
          setOverlayHover(null)
        }}
        onDetails={pinnedOverlayHover?.layerId ? (hover) => {
          if (!hover.layerId) return
          overlayActionsRef.current.selectFeature({
            layerId: hover.layerId,
            featureId: hover.featureId,
          }, { navigate: false })
          openOverlayFeatureDetails(datasetId, hover)
        } : undefined}
      />
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
      {(overlayLoading || pickFeedback || detectionLoading || detectionError) && (
        <div className={`panorama-point-status shp-status ${pickFeedback || detectionError ? 'error' : ''}`}>
          {overlayLoading || detectionLoading
            ? <LoaderCircle size={13} className="spin" />
            : <Crosshair size={13} />}
          <span>
            {pickFeedback
              ?? detectionError
              ?? (overlayLoading
                ? 'SHP 포인트를 파노라마에 맞추는 중'
                : 'YOLO 검출 박스를 불러오는 중')}
          </span>
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
        {renderedDetectionBoxes.length > 0 && (
          <span title="현재 파노라마의 원본 YOLO 검출 박스">
            <ScanLine size={14} /> YOLO {renderedDetectionBoxes.length.toLocaleString('ko-KR')}
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
