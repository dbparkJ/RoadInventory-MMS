import {
  AlertTriangle,
  Box,
  Camera,
  Check,
  CircleGauge,
  Crosshair,
  LoaderCircle,
  MapPin,
  Palette,
  RefreshCcw,
  Ruler,
  Rotate3D,
  Scan,
  X,
} from 'lucide-react'
import { useDeferredValue, useEffect, useMemo, useRef, useState } from 'react'
import * as THREE from 'three'
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js'
import {
  openOverlayFeatureDetails,
  OverlayHoverTooltip,
  type OverlayHoverState,
} from '../components/OverlayHoverTooltip'
import {
  POLE_BASE_REASON_MESSAGES,
  poleBaseReasonMessage,
  poleBaseTemplateValidationBlocksSave,
  useOptionalOverlayWorkspace,
  type OverlayPickTarget,
  type PoleBaseProposalState,
} from '../components/OverlayContext'
import { useOptionalManualObjectWorkspace } from '../components/ManualObjectContext'
import { api, ApiError } from '../lib/api'
import { createDemoPointCloud } from '../lib/demo'
import { formatCount } from '../lib/format'
import { parseMmsp } from '../lib/mmsp'
import { DEFAULT_USER_SETTINGS } from '../lib/userSettings'
import {
  projectFrameLocalPointToPanorama,
  type PanoramaHoverProjection,
} from '../lib/panoramaProjection'
import type {
  Frame,
  OverlayFeature,
  PanoramaDetectionBoxObservation,
  PanoramaProjectionMetadata,
  PoleBaseInferResponse,
  PointCloudPayload,
} from '../types'

export const POINT_CLOUD_BUDGETS = [
  { value: 250_000, label: '기본 · 25만' },
  { value: 500_000, label: '정밀 · 50만' },
  { value: 1_000_000, label: '최대 · 100만' },
]
export const DEFAULT_POINT_CLOUD_BUDGET = 250_000
export function pointCloudBudgetsForMaximum(maximum: number) {
  const finiteMaximum = Number.isFinite(maximum) ? maximum : DEFAULT_POINT_CLOUD_BUDGET
  const safeMaximum = Math.max(
    DEFAULT_POINT_CLOUD_BUDGET,
    Math.min(1_000_000, Math.floor(finiteMaximum)),
  )
  return POINT_CLOUD_BUDGETS.filter((entry) => entry.value <= safeMaximum)
}
const POINT_PREVIEW_DENSE_RADIUS_M = 15
const POINT_PREVIEW_MAX_RADIUS_M = 25

export type PointCloudColorMode = 'rgb' | 'intensity' | 'classification' | 'height'

export interface PointCloudDisplayOptions {
  clipRadiusM: number | null
  clipCenter?: [number, number]
  zRange?: [number, number] | null
  proposalPosition?: [number, number, number] | null
  isolateProposal?: boolean
  proposalRadiusM?: number
}

export function pointCloudMeasurement(
  first: [number, number, number],
  second: [number, number, number],
): { distance3d: number; distanceXy: number; vertical: number } {
  const dx = second[0] - first[0]
  const dy = second[1] - first[1]
  const dz = second[2] - first[2]
  return {
    distance3d: Math.hypot(dx, dy, dz),
    distanceXy: Math.hypot(dx, dy),
    vertical: Math.abs(dz),
  }
}

export function buildPointCloudDisplayPayload(
  payload: PointCloudPayload,
  options: PointCloudDisplayOptions,
): PointCloudPayload {
  const filtering =
    options.clipRadiusM !== null ||
    options.zRange !== null && options.zRange !== undefined ||
    Boolean(options.isolateProposal && options.proposalPosition)
  if (!filtering) return payload

  const positions = new Float32Array(payload.positions.length)
  const colors = payload.colors ? new Uint8Array(payload.pointCount * 3) : undefined
  const clipCenter = options.clipCenter ?? (
    options.proposalPosition ? [options.proposalPosition[0], options.proposalPosition[1]] : [0, 0]
  )
  const proposalRadius = options.proposalRadiusM ?? 3
  const bounds = {
    min: [Number.POSITIVE_INFINITY, Number.POSITIVE_INFINITY, Number.POSITIVE_INFINITY] as [number, number, number],
    max: [Number.NEGATIVE_INFINITY, Number.NEGATIVE_INFINITY, Number.NEGATIVE_INFINITY] as [number, number, number],
  }
  let count = 0
  for (let index = 0; index < payload.pointCount; index += 1) {
    const sourceOffset = index * 3
    const x = payload.positions[sourceOffset]
    const y = payload.positions[sourceOffset + 1]
    const z = payload.positions[sourceOffset + 2]
    if (options.clipRadiusM !== null && Math.hypot(x - clipCenter[0], y - clipCenter[1]) > options.clipRadiusM) continue
    if (options.zRange && (z < options.zRange[0] || z > options.zRange[1])) continue
    if (
      options.isolateProposal &&
      options.proposalPosition &&
      Math.hypot(
        x - options.proposalPosition[0],
        y - options.proposalPosition[1],
        z - options.proposalPosition[2],
      ) > proposalRadius
    ) continue

    const targetOffset = count * 3
    positions[targetOffset] = x
    positions[targetOffset + 1] = y
    positions[targetOffset + 2] = z
    bounds.min[0] = Math.min(bounds.min[0], x)
    bounds.min[1] = Math.min(bounds.min[1], y)
    bounds.min[2] = Math.min(bounds.min[2], z)
    bounds.max[0] = Math.max(bounds.max[0], x)
    bounds.max[1] = Math.max(bounds.max[1], y)
    bounds.max[2] = Math.max(bounds.max[2], z)

    if (colors && payload.colors) {
      colors[targetOffset] = payload.colors[sourceOffset]
      colors[targetOffset + 1] = payload.colors[sourceOffset + 1]
      colors[targetOffset + 2] = payload.colors[sourceOffset + 2]
    }
    count += 1
  }
  if (count === 0) {
    bounds.min = [0, 0, 0]
    bounds.max = [0, 0, 0]
  }
  return {
    positions: positions.slice(0, count * 3),
    colors: colors ? colors.slice(0, count * 3) : null,
    bounds,
    pointCount: count,
  }
}

export interface PointCloudViewState {
  position: [number, number, number]
  target: [number, number, number]
  zoom: number
}

export interface PointCloudAnimationScheduler {
  requestAnimationFrame: (callback: FrameRequestCallback) => number
  cancelAnimationFrame: (handle: number) => void
}

export interface PointCloudRenderLoop {
  wake: () => void
  stop: () => void
  suspend: () => void
  resume: () => void
  dispose: () => void
}

export function pointCloudOwnerWindow(
  host: Pick<HTMLElement, 'ownerDocument'>,
): Window {
  return host.ownerDocument.defaultView ?? window
}

/**
 * Keep one restartable RAF chain in the Window that owns the WebGL canvas.
 * A detached viewer must not inherit the opener's throttled animation clock.
 */
export function createPointCloudRenderLoop(
  scheduler: PointCloudAnimationScheduler,
  draw: () => void,
  canDraw: () => boolean = () => true,
): PointCloudRenderLoop {
  let animationFrame = 0
  let disposed = false
  let suspended = false

  const stop = () => {
    if (!animationFrame) return
    scheduler.cancelAnimationFrame(animationFrame)
    animationFrame = 0
  }

  const schedule = () => {
    if (disposed || suspended || animationFrame || !canDraw()) return
    animationFrame = scheduler.requestAnimationFrame(tick)
  }

  const tick = () => {
    animationFrame = 0
    if (disposed || suspended || !canDraw()) return
    draw()
    schedule()
  }

  const wake = () => {
    if (disposed) return
    // Replace a potentially stale callback after a browser/popup suspension.
    stop()
    if (suspended || !canDraw()) return
    draw()
    schedule()
  }

  return {
    wake,
    stop,
    suspend: () => {
      if (disposed) return
      suspended = true
      stop()
    },
    resume: () => {
      if (disposed) return
      suspended = false
      wake()
    },
    dispose: () => {
      if (disposed) return
      stop()
      disposed = true
    },
  }
}

export function capturePointCloudViewState(
  camera: THREE.PerspectiveCamera,
  target: THREE.Vector3,
): PointCloudViewState {
  return {
    position: [camera.position.x, camera.position.y, camera.position.z],
    target: [target.x, target.y, target.z],
    zoom: camera.zoom,
  }
}

export function restorePointCloudViewState(
  camera: THREE.PerspectiveCamera,
  target: THREE.Vector3,
  state: PointCloudViewState,
) {
  camera.position.fromArray(state.position)
  camera.zoom = state.zoom
  camera.updateProjectionMatrix()
  target.fromArray(state.target)
}

export interface RenderOverlayPoint {
  layerId: string
  layerName: string
  featureId: string | number
  color: string
  position: [number, number, number]
  selected: boolean
  properties: Record<string, unknown>
}

export interface RenderPointCloudDetection {
  sourceId: string
  observationId: string
  layerId?: string
  layerName: string
  featureId?: string | number
  color: string
  tooltipColor?: string
  position: [number, number, number]
  selected: boolean
  properties: Record<string, unknown>
}

type PointCloudHoverEntry = RenderOverlayPoint | RenderPointCloudDetection

export function pointCloudHoverState(
  entry: PointCloudHoverEntry,
  viewport: Pick<OverlayHoverState, 'x' | 'y' | 'viewportWidth' | 'viewportHeight'>,
): OverlayHoverState {
  const observationId = 'observationId' in entry ? entry.observationId : ''
  return {
    layerId: entry.layerId,
    layerName: entry.layerName,
    featureId: entry.featureId ?? observationId,
    properties: entry.properties,
    layerColor: 'tooltipColor' in entry ? entry.tooltipColor ?? entry.color : entry.color,
    ...viewport,
  }
}

interface NearbyOverlayFeature {
  layerId: string
  layerName: string
  color: string
  feature: OverlayFeature
}

export function pointCloudOverlayPointSize(selected: boolean): number {
  return selected ? 1 : 0.62
}

export const POINT_CLOUD_YOLO_MARKER_SIZE = 0.36
export const POINT_CLOUD_YOLO_RAYCAST_THRESHOLD = 0.28
export const POINT_CLOUD_YOLO_HIT_RADIUS_PX = 9

export function pointCloudYoloBoxHalfSize(selected: boolean): number {
  return selected ? 0.28 : 0.22
}

const DETECTION_COLORS = ['#ffb84d', '#4dd9ff', '#ff6f91', '#7ee787', '#c59cff']
const DETECTION_BOX_EDGE_INDICES = [
  0, 1, 1, 3, 3, 2, 2, 0,
  4, 5, 5, 7, 7, 6, 6, 4,
  0, 4, 1, 5, 2, 6, 3, 7,
] as const
const DETECTION_BOX_CORNERS = [
  [-1, -1, -1], [1, -1, -1], [-1, 1, -1], [1, 1, -1],
  [-1, -1, 1], [1, -1, 1], [-1, 1, 1], [1, 1, 1],
] as const

function pointCloudDetectionColor(sourceId: string): string {
  let hash = 0
  for (let index = 0; index < sourceId.length; index += 1) {
    hash = ((hash * 31) + sourceId.charCodeAt(index)) >>> 0
  }
  return DETECTION_COLORS[hash % DETECTION_COLORS.length]
}

export function pointCloudDetectionWireframePositions(
  detections: RenderPointCloudDetection[],
): Float32Array {
  const positions = new Float32Array(detections.length * DETECTION_BOX_EDGE_INDICES.length * 3)
  let offset = 0
  detections.forEach((entry) => {
    // The cube is an identity marker around the pipeline's accepted 3-D
    // representative point, not an inferred physical object extent.
    const halfSize = pointCloudYoloBoxHalfSize(entry.selected)
    DETECTION_BOX_EDGE_INDICES.forEach((cornerIndex) => {
      const corner = DETECTION_BOX_CORNERS[cornerIndex]
      positions[offset] = entry.position[0] + corner[0] * halfSize
      positions[offset + 1] = entry.position[1] + corner[1] * halfSize
      positions[offset + 2] = entry.position[2] + corner[2] * halfSize
      offset += 3
    })
  })
  return positions
}

function normalizedDetectionProperty(
  properties: Record<string, unknown>,
  ...aliases: string[]
): string {
  const normalizedAliases = new Set(
    aliases.map((alias) => alias.toLocaleLowerCase('en-US')),
  )
  const match = Object.entries(properties).find(([key, value]) => (
    normalizedAliases.has(key.toLocaleLowerCase('en-US'))
    && String(value ?? '').trim().length > 0
  ))
  return String(match?.[1] ?? '').trim().normalize('NFC').toLocaleLowerCase('en-US')
}

function compatibleDetectionProperty(
  left: Record<string, unknown>,
  right: Record<string, unknown>,
  ...aliases: string[]
): boolean {
  const leftValue = normalizedDetectionProperty(left, ...aliases)
  const rightValue = normalizedDetectionProperty(right, ...aliases)
  return !leftValue || !rightValue || leftValue === rightValue
}

export function pointCloudDetectionsFromObservations(
  observations: PanoramaDetectionBoxObservation[],
  frameOrigin: [number, number, number] | undefined,
  overlayPoints: RenderOverlayPoint[],
  maximumRadius = POINT_PREVIEW_MAX_RADIUS_M,
): RenderPointCloudDetection[] {
  if (!frameOrigin) return []
  const maximumDistanceSquared = maximumRadius ** 2
  return observations.flatMap((observation) => {
    const position = datasetPointToFrameLocal(observation.dataset_position, frameOrigin)
    if (
      !position
      || position[0] ** 2 + position[1] ** 2 + position[2] ** 2 > maximumDistanceSquared
    ) return []
    const detectionId = normalizedDetectionProperty(
      observation.properties,
      'det_id',
      'detection_id',
    )
    const representative = overlayPoints.find((point) => {
      const pointDetectionId = normalizedDetectionProperty(
        point.properties,
        'det_id',
        'detection_id',
      )
      if (!detectionId || !pointDetectionId || detectionId !== pointDetectionId) return false
      if (!compatibleDetectionProperty(observation.properties, point.properties, 'model_nm', 'model_name')) {
        return false
      }
      if (!compatibleDetectionProperty(observation.properties, point.properties, 'img_name', 'image_name')) {
        return false
      }
      if (!compatibleDetectionProperty(observation.properties, point.properties, 'class_nm', 'class_name')) {
        return false
      }
      const deltaSquared = point.position.reduce(
        (sum, coordinate, index) => sum + (coordinate - position[index]) ** 2,
        0,
      )
      return deltaSquared <= 0.5 ** 2
    })
    return [{
      sourceId: observation.source_id,
      observationId: observation.observation_id,
      layerId: representative?.layerId,
      layerName: representative?.layerName
        ?? (observation.source_name ? `YOLO · ${observation.source_name}` : 'YOLO 검출'),
      featureId: representative?.featureId,
      color: pointCloudDetectionColor(observation.model_id ?? observation.source_id),
      tooltipColor: representative?.color,
      position,
      selected: representative?.selected ?? false,
      properties: {
        ...observation.properties,
        ...(representative?.properties ?? {}),
      },
    }]
  })
}

export function demoPanoramaProjectionMetadata(frame: Frame): PanoramaProjectionMetadata {
  return {
    frame_id: frame.id,
    coordinate_space: 'dataset',
    projection: 'normalized_equirectangular',
    origin: frame.dataset_position ?? [0, 0, 0],
    forward: [0, 1, 0],
    right: [1, 0, 0],
    up: [0, 0, 1],
    yaw_offset_deg: 0,
    pitch_offset_deg: 0,
  }
}

export function datasetPointToFrameLocal(
  coordinates: unknown,
  frameOrigin: [number, number, number] | undefined,
): [number, number, number] | null {
  if (
    !frameOrigin ||
    !Array.isArray(coordinates) ||
    coordinates.length < 2 ||
    !Number.isFinite(coordinates[0]) ||
    !Number.isFinite(coordinates[1])
  ) {
    return null
  }
  const z = Number.isFinite(coordinates[2]) ? Number(coordinates[2]) : frameOrigin[2]
  return [
    Number(coordinates[0]) - frameOrigin[0],
    Number(coordinates[1]) - frameOrigin[1],
    z - frameOrigin[2],
  ]
}

export interface PoleBasePreviewGeometry {
  seed: [number, number, number]
  base: [number, number, number] | null
  axis: [[number, number, number], [number, number, number]] | null
  guide: [[number, number, number], [number, number, number]] | null
}

/** Build the proposal in the same frame-local coordinates used by MMSP. */
export function poleBasePreviewGeometry(
  seed: [number, number, number],
  result: PoleBaseInferResponse | null,
  frameOrigin: [number, number, number],
): PoleBasePreviewGeometry {
  const local = (point: [number, number, number]): [number, number, number] => [
    point[0] - frameOrigin[0],
    point[1] - frameOrigin[1],
    point[2] - frameOrigin[2],
  ]
  const localSeed = local(seed)
  const localBase = result?.base_position ? local(result.base_position) : null
  const axis = result?.axis
  let localAxis: PoleBasePreviewGeometry['axis'] = null
  if (axis) {
    const direction = new THREE.Vector3(...axis.direction)
    const point = new THREE.Vector3(...axis.point)
    let start: THREE.Vector3
    let end: THREE.Vector3
    if (Math.abs(direction.z) > 1e-8) {
      start = point.clone().addScaledVector(
        direction,
        (axis.observed_z_min - point.z) / direction.z,
      )
      end = point.clone().addScaledVector(
        direction,
        (axis.observed_z_max - point.z) / direction.z,
      )
    } else {
      const halfSpan = Math.max(0.5, axis.vertical_span_m / 2)
      start = point.clone().addScaledVector(direction.clone().normalize(), -halfSpan)
      end = point.clone().addScaledVector(direction.clone().normalize(), halfSpan)
    }
    localAxis = [
      local(start.toArray() as [number, number, number]),
      local(end.toArray() as [number, number, number]),
    ]
  }
  return {
    seed: localSeed,
    base: localBase,
    axis: localAxis,
    guide: localBase ? [localSeed, localBase] : null,
  }
}

export function poleBaseStatusLabel(status: PoleBaseInferResponse['status']): string {
  if (status === 'auto') return '자동 산출 가능'
  if (status === 'review') return '검토 필요'
  return '산출 실패'
}

export function poleBasePrimaryWarning(result: PoleBaseInferResponse): string | null {
  const knownReason = result.reason_codes.find((entry) => POLE_BASE_REASON_MESSAGES[entry])
  if (knownReason) return poleBaseReasonMessage(knownReason)
  const warning = result.warnings.find((entry) => entry.trim())
  if (warning) return warning
  const reason = result.reason_codes.find((entry) => entry.trim())
  if (reason) return poleBaseReasonMessage(reason)
  if (result.status === 'review') return '자동 품질 기준을 일부 통과하지 못했습니다.'
  return null
}

export async function applyPointCloudPickedCoordinate(
  target: OverlayPickTarget,
  frameId: string,
  coordinates: [number, number, number],
  actions: {
    applyPointCloudCoordinate: (
      frameId: string,
      coordinates: [number, number, number],
    ) => Promise<void>
    applyPoleSeed: (
      frameId: string,
      coordinates: [number, number, number],
    ) => Promise<void>
  },
): Promise<void> {
  if (target.kind === 'pole-base-create' || target.kind === 'pole-base-move') {
    await actions.applyPoleSeed(frameId, coordinates)
    return
  }
  await actions.applyPointCloudCoordinate(frameId, coordinates)
}

export function pointCloudPickTargetAcceptsPoint(
  target: OverlayPickTarget | null | undefined,
  poleBaseStatus: PoleBaseProposalState['status'],
): boolean {
  if (target?.kind === 'move' || target?.kind === 'create') return true
  return Boolean(
    (target?.kind === 'pole-base-create' || target?.kind === 'pole-base-move') &&
      poleBaseStatus === 'picking',
  )
}

export function captureHeadingDirection(heading: number | null | undefined): [number, number, number] {
  const radians = ((Number.isFinite(heading) ? Number(heading) : 0) * Math.PI) / 180
  return [Math.sin(radians), Math.cos(radians), 0]
}

interface PointHitCandidate {
  index?: number
  point: THREE.Vector3
  distance: number
  object?: THREE.Object3D
}

function actualPointHitPosition(
  candidate: PointHitCandidate,
  target: THREE.Vector3,
): THREE.Vector3 {
  const points = candidate.object as THREE.Points<THREE.BufferGeometry> | undefined
  const attribute = points?.geometry?.getAttribute?.('position')
  if (
    points &&
    candidate.index !== undefined &&
    attribute &&
    candidate.index >= 0 &&
    candidate.index < attribute.count
  ) {
    target.fromBufferAttribute(attribute, candidate.index)
    return target.applyMatrix4(points.matrixWorld)
  }
  // Keep the helper usable for lightweight test candidates and non-Points
  // callers. Three.Points intersections use the geometry branch above because
  // Intersection.point is the closest position *on the ray*, not the vertex.
  return target.copy(candidate.point)
}

/**
 * Three.js sorts Points ray hits by camera depth. For dense clouds that can
 * choose a nearer point several pixels away from the pointer instead of the
 * visible point directly under it. Rank the bounded ray candidates in screen
 * space first, then use depth only to resolve an actual pixel tie.
 */
export function closestPointHitIndex(
  candidates: readonly PointHitCandidate[],
  pointerNdc: { x: number; y: number },
  camera: THREE.Camera,
  viewportWidth: number,
  viewportHeight: number,
  maximumPixelDistance = 7,
): number | null {
  if (
    !Number.isFinite(pointerNdc.x) ||
    !Number.isFinite(pointerNdc.y) ||
    !Number.isFinite(viewportWidth) ||
    !Number.isFinite(viewportHeight) ||
    !Number.isFinite(maximumPixelDistance) ||
    viewportWidth <= 0 ||
    viewportHeight <= 0 ||
    maximumPixelDistance <= 0
  ) {
    return null
  }
  const projected = new THREE.Vector3()
  const maximumSquared = maximumPixelDistance * maximumPixelDistance
  let selectedIndex: number | null = null
  let selectedScreenDistance = maximumSquared
  let selectedDepth = Number.POSITIVE_INFINITY
  candidates.forEach((candidate) => {
    if (candidate.index === undefined) return
    actualPointHitPosition(candidate, projected).project(camera)
    if (!Number.isFinite(projected.x) || !Number.isFinite(projected.y) || projected.z < -1 || projected.z > 1) {
      return
    }
    const deltaX = ((projected.x - pointerNdc.x) * viewportWidth) / 2
    const deltaY = ((projected.y - pointerNdc.y) * viewportHeight) / 2
    const screenDistance = deltaX * deltaX + deltaY * deltaY
    const closerOnScreen = screenDistance < selectedScreenDistance - 1e-9
    const sameScreenDistance = Math.abs(screenDistance - selectedScreenDistance) <= 1e-9
    if (
      screenDistance <= maximumSquared &&
      (closerOnScreen || (sameScreenDistance && candidate.distance < selectedDepth))
    ) {
      selectedIndex = candidate.index
      selectedScreenDistance = screenDistance
      selectedDepth = candidate.distance
    }
  })
  return selectedIndex
}

export default function PointCloudView({
  datasetId,
  frame,
  demoMode,
  maxPointBudget = 1_000_000,
  detectionRevisionKey = '',
  poleBaseMarkerColor = DEFAULT_USER_SETTINGS.poleBaseMarkerColor,
  poleBaseMarkerSizeM = DEFAULT_USER_SETTINGS.poleBaseMarkerSizeM,
  onHoverPanoramaPoint,
}: {
  datasetId: string
  frame: Frame | null
  demoMode: boolean
  maxPointBudget?: number
  detectionRevisionKey?: string
  poleBaseMarkerColor?: string
  poleBaseMarkerSizeM?: number
  onHoverPanoramaPoint?: (point: PanoramaHoverProjection | null) => void
}) {
  const overlay = useOptionalOverlayWorkspace()
  const manualObject = useOptionalManualObjectWorkspace()
  const hostRef = useRef<HTMLDivElement>(null)
  const pointMaterialRef = useRef<THREE.PointsMaterial | null>(null)
  const renderSceneRef = useRef<THREE.Scene | null>(null)
  const wakeRenderSceneRef = useRef<(() => void) | null>(null)
  const viewStateRef = useRef<PointCloudViewState | null>(null)
  const viewResetRequestedRef = useRef(false)
  const viewScopeRef = useRef(`${demoMode}:${datasetId}`)
  const pointSizeRef = useRef(1.4)
  const projectionMetadataRef = useRef<PanoramaProjectionMetadata | null>(null)
  const onHoverPanoramaPointRef = useRef(onHoverPanoramaPoint)
  const availableBudgets = useMemo(
    () => pointCloudBudgetsForMaximum(maxPointBudget),
    [maxPointBudget],
  )
  const maximumAvailableBudget = availableBudgets.at(-1)?.value ?? DEFAULT_POINT_CLOUD_BUDGET
  const [payload, setPayload] = useState<PointCloudPayload | null>(null)
  const [budget, setBudget] = useState(() =>
    Math.min(DEFAULT_POINT_CLOUD_BUDGET, maximumAvailableBudget),
  )
  const [pointSize, setPointSize] = useState(1.4)
  const [loading, setLoading] = useState(false)
  const [indexing, setIndexing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  const [nearbyOverlayFeatures, setNearbyOverlayFeatures] = useState<NearbyOverlayFeature[]>([])
  const [detectionObservations, setDetectionObservations] = useState<PanoramaDetectionBoxObservation[]>([])
  const [detectionLoading, setDetectionLoading] = useState(false)
  const [detectionError, setDetectionError] = useState<string | null>(null)
  const [nearbyOverlayTotal, setNearbyOverlayTotal] = useState(0)
  const [overlayLoading, setOverlayLoading] = useState(false)
  const [overlayError, setOverlayError] = useState<string | null>(null)
  const [overlayHover, setOverlayHover] = useState<OverlayHoverState | null>(null)
  const [pinnedOverlayHover, setPinnedOverlayHover] = useState<OverlayHoverState | null>(null)
  const [colorMode, setColorMode] = useState<PointCloudColorMode>('rgb')
  const [clipEnabled, setClipEnabled] = useState(false)
  const [clipRadiusM, setClipRadiusM] = useState(8)
  const [clipRadiusDraftM, setClipRadiusDraftM] = useState(8)
  const [zSliceEnabled, setZSliceEnabled] = useState(false)
  const [zMinimum, setZMinimum] = useState(-5)
  const [zMaximum, setZMaximum] = useState(10)
  const [zMinimumDraft, setZMinimumDraft] = useState(-5)
  const [zMaximumDraft, setZMaximumDraft] = useState(10)
  const [isolateProposal, setIsolateProposal] = useState(false)
  const [measureMode, setMeasureMode] = useState(false)
  const [measurementPoints, setMeasurementPoints] = useState<Array<[number, number, number]>>([])
  const [renderSceneGeneration, setRenderSceneGeneration] = useState(0)
  const [toolSettingsDatasetId, setToolSettingsDatasetId] = useState('')
  const measureModeRef = useRef(measureMode)
  measureModeRef.current = measureMode

  useEffect(() => {
    setToolSettingsDatasetId('')
    setBudget((current) => Math.min(current, maximumAvailableBudget))
  }, [maximumAvailableBudget])
  useEffect(() => {
    setColorMode('rgb')
    setClipEnabled(false)
    setClipRadiusM(8)
    setClipRadiusDraftM(8)
    setZSliceEnabled(false)
    setZMinimum(-5)
    setZMaximum(10)
    setZMinimumDraft(-5)
    setZMaximumDraft(10)
    try {
      const stored = JSON.parse(window.localStorage.getItem(`mms.pointcloud-tools:${datasetId}`) ?? '{}') as Record<string, unknown>
      if (['rgb', 'intensity', 'classification', 'height'].includes(String(stored.colorMode))) {
        setColorMode(stored.colorMode as PointCloudColorMode)
      }
      if (typeof stored.clipEnabled === 'boolean') setClipEnabled(stored.clipEnabled)
      if (Number.isFinite(stored.clipRadiusM)) {
        const radius = Math.min(25, Math.max(1, Number(stored.clipRadiusM)))
        setClipRadiusM(radius)
        setClipRadiusDraftM(radius)
      }
      if (typeof stored.zSliceEnabled === 'boolean') setZSliceEnabled(stored.zSliceEnabled)
      if (Number.isFinite(stored.zMinimum)) {
        setZMinimum(Number(stored.zMinimum))
        setZMinimumDraft(Number(stored.zMinimum))
      }
      if (Number.isFinite(stored.zMaximum)) {
        setZMaximum(Number(stored.zMaximum))
        setZMaximumDraft(Number(stored.zMaximum))
      }
    } catch {
      // Local viewer settings are optional.
    }
    setIsolateProposal(false)
    setMeasureMode(false)
    setMeasurementPoints([])
    setToolSettingsDatasetId(datasetId)
  }, [datasetId])
  useEffect(() => {
    setMeasureMode(false)
    setMeasurementPoints([])
  }, [frame?.id])
  useEffect(() => {
    if (toolSettingsDatasetId !== datasetId) return
    try {
      window.localStorage.setItem(`mms.pointcloud-tools:${datasetId}`, JSON.stringify({
        colorMode,
        clipEnabled,
        clipRadiusM,
        zSliceEnabled,
        zMinimum,
        zMaximum,
      }))
    } catch {
      // Local viewer settings are optional.
    }
  }, [clipEnabled, clipRadiusM, colorMode, datasetId, toolSettingsDatasetId, zMaximum, zMinimum, zSliceEnabled])
  useEffect(() => {
    if (!payload) return
    if (!zSliceEnabled) {
      setZMinimum(payload.bounds.min[2])
      setZMaximum(payload.bounds.max[2])
      setZMinimumDraft(payload.bounds.min[2])
      setZMaximumDraft(payload.bounds.max[2])
    }
  }, [payload, zSliceEnabled])
  const overlayHoverRef = useRef<OverlayHoverState | null>(null)
  const selectedOverlay = overlay?.selected
  const poleBaseProposal = overlay?.poleBaseProposal
  const manualProposalLocalPosition = useMemo(
    () => datasetPointToFrameLocal(
      manualObject?.proposalPosition ?? null,
      frame?.dataset_position,
    ),
    [frame?.dataset_position, manualObject?.proposalPosition],
  )
  const selectedOverlayLocalPosition = useMemo(() => {
    const feature = overlay?.selectedDatasetFeature
    if (feature?.geometry?.type !== 'Point') return null
    return datasetPointToFrameLocal(feature.geometry.coordinates, frame?.dataset_position)
  }, [frame?.dataset_position, overlay?.selectedDatasetFeature])
  const displayOptions = useMemo<PointCloudDisplayOptions>(() => ({
      clipRadiusM: clipEnabled ? clipRadiusM : null,
      clipCenter: selectedOverlayLocalPosition
        ? [selectedOverlayLocalPosition[0], selectedOverlayLocalPosition[1]]
        : [0, 0],
      zRange: zSliceEnabled ? [Math.min(zMinimum, zMaximum), Math.max(zMinimum, zMaximum)] : null,
      proposalPosition: manualProposalLocalPosition,
      isolateProposal: isolateProposal && Boolean(manualProposalLocalPosition),
      proposalRadiusM: Math.min(clipRadiusM, 4),
    }),
    [clipEnabled, clipRadiusM, isolateProposal, manualProposalLocalPosition, selectedOverlayLocalPosition, zMaximum, zMinimum, zSliceEnabled],
  )
  const deferredDisplayOptions = useDeferredValue(displayOptions)
  const displayPayload = useMemo(
    () => payload ? buildPointCloudDisplayPayload(payload, deferredDisplayOptions) : null,
    [deferredDisplayOptions, payload],
  )
  const measurement = measurementPoints.length === 2
    ? pointCloudMeasurement(measurementPoints[0], measurementPoints[1])
    : null
  useEffect(() => {
    if (!manualProposalLocalPosition) setIsolateProposal(false)
  }, [manualProposalLocalPosition])
  const poleBasePicking = poleBaseProposal?.status === 'picking'
  const coordinatePickActive = pointCloudPickTargetAcceptsPoint(
    overlay?.pickTarget,
    poleBaseProposal?.status ?? 'idle',
  )
  const visibleOverlayLayers = useMemo(
    () => (overlay?.layers ?? []).filter((layer) => overlay?.visibleLayerIds.has(layer.id)),
    [overlay?.layers, overlay?.visibleLayerIds],
  )
  const visibleOverlayLayerKey = visibleOverlayLayers
    .map((layer) => `${layer.id}:${layer.revision}`)
    .join('|')
  const overlayLayerColor = overlay?.layerColor
  const overlayActionsRef = useRef({
    pickTarget: null as OverlayPickTarget | null,
    selectFeature: (
      _selection: { layerId: string; featureId: string | number } | null,
      _options?: { navigate?: boolean },
    ) => {},
    applyPointCloudCoordinate: async (
      _frameId: string,
      _coordinates: [number, number, number],
    ) => {},
    applyPoleSeed: async (
      _frameId: string,
      _coordinates: [number, number, number],
    ) => {},
  })
  overlayActionsRef.current = {
    pickTarget: overlay?.pickTarget ?? null,
    selectFeature: overlay?.selectFeature ?? (() => {}),
    applyPointCloudCoordinate: overlay?.applyPointCloudCoordinate ?? (async () => {}),
    applyPoleSeed: overlay?.applyPoleSeed ?? (async () => {}),
  }

  useEffect(() => {
    if (frame?.id) overlay?.handlePoleBaseFrameChange(frame.id)
  }, [frame?.id, overlay?.handlePoleBaseFrameChange])

  const viewScope = `${demoMode}:${datasetId}`
  if (viewScopeRef.current !== viewScope) {
    viewScopeRef.current = viewScope
    viewStateRef.current = null
  }
  pointSizeRef.current = pointSize
  onHoverPanoramaPointRef.current = onHoverPanoramaPoint
  overlayHoverRef.current = overlayHover

  useEffect(() => {
    projectionMetadataRef.current = null
    onHoverPanoramaPointRef.current?.(null)
    setPinnedOverlayHover(null)
    if (!frame) return
    if (demoMode) {
      projectionMetadataRef.current = demoPanoramaProjectionMetadata(frame)
      return () => {
        projectionMetadataRef.current = null
        onHoverPanoramaPointRef.current?.(null)
      }
    }
    const controller = new AbortController()
    void api
      .panoramaProjectionMetadata(datasetId, frame.id, controller.signal)
      .then((metadata) => {
        if (!controller.signal.aborted && metadata.frame_id === frame.id) {
          projectionMetadataRef.current = metadata
        }
      })
      .catch(() => {
        // Point preview remains usable when a frame has no valid camera basis.
      })
    return () => {
      controller.abort()
      projectionMetadataRef.current = null
      onHoverPanoramaPointRef.current?.(null)
    }
  }, [datasetId, demoMode, frame])

  useEffect(() => {
    const origin = frame?.dataset_position
    if (!origin || demoMode || !visibleOverlayLayers.length) {
      setNearbyOverlayFeatures([])
      setNearbyOverlayTotal(0)
      setOverlayLoading(false)
      setOverlayError(null)
      return
    }
    const controller = new AbortController()
    setNearbyOverlayFeatures([])
    setNearbyOverlayTotal(0)
    setOverlayLoading(true)
    setOverlayError(null)
    const loadNearby = async () => {
      const groups: NearbyOverlayFeature[][] = []
      const totals: number[] = []
      const errors: unknown[] = []
      let nextIndex = 0
      const worker = async () => {
        while (!controller.signal.aborted) {
          const index = nextIndex
          nextIndex += 1
          const layer = visibleOverlayLayers[index]
          if (!layer) return
          try {
            const page = await api.overlaySpatialFeatures(
              datasetId,
              layer.id,
              [origin[0], origin[1]],
              POINT_PREVIEW_MAX_RADIUS_M,
              5_000,
              controller.signal,
            )
            totals[index] = page.total
            const color = overlayLayerColor?.(layer.id) ?? '#ffb84d'
            groups[index] = page.features.map((feature) => ({
              layerId: layer.id,
              layerName: layer.name,
              color,
              feature,
            }))
          } catch (reason) {
            if (!controller.signal.aborted) errors.push(reason)
            totals[index] = 0
            groups[index] = []
          }
        }
      }
      await Promise.all(
        Array.from({ length: Math.min(4, visibleOverlayLayers.length) }, () => worker()),
      )
      if (controller.signal.aborted) return
      setNearbyOverlayFeatures(groups.flat())
      setNearbyOverlayTotal(totals.reduce((sum, value) => sum + (value ?? 0), 0))
      if (errors.length) {
        setOverlayError(
          errors.length === visibleOverlayLayers.length
            ? errors[0] instanceof Error
              ? errors[0].message
              : '주변 SHP 포인트를 불러오지 못했습니다.'
            : `일부 SHP 레이어(${errors.length}개)를 불러오지 못했습니다.`,
        )
      }
    }
    void loadNearby().finally(() => {
      if (!controller.signal.aborted) setOverlayLoading(false)
    })
    return () => controller.abort()
  }, [
    datasetId,
    demoMode,
    frame?.dataset_position,
    frame?.id,
    overlayLayerColor,
    visibleOverlayLayerKey,
    visibleOverlayLayers,
  ])

  const overlayPoints = useMemo<RenderOverlayPoint[]>(() => {
    const origin = frame?.dataset_position
    if (!origin) return []
    const maximumDistanceSquared = POINT_PREVIEW_MAX_RADIUS_M ** 2
    return nearbyOverlayFeatures.flatMap(({ layerId, layerName, color, feature }) => {
      if (feature.geometry?.type !== 'Point') return []
      const position = datasetPointToFrameLocal(feature.geometry.coordinates, origin)
      if (!position || position[0] ** 2 + position[1] ** 2 > maximumDistanceSquared) return []
      return [{
        layerId,
        layerName,
        featureId: feature.id,
        color,
        position,
        properties: feature.properties,
        selected:
          selectedOverlay?.layerId === layerId &&
          String(selectedOverlay.featureId) === String(feature.id),
      }]
    })
  }, [frame?.dataset_position, nearbyOverlayFeatures, selectedOverlay])

  useEffect(() => {
    if (!frame || demoMode) {
      setDetectionObservations([])
      setDetectionLoading(false)
      setDetectionError(null)
      return
    }
    const controller = new AbortController()
    setDetectionObservations([])
    setDetectionLoading(true)
    setDetectionError(null)
    void api.frameDetections(datasetId, frame.id, controller.signal)
      .then((response) => {
        if (!controller.signal.aborted) setDetectionObservations(response.items)
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) {
          setDetectionError(
            reason instanceof Error ? reason.message : 'YOLO 3D 위치를 불러오지 못했습니다.',
          )
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setDetectionLoading(false)
      })
    return () => controller.abort()
  }, [datasetId, demoMode, detectionRevisionKey, frame])

  const detectionPoints = useMemo(
    () => pointCloudDetectionsFromObservations(
      detectionObservations,
      frame?.dataset_position,
      overlayPoints,
    ),
    [detectionObservations, frame?.dataset_position, overlayPoints],
  )

  useEffect(() => {
    if (!frame) {
      setPayload(null)
      return
    }
    const controller = new AbortController()
    let retryTimer: number | undefined
    let attempts = 0
    setPayload(null)
    setLoading(true)
    setIndexing(false)
    setError(null)

    const load = async () => {
      try {
        const data = demoMode
          ? createDemoPointCloud(budget)
          : parseMmsp(await api.points(datasetId, frame.id, budget, controller.signal, colorMode))
        if (!controller.signal.aborted) {
          setPayload(data)
          setLoading(false)
          setIndexing(false)
        }
      } catch (reason) {
        if (controller.signal.aborted) return
        if (reason instanceof ApiError && reason.status === 202 && attempts < 8) {
          attempts += 1
          setIndexing(true)
          retryTimer = window.setTimeout(load, Math.min(8_000, 1_200 * attempts))
          return
        }
        setError(reason instanceof Error ? reason.message : '포인트 데이터를 불러오지 못했습니다.')
        setLoading(false)
      }
    }
    void load()
    return () => {
      controller.abort()
      if (retryTimer) window.clearTimeout(retryTimer)
    }
  }, [budget, colorMode, datasetId, demoMode, frame, reloadKey])

  useEffect(() => {
    const host = hostRef.current
    if (!host || !displayPayload) return
    const ownerDocument = host.ownerDocument
    const ownerWindow = pointCloudOwnerWindow(host)
    const ownerPixelRatio = () => {
      const ratio = ownerWindow.devicePixelRatio
      return Number.isFinite(ratio) && ratio > 0 ? Math.min(ratio, 1.75) : 1
    }
    const initialWidth = Math.max(1, host.clientWidth)
    const initialHeight = Math.max(1, host.clientHeight)

    const scene = new THREE.Scene()
    scene.background = new THREE.Color(0x07111f)
    scene.fog = new THREE.FogExp2(0x07111f, 0.009)

    const camera = new THREE.PerspectiveCamera(52, initialWidth / initialHeight, 0.05, 2000)
    camera.up.set(0, 0, 1)
    const spanX = displayPayload.bounds.max[0] - displayPayload.bounds.min[0]
    const spanY = displayPayload.bounds.max[1] - displayPayload.bounds.min[1]
    const span = Math.max(20, spanX, spanY)
    const savedView = viewStateRef.current
    if (!savedView) {
      camera.position.set(span * 0.38, -span * 0.52, span * 0.32)
    }

    let renderer: THREE.WebGLRenderer
    try {
      renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: 'high-performance' })
    } catch {
      setError('이 브라우저에서 WebGL을 시작할 수 없습니다. 그래픽 가속 설정을 확인해 주세요.')
      return
    }
    renderer.setPixelRatio(ownerPixelRatio())
    renderer.setSize(initialWidth, initialHeight)
    renderer.outputColorSpace = THREE.SRGBColorSpace
    host.appendChild(renderer.domElement)

    const controls = new OrbitControls(camera, renderer.domElement)
    controls.enableDamping = true
    controls.dampingFactor = 0.07
    controls.target.set(
      (displayPayload.bounds.min[0] + displayPayload.bounds.max[0]) / 2,
      (displayPayload.bounds.min[1] + displayPayload.bounds.max[1]) / 2,
      (displayPayload.bounds.min[2] + displayPayload.bounds.max[2]) / 2,
    )
    if (savedView) restorePointCloudViewState(camera, controls.target, savedView)
    controls.update()

    const rememberView = () => {
      viewStateRef.current = capturePointCloudViewState(camera, controls.target)
    }
    controls.addEventListener('change', rememberView)

    const geometry = new THREE.BufferGeometry()
    geometry.setAttribute('position', new THREE.BufferAttribute(displayPayload.positions, 3))
    if (displayPayload.colors) {
      geometry.setAttribute('color', new THREE.Uint8BufferAttribute(displayPayload.colors, 3, true))
    }
    geometry.computeBoundingSphere()

    const material = new THREE.PointsMaterial({
      size: pointSizeRef.current * 0.045,
      sizeAttenuation: true,
      vertexColors: Boolean(displayPayload.colors),
      color: displayPayload.colors ? 0xffffff : 0x69e0be,
      transparent: true,
      opacity: 0.94,
    })
    pointMaterialRef.current = material
    const points = new THREE.Points(geometry, material)
    scene.add(points)

    // MMSP coordinates are frame-local, so the origin is the physical capture
    // position. Show both that origin and the GNSS heading to make the current
    // acquisition pose unambiguous while orbiting the cloud.
    const capturePose = new THREE.Group()
    capturePose.name = 'current-capture-pose'
    const poseOrigin = new THREE.Vector3(0, 0, 0.35)
    const markerMaterial = new THREE.MeshBasicMaterial({
      color: 0xffd166,
      depthTest: false,
      transparent: true,
      opacity: 0.98,
    })
    const marker = new THREE.Mesh(new THREE.SphereGeometry(0.24, 18, 12), markerMaterial)
    marker.position.copy(poseOrigin)
    marker.renderOrder = 20
    capturePose.add(marker)
    const ringMaterial = new THREE.MeshBasicMaterial({
      color: 0xffffff,
      depthTest: false,
      transparent: true,
      opacity: 0.9,
    })
    const ring = new THREE.Mesh(new THREE.TorusGeometry(0.44, 0.045, 8, 28), ringMaterial)
    ring.position.copy(poseOrigin)
    ring.renderOrder = 19
    capturePose.add(ring)
    const direction = new THREE.Vector3(...captureHeadingDirection(frame?.heading)).normalize()
    const arrow = new THREE.ArrowHelper(
      direction,
      poseOrigin,
      Math.min(7, Math.max(3, span * 0.12)),
      0xffd166,
      0.75,
      0.42,
    )
    arrow.traverse((object) => {
      object.renderOrder = 20
      const renderable = object as THREE.Object3D & { material?: THREE.Material | THREE.Material[] }
      const materials = Array.isArray(renderable.material)
        ? renderable.material
        : renderable.material
          ? [renderable.material]
          : []
      materials.forEach((entry) => {
        entry.depthTest = false
        entry.transparent = true
      })
    })
    capturePose.add(arrow)
    scene.add(capturePose)

    let overlayGeometry: THREE.BufferGeometry | null = null
    let overlayMaterial: THREE.PointsMaterial | null = null
    let overlayObject: THREE.Points | null = null
    let selectedGeometry: THREE.BufferGeometry | null = null
    let selectedMaterial: THREE.PointsMaterial | null = null
    let detectionGeometry: THREE.BufferGeometry | null = null
    let detectionMaterial: THREE.PointsMaterial | null = null
    let detectionObject: THREE.Points | null = null
    let detectionBoxGeometry: THREE.BufferGeometry | null = null
    let detectionBoxMaterial: THREE.LineBasicMaterial | null = null
    let detectionBoxObject: THREE.LineSegments | null = null
    if (overlayPoints.length) {
      const positions = new Float32Array(overlayPoints.length * 3)
      const colors = new Float32Array(overlayPoints.length * 3)
      overlayPoints.forEach((entry, index) => {
        positions.set(entry.position, index * 3)
        const color = new THREE.Color(entry.color)
        colors.set([color.r, color.g, color.b], index * 3)
      })
      overlayGeometry = new THREE.BufferGeometry()
      overlayGeometry.setAttribute('position', new THREE.BufferAttribute(positions, 3))
      overlayGeometry.setAttribute('color', new THREE.BufferAttribute(colors, 3))
      overlayMaterial = new THREE.PointsMaterial({
        size: pointCloudOverlayPointSize(false),
        sizeAttenuation: true,
        vertexColors: true,
        depthTest: false,
      })
      overlayObject = new THREE.Points(overlayGeometry, overlayMaterial)
      overlayObject.renderOrder = 4
      scene.add(overlayObject)

      const selectedEntry = overlayPoints.find((entry) => entry.selected)
      if (selectedEntry) {
        selectedGeometry = new THREE.BufferGeometry()
        selectedGeometry.setAttribute(
          'position',
          new THREE.Float32BufferAttribute(selectedEntry.position, 3),
        )
        selectedMaterial = new THREE.PointsMaterial({
          size: pointCloudOverlayPointSize(true),
          sizeAttenuation: true,
          color: 0xffffff,
          depthTest: false,
        })
        const selectedObject = new THREE.Points(selectedGeometry, selectedMaterial)
        selectedObject.renderOrder = 5
        scene.add(selectedObject)
      }
    }

    if (detectionPoints.length) {
      const positions = new Float32Array(detectionPoints.length * 3)
      const colors = new Float32Array(detectionPoints.length * 3)
      const boxColors = new Float32Array(
        detectionPoints.length * DETECTION_BOX_EDGE_INDICES.length * 3,
      )
      detectionPoints.forEach((entry, index) => {
        positions.set(entry.position, index * 3)
        const color = new THREE.Color(entry.selected ? '#ffffff' : entry.color)
        colors.set([color.r, color.g, color.b], index * 3)
        for (let vertex = 0; vertex < DETECTION_BOX_EDGE_INDICES.length; vertex += 1) {
          boxColors.set(
            [color.r, color.g, color.b],
            (index * DETECTION_BOX_EDGE_INDICES.length + vertex) * 3,
          )
        }
      })
      detectionBoxGeometry = new THREE.BufferGeometry()
      detectionBoxGeometry.setAttribute(
        'position',
        new THREE.BufferAttribute(pointCloudDetectionWireframePositions(detectionPoints), 3),
      )
      detectionBoxGeometry.setAttribute('color', new THREE.BufferAttribute(boxColors, 3))
      detectionBoxMaterial = new THREE.LineBasicMaterial({
        vertexColors: true,
        depthTest: false,
        transparent: true,
        opacity: 0.95,
      })
      detectionBoxObject = new THREE.LineSegments(detectionBoxGeometry, detectionBoxMaterial)
      detectionBoxObject.renderOrder = 8
      detectionGeometry = new THREE.BufferGeometry()
      detectionGeometry.setAttribute('position', new THREE.BufferAttribute(positions, 3))
      detectionGeometry.setAttribute('color', new THREE.BufferAttribute(colors, 3))
      detectionMaterial = new THREE.PointsMaterial({
        size: POINT_CLOUD_YOLO_MARKER_SIZE,
        sizeAttenuation: true,
        vertexColors: true,
        depthTest: false,
      })
      detectionObject = new THREE.Points(detectionGeometry, detectionMaterial)
      detectionObject.renderOrder = 9
      scene.add(detectionBoxObject)
      scene.add(detectionObject)
    }

    let poleBaseGroup: THREE.Group | null = null
    if (
      frame?.dataset_position &&
      poleBaseProposal &&
      poleBaseProposal.status !== 'idle' &&
      poleBaseProposal.status !== 'picking' &&
      poleBaseProposal.frameId === frame.id &&
      poleBaseProposal.seed
    ) {
      const preview = poleBasePreviewGeometry(
        poleBaseProposal.seed,
        poleBaseProposal.status === 'ready' ? poleBaseProposal.result : null,
        frame.dataset_position,
      )
      poleBaseGroup = new THREE.Group()
      poleBaseGroup.name = 'pole-base-proposal'

      const addMarker = (
        name: string,
        position: [number, number, number],
        color: number,
        radius: number,
      ) => {
        const markerGeometry = new THREE.SphereGeometry(radius, 18, 12)
        const markerMaterial = new THREE.MeshBasicMaterial({
          color,
          depthTest: false,
          transparent: true,
          opacity: 0.98,
        })
        const proposalMarker = new THREE.Mesh(markerGeometry, markerMaterial)
        proposalMarker.name = name
        proposalMarker.position.fromArray(position)
        proposalMarker.renderOrder = 31
        poleBaseGroup?.add(proposalMarker)
      }
      const addLine = (
        name: string,
        segment: [[number, number, number], [number, number, number]],
        color: number,
        dashed = false,
      ) => {
        const lineGeometry = new THREE.BufferGeometry().setFromPoints(
          segment.map((point) => new THREE.Vector3(...point)),
        )
        const lineMaterial = dashed
          ? new THREE.LineDashedMaterial({
              color,
              depthTest: false,
              transparent: true,
              opacity: 0.9,
              dashSize: 0.16,
              gapSize: 0.1,
            })
          : new THREE.LineBasicMaterial({
              color,
              depthTest: false,
              transparent: true,
              opacity: 0.98,
            })
        const proposalLine = new THREE.Line(lineGeometry, lineMaterial)
        proposalLine.name = name
        proposalLine.renderOrder = 30
        if (dashed) proposalLine.computeLineDistances()
        poleBaseGroup?.add(proposalLine)
      }

      addMarker('pole-base-seed', preview.seed, 0xffc857, 0.17)
      if (preview.axis) addLine('pole-base-axis', preview.axis, 0xff6f91)
      if (preview.guide) addLine('pole-base-guide', preview.guide, 0xffc857, true)
      if (preview.base) {
        addMarker(
          'pole-base-result',
          preview.base,
          new THREE.Color(poleBaseMarkerColor).getHex(),
          poleBaseMarkerSizeM,
        )
      }
      scene.add(poleBaseGroup)
    }

    let manualProposalGroup: THREE.Group | null = null
    if (manualProposalLocalPosition) {
      manualProposalGroup = new THREE.Group()
      manualProposalGroup.name = 'manual-object-proposal'
      const material = new THREE.MeshBasicMaterial({
        color: 0xffb84d,
        depthTest: false,
        transparent: true,
        opacity: 0.98,
      })
      const marker = new THREE.Mesh(new THREE.SphereGeometry(0.28, 18, 12), material)
      marker.position.fromArray(manualProposalLocalPosition)
      marker.renderOrder = 34
      manualProposalGroup.add(marker)
      const ring = new THREE.Mesh(
        new THREE.TorusGeometry(0.48, 0.055, 8, 28),
        new THREE.MeshBasicMaterial({ color: 0xffffff, depthTest: false }),
      )
      ring.position.fromArray(manualProposalLocalPosition)
      ring.renderOrder = 33
      manualProposalGroup.add(ring)
      scene.add(manualProposalGroup)
    }

    const grid = new THREE.GridHelper(Math.ceil(span * 1.4), 24, 0x2bcfa8, 0x213548)
    grid.rotation.x = Math.PI / 2
    grid.position.z = displayPayload.bounds.min[2] - 0.1
    ;(grid.material as THREE.Material).opacity = 0.22
    ;(grid.material as THREE.Material).transparent = true
    scene.add(grid)

    const render = () => {
      controls.update()
      renderer.render(scene, camera)
    }

    let rendererDisposed = false
    const syncViewport = () => {
      if (rendererDisposed) return false
      const width = host.clientWidth
      const height = host.clientHeight
      if (width <= 0 || height <= 0) return false
      renderer.setPixelRatio(ownerPixelRatio())
      camera.aspect = width / height
      camera.updateProjectionMatrix()
      renderer.setSize(width, height)
      return true
    }
    const renderLoop = createPointCloudRenderLoop(
      ownerWindow,
      render,
      () => ownerDocument.visibilityState !== 'hidden',
    )
    const wakeRenderer = () => {
      if (syncViewport()) renderLoop.wake()
      else renderLoop.stop()
    }

    const ResizeObserverConstructor = (
      ownerWindow as Window & typeof globalThis
    ).ResizeObserver ?? ResizeObserver
    const resizeObserver = new ResizeObserverConstructor(() => {
      wakeRenderer()
    })
    resizeObserver.observe(host)
    const onVisibilityChange = () => {
      if (ownerDocument.visibilityState === 'hidden') renderLoop.stop()
      else wakeRenderer()
    }
    const onPageHide = () => renderLoop.stop()
    const onContextLost = (event: Event) => {
      event.preventDefault()
      renderLoop.suspend()
      setError('그래픽 컨텍스트가 중단되어 복구를 기다리고 있습니다.')
    }
    const onContextRestored = () => {
      syncViewport()
      setError(null)
      renderLoop.resume()
    }
    ownerWindow.addEventListener('resize', wakeRenderer)
    ownerWindow.addEventListener('focus', wakeRenderer)
    ownerWindow.addEventListener('pageshow', wakeRenderer)
    ownerWindow.addEventListener('pagehide', onPageHide)
    ownerDocument.addEventListener('visibilitychange', onVisibilityChange)
    renderer.domElement.addEventListener('webglcontextlost', onContextLost)
    renderer.domElement.addEventListener('webglcontextrestored', onContextRestored)
    wakeRenderer()
    renderSceneRef.current = scene
    wakeRenderSceneRef.current = wakeRenderer
    setRenderSceneGeneration((value) => value + 1)
    const pointRaycaster = new THREE.Raycaster()
    pointRaycaster.params.Points = { threshold: 0.18 }
    const overlayRaycaster = new THREE.Raycaster()
    overlayRaycaster.params.Points = { threshold: 0.48 }
    const detectionRaycaster = new THREE.Raycaster()
    detectionRaycaster.params.Points = { threshold: POINT_CLOUD_YOLO_RAYCAST_THRESHOLD }
    const pointer = new THREE.Vector2()
    let pointerStart: { x: number; y: number } | null = null
    let hoverFrame = 0
    let pendingHover: { x: number; y: number } | null = null
    let lastHoveredPointIndex: number | null = null
    let lastHoverRaycastAt = Number.NEGATIVE_INFINITY

    const clearHover = () => {
      pendingHover = null
      setOverlayHover(null)
      if (lastHoveredPointIndex !== null) {
        lastHoveredPointIndex = null
        onHoverPanoramaPointRef.current?.(null)
      }
    }

    const setPointerFromClient = (clientX: number, clientY: number) => {
      const bounds = renderer.domElement.getBoundingClientRect()
      if (!bounds.width || !bounds.height) return null
      pointer.set(
        ((clientX - bounds.left) / bounds.width) * 2 - 1,
        -((clientY - bounds.top) / bounds.height) * 2 + 1,
      )
      pointRaycaster.setFromCamera(pointer, camera)
      overlayRaycaster.setFromCamera(pointer, camera)
      detectionRaycaster.setFromCamera(pointer, camera)
      return bounds
    }

    const updateHover = (timestamp: number) => {
      if (timestamp - lastHoverRaycastAt < 33) {
        hoverFrame = ownerWindow.requestAnimationFrame(updateHover)
        return
      }
      hoverFrame = 0
      lastHoverRaycastAt = timestamp
      const pending = pendingHover
      pendingHover = null
      if (!pending) return
      const bounds = setPointerFromClient(pending.x, pending.y)
      if (!bounds) return

      const detectionIndex = detectionObject
        ? closestPointHitIndex(
            detectionRaycaster.intersectObject(detectionObject, false),
            pointer,
            camera,
            bounds.width,
            bounds.height,
            POINT_CLOUD_YOLO_HIT_RADIUS_PX,
          )
        : null
      const detectionEntry = detectionIndex === null
        ? undefined
        : detectionPoints[detectionIndex]
      const overlayIndex = !detectionEntry && overlayObject
        ? closestPointHitIndex(
            overlayRaycaster.intersectObject(overlayObject, false),
            pointer,
            camera,
            bounds.width,
            bounds.height,
            12,
          )
        : null
      const overlayEntry = overlayIndex === null
        ? undefined
        : overlayPoints[overlayIndex]
      const hoverEntry = detectionEntry ?? overlayEntry
      setOverlayHover(hoverEntry ? pointCloudHoverState(hoverEntry, {
        x: pending.x - bounds.left,
        y: pending.y - bounds.top,
        viewportWidth: bounds.width,
        viewportHeight: bounds.height,
      }) : null)

      const pointIndex = closestPointHitIndex(
        pointRaycaster.intersectObject(points, false),
        pointer,
        camera,
        bounds.width,
        bounds.height,
        7,
      )
      const metadata = projectionMetadataRef.current
      if (pointIndex === null || !metadata || metadata.frame_id !== frame?.id) {
        if (lastHoveredPointIndex !== null) onHoverPanoramaPointRef.current?.(null)
        lastHoveredPointIndex = null
        return
      }
      if (pointIndex === lastHoveredPointIndex) return
      lastHoveredPointIndex = pointIndex
      const offset = pointIndex * 3
      const projected = projectFrameLocalPointToPanorama(
        [displayPayload.positions[offset], displayPayload.positions[offset + 1], displayPayload.positions[offset + 2]],
        metadata,
      )
      onHoverPanoramaPointRef.current?.(
        projected ? { ...projected, frameId: metadata.frame_id } : null,
      )
    }

    const onPointerMove = (event: PointerEvent) => {
      if (event.buttons !== 0) {
        clearHover()
        return
      }
      pendingHover = { x: event.clientX, y: event.clientY }
      if (!hoverFrame) hoverFrame = ownerWindow.requestAnimationFrame(updateHover)
    }

    const onPointerLeave = () => {
      if (hoverFrame) ownerWindow.cancelAnimationFrame(hoverFrame)
      hoverFrame = 0
      clearHover()
    }
    const onPointerDown = (event: PointerEvent) => {
      pointerStart = { x: event.clientX, y: event.clientY }
    }
    const onCanvasClick = (event: MouseEvent) => {
      if (
        pointerStart &&
        Math.hypot(event.clientX - pointerStart.x, event.clientY - pointerStart.y) > 5
      ) {
        pointerStart = null
        return
      }
      pointerStart = null
      const bounds = setPointerFromClient(event.clientX, event.clientY)
      if (!bounds) return
      setPinnedOverlayHover(null)
      const actions = overlayActionsRef.current
      const target = actions.pickTarget
      const targetAcceptsPoint = pointCloudPickTargetAcceptsPoint(
        target,
        poleBaseProposal?.status ?? 'idle',
      )
      if (target && targetAcceptsPoint && frame?.dataset_position) {
        const pointIndex = closestPointHitIndex(
          pointRaycaster.intersectObject(points, false),
          pointer,
          camera,
          bounds.width,
          bounds.height,
          7,
        )
        if (pointIndex !== null) {
          const offset = pointIndex * 3
          const datasetCoordinates: [number, number, number] = [
            displayPayload.positions[offset] + frame.dataset_position[0],
            displayPayload.positions[offset + 1] + frame.dataset_position[1],
            displayPayload.positions[offset + 2] + frame.dataset_position[2],
          ]
          void applyPointCloudPickedCoordinate(target, frame.id, datasetCoordinates, actions)
        }
        return
      }
      if (measureModeRef.current) {
        const pointIndex = closestPointHitIndex(
          pointRaycaster.intersectObject(points, false),
          pointer,
          camera,
          bounds.width,
          bounds.height,
          7,
        )
        if (pointIndex !== null) {
          const offset = pointIndex * 3
          const point: [number, number, number] = [
            displayPayload.positions[offset],
            displayPayload.positions[offset + 1],
            displayPayload.positions[offset + 2],
          ]
          setMeasurementPoints((current) => current.length >= 2 ? [point] : [...current, point])
        }
        return
      }
      if (detectionObject) {
        const detectionIndex = closestPointHitIndex(
          detectionRaycaster.intersectObject(detectionObject, false),
          pointer,
          camera,
          bounds.width,
          bounds.height,
          POINT_CLOUD_YOLO_HIT_RADIUS_PX,
        )
        const entry = detectionIndex === null ? undefined : detectionPoints[detectionIndex]
        if (entry) {
          const transient = overlayHoverRef.current
          setPinnedOverlayHover(
            transient
              && transient.layerId === entry.layerId
              && String(transient.featureId) === String(entry.featureId ?? entry.observationId)
              ? transient
              : pointCloudHoverState(entry, {
                  x: event.clientX - bounds.left,
                  y: event.clientY - bounds.top,
                  viewportWidth: bounds.width,
                  viewportHeight: bounds.height,
                }),
          )
          return
        }
      }
      if (overlayObject) {
        const overlayIndex = closestPointHitIndex(
          overlayRaycaster.intersectObject(overlayObject, false),
          pointer,
          camera,
          bounds.width,
          bounds.height,
          12,
        )
        const entry = overlayIndex === null ? undefined : overlayPoints[overlayIndex]
        if (entry) {
          const transient = overlayHoverRef.current
          setPinnedOverlayHover(
            transient?.layerId === entry.layerId &&
              String(transient.featureId) === String(entry.featureId)
              ? transient
              : pointCloudHoverState(entry, {
                  x: event.clientX - bounds.left,
                  y: event.clientY - bounds.top,
                  viewportWidth: bounds.width,
                  viewportHeight: bounds.height,
                }),
          )
        }
      }
    }
    renderer.domElement.addEventListener('pointerdown', onPointerDown)
    renderer.domElement.addEventListener('pointermove', onPointerMove)
    renderer.domElement.addEventListener('pointerleave', onPointerLeave)
    renderer.domElement.addEventListener('click', onCanvasClick)

    return () => {
      rendererDisposed = true
      renderLoop.dispose()
      resizeObserver.disconnect()
      ownerWindow.removeEventListener('resize', wakeRenderer)
      ownerWindow.removeEventListener('focus', wakeRenderer)
      ownerWindow.removeEventListener('pageshow', wakeRenderer)
      ownerWindow.removeEventListener('pagehide', onPageHide)
      ownerDocument.removeEventListener('visibilitychange', onVisibilityChange)
      renderer.domElement.removeEventListener('webglcontextlost', onContextLost)
      renderer.domElement.removeEventListener('webglcontextrestored', onContextRestored)
      renderer.domElement.removeEventListener('pointerdown', onPointerDown)
      renderer.domElement.removeEventListener('pointermove', onPointerMove)
      renderer.domElement.removeEventListener('pointerleave', onPointerLeave)
      renderer.domElement.removeEventListener('click', onCanvasClick)
      if (hoverFrame) ownerWindow.cancelAnimationFrame(hoverFrame)
      clearHover()
      if (viewResetRequestedRef.current) {
        viewStateRef.current = null
        viewResetRequestedRef.current = false
      } else {
        rememberView()
      }
      controls.removeEventListener('change', rememberView)
      controls.dispose()
      geometry.dispose()
      material.dispose()
      overlayGeometry?.dispose()
      overlayMaterial?.dispose()
      selectedGeometry?.dispose()
      selectedMaterial?.dispose()
      detectionGeometry?.dispose()
      detectionMaterial?.dispose()
      detectionBoxGeometry?.dispose()
      detectionBoxMaterial?.dispose()
      poleBaseGroup?.traverse((object) => {
        const renderable = object as THREE.Object3D & {
          geometry?: THREE.BufferGeometry
          material?: THREE.Material | THREE.Material[]
        }
        renderable.geometry?.dispose()
        if (Array.isArray(renderable.material)) {
          renderable.material.forEach((entry) => entry.dispose())
        } else {
          renderable.material?.dispose()
        }
      })
      ;[manualProposalGroup].forEach((group) => {
        group?.traverse((object) => {
          const renderable = object as THREE.Object3D & {
            geometry?: THREE.BufferGeometry
            material?: THREE.Material | THREE.Material[]
          }
          renderable.geometry?.dispose()
          if (Array.isArray(renderable.material)) {
            renderable.material.forEach((entry) => entry.dispose())
          } else {
            renderable.material?.dispose()
          }
        })
      })
      capturePose.traverse((object) => {
        const renderable = object as THREE.Object3D & {
          geometry?: THREE.BufferGeometry
          material?: THREE.Material | THREE.Material[]
        }
        renderable.geometry?.dispose()
        if (Array.isArray(renderable.material)) {
          renderable.material.forEach((entry) => entry.dispose())
        } else {
          renderable.material?.dispose()
        }
      })
      if (pointMaterialRef.current === material) pointMaterialRef.current = null
      if (renderSceneRef.current === scene) renderSceneRef.current = null
      if (wakeRenderSceneRef.current === wakeRenderer) wakeRenderSceneRef.current = null
      renderer.dispose()
      renderer.domElement.remove()
    }
  }, [
    detectionPoints,
    frame?.dataset_position,
    frame?.heading,
    frame?.id,
    overlayPoints,
    displayPayload,
    manualProposalLocalPosition,
    poleBaseMarkerColor,
    poleBaseMarkerSizeM,
    poleBaseProposal,
  ])

  useEffect(() => {
    const scene = renderSceneRef.current
    if (!scene || measurementPoints.length === 0) return
    const group = new THREE.Group()
    group.name = 'point-measurement'
    measurementPoints.forEach((position, index) => {
      const marker = new THREE.Mesh(
        new THREE.SphereGeometry(0.14, 12, 8),
        new THREE.MeshBasicMaterial({
          color: index === 0 ? 0x65a9ff : 0xff6f91,
          depthTest: false,
        }),
      )
      marker.position.fromArray(position)
      marker.renderOrder = 36
      group.add(marker)
    })
    if (measurementPoints.length === 2) {
      const line = new THREE.Line(
        new THREE.BufferGeometry().setFromPoints(
          measurementPoints.map((point) => new THREE.Vector3(...point)),
        ),
        new THREE.LineBasicMaterial({ color: 0xffffff, depthTest: false }),
      )
      line.renderOrder = 35
      group.add(line)
    }
    scene.add(group)
    wakeRenderSceneRef.current?.()
    return () => {
      scene.remove(group)
      group.traverse((object) => {
        const renderable = object as THREE.Object3D & {
          geometry?: THREE.BufferGeometry
          material?: THREE.Material | THREE.Material[]
        }
        renderable.geometry?.dispose()
        if (Array.isArray(renderable.material)) {
          renderable.material.forEach((entry) => entry.dispose())
        } else {
          renderable.material?.dispose()
        }
      })
      wakeRenderSceneRef.current?.()
    }
  }, [measurementPoints, renderSceneGeneration])

  useEffect(() => {
    const material = pointMaterialRef.current
    if (!material) return
    material.size = pointSize * 0.045
  }, [pointSize])

  const readyPoleBaseResult = poleBaseProposal?.status === 'ready'
    ? poleBaseProposal.result
    : null
  const poleBaseWarning = readyPoleBaseResult
    ? poleBasePrimaryWarning(readyPoleBaseResult)
    : null
  const formatMetric = (value: number | null | undefined) =>
    Number.isFinite(value) ? `${Number(value).toFixed(3)} m` : '—'
  const resetPointTools = () => {
    setColorMode('rgb')
    setClipEnabled(false)
    setClipRadiusM(8)
    setClipRadiusDraftM(8)
    setZSliceEnabled(false)
    if (payload) {
      setZMinimum(payload.bounds.min[2])
      setZMaximum(payload.bounds.max[2])
      setZMinimumDraft(payload.bounds.min[2])
      setZMaximumDraft(payload.bounds.max[2])
    }
    setIsolateProposal(false)
    setMeasureMode(false)
    setMeasurementPoints([])
    viewResetRequestedRef.current = true
    viewStateRef.current = null
  }

  return (
    <div
      className={`pointcloud-view ${coordinatePickActive ? 'coordinate-pick-active' : ''}`}
      data-shp-point-count={overlayPoints.length}
      data-yolo-point-count={detectionPoints.length}
      data-pole-base-status={poleBaseProposal?.status ?? 'idle'}
      data-manual-proposal-preview={String(Boolean(manualProposalLocalPosition))}
      data-visible-point-count={displayPayload?.pointCount ?? 0}
    >
      <div ref={hostRef} className="pointcloud-canvas" />
      <OverlayHoverTooltip
        hover={pinnedOverlayHover ?? overlayHover}
        pinned={Boolean(pinnedOverlayHover)}
        onClose={() => {
          setPinnedOverlayHover(null)
          setOverlayHover(null)
        }}
        onDetails={(state) => {
          if (!overlay || !state.layerId) return
          overlay.selectFeature(
            { layerId: state.layerId, featureId: state.featureId },
            { navigate: false },
          )
          openOverlayFeatureDetails(overlay.datasetId, state)
        }}
      />
      {loading && (
        <div className="viewer-loading floating">
          <LoaderCircle className="spin" size={25} />
          <strong>{indexing ? '서버에서 미리보기 인덱싱 중' : '포인트 샘플 스트리밍 중'}</strong>
          <small>
            {indexing
              ? '준비되는 즉시 자동으로 다시 요청합니다.'
              : `최대 ${formatCount(budget)}개 포인트만 요청합니다.`}
          </small>
        </div>
      )}
      {error && (
        <div className="viewer-error">
          <RefreshCcw size={25} />
          <strong>3D 데이터를 표시할 수 없습니다</strong>
          <p>{error}</p>
          <button type="button" className="button secondary" onClick={() => setReloadKey((value) => value + 1)}>
            다시 불러오기
          </button>
        </div>
      )}
      {!frame && (
        <div className="viewer-error neutral">
          <Box size={28} />
          <strong>프레임을 선택해 주세요</strong>
          <p>선택한 위치 주변의 경량 포인트 샘플을 표시합니다.</p>
        </div>
      )}

      <div className="viewer-toolbar point-toolbar">
        <span>
          <Rotate3D size={15} />
          회전 · 우클릭 이동 · 휠 확대
        </span>
        <i />
        <label>
          <CircleGauge size={14} />
          <select value={budget} onChange={(event) => setBudget(Number(event.target.value))}>
            {availableBudgets.map((entry) => (
              <option value={entry.value} key={entry.value}>
                {entry.label}
              </option>
            ))}
          </select>
        </label>
        <span title="촬영 위치 기준 15m 안쪽은 고밀도, 15~25m는 저밀도로 샘플링합니다.">
          범위 · 15m 고밀도 · 최대 25m
        </span>
        <details className="pointcloud-tool-menu">
          <summary><Palette size={14} /> 판독 도구</summary>
          <div>
            <label>
              <span>색상</span>
              <select aria-label="점군 색상 모드" value={colorMode} onChange={(event) => setColorMode(event.target.value as PointCloudColorMode)}>
                <option value="rgb">RGB</option>
                <option value="intensity">Intensity</option>
                <option value="classification">Classification</option>
                <option value="height">Height</option>
              </select>
            </label>
            <label className="pointcloud-tool-check"><input type="checkbox" checked={clipEnabled} onChange={(event) => setClipEnabled(event.target.checked)} /> Local clip</label>
            <label>
              <span>XY 반경 {clipRadiusDraftM.toFixed(0)}m</span>
              <input
                type="range"
                min="1"
                max="25"
                step="1"
                value={clipRadiusDraftM}
                disabled={!clipEnabled}
                aria-label="점군 local clip 반경"
                onChange={(event) => setClipRadiusDraftM(Number(event.target.value))}
                onPointerUp={() => setClipRadiusM(clipRadiusDraftM)}
                onKeyUp={() => setClipRadiusM(clipRadiusDraftM)}
                onBlur={() => setClipRadiusM(clipRadiusDraftM)}
              />
            </label>
            <label className="pointcloud-tool-check"><input type="checkbox" checked={zSliceEnabled} onChange={(event) => setZSliceEnabled(event.target.checked)} /> Z slice</label>
            <div className="pointcloud-z-range">
              <label><span>Z 최소</span><input type="number" step="0.1" value={Number(zMinimumDraft.toFixed(2))} disabled={!zSliceEnabled} aria-label="점군 Z 최소" onChange={(event) => setZMinimumDraft(Number(event.target.value))} onBlur={() => setZMinimum(zMinimumDraft)} onKeyUp={(event) => { if (event.key === 'Enter' || event.key.startsWith('Arrow')) setZMinimum(zMinimumDraft) }} /></label>
              <label><span>Z 최대</span><input type="number" step="0.1" value={Number(zMaximumDraft.toFixed(2))} disabled={!zSliceEnabled} aria-label="점군 Z 최대" onChange={(event) => setZMaximumDraft(Number(event.target.value))} onBlur={() => setZMaximum(zMaximumDraft)} onKeyUp={(event) => { if (event.key === 'Enter' || event.key.startsWith('Arrow')) setZMaximum(zMaximumDraft) }} /></label>
            </div>
            <label className="pointcloud-tool-check"><input type="checkbox" checked={isolateProposal} disabled={!manualProposalLocalPosition} onChange={(event) => setIsolateProposal(event.target.checked)} /> 제안 주변만 표시</label>
            <button type="button" className={`button compact ${measureMode ? 'primary' : 'secondary'}`} disabled={coordinatePickActive} onClick={() => { setMeasureMode((value) => !value); setMeasurementPoints([]) }}><Ruler size={13} /> 2점 측정</button>
            <button type="button" className="button secondary compact" onClick={resetPointTools}><RefreshCcw size={13} /> 초기화</button>
            <small>{formatCount(displayPayload?.pointCount ?? 0)} / {formatCount(payload?.pointCount ?? 0)} 표시</small>
          </div>
        </details>
        {overlayPoints.length > 0 && (
          <span title="현재 프레임 주변에 표시된 SHP 포인트">
            <MapPin size={14} /> SHP {overlayPoints.length.toLocaleString('ko-KR')}
            {nearbyOverlayTotal > overlayPoints.length ? '+' : ''}
          </span>
        )}
        {detectionPoints.length > 0 && (
          <span title="YOLO 검출과 포인트클라우드가 실제로 연결된 3D 대표 위치">
            <Crosshair size={14} /> YOLO 3D {detectionPoints.length.toLocaleString('ko-KR')}
          </span>
        )}
        {detectionLoading && <LoaderCircle size={14} className="spin" aria-label="YOLO 3D 위치 불러오는 중" />}
        {detectionError && <span className="viewer-overlay-error" title={detectionError}>YOLO 3D 일부 오류</span>}
        {overlayLoading && <LoaderCircle size={14} className="spin" aria-label="SHP 포인트 불러오는 중" />}
        {overlayError && <span className="viewer-overlay-error" title={overlayError}>SHP 일부 오류</span>}
        {poleBasePicking && (
          <strong className="viewer-pick-indicator pole-base-pick-indicator">
            <Crosshair size={14} /> 지주 몸체의 실제 포인트를 클릭해 하단 산출 (B)
          </strong>
        )}
        {manualProposalLocalPosition && (
          <strong className="viewer-pick-indicator manual-proposal-indicator">
            <MapPin size={14} /> 수동 객체 제안 미리보기
          </strong>
        )}
        {!poleBasePicking && coordinatePickActive && (
          <strong className="viewer-pick-indicator">
            <Crosshair size={14} /> 실제 포인트를 클릭해 좌표 적용
          </strong>
        )}
      </div>

      {measureMode && (
        <section className="pointcloud-measure-card" aria-label="점군 2점 측정">
          <strong><Ruler size={14} /> 2점 측정</strong>
          {measurement ? (
            <dl>
              <div><dt>3D 거리</dt><dd>{measurement.distance3d.toFixed(3)} m</dd></div>
              <div><dt>XY 거리</dt><dd>{measurement.distanceXy.toFixed(3)} m</dd></div>
              <div><dt>수직 높이</dt><dd>{measurement.vertical.toFixed(3)} m</dd></div>
            </dl>
          ) : <span>{measurementPoints.length === 0 ? '첫 번째 점을 선택하세요.' : '두 번째 점을 선택하세요.'}</span>}
          <button type="button" className="button secondary compact" onClick={() => setMeasurementPoints([])}>측정 초기화</button>
        </section>
      )}

      {poleBaseProposal && poleBaseProposal.status !== 'idle' && poleBaseProposal.status !== 'picking' && (
        <section
          className={`pole-base-result-card pole-base-result-${
            readyPoleBaseResult?.status ?? poleBaseProposal.status
          }`}
          aria-label="지주 하단 산출 결과"
          aria-live="polite"
        >
          <header>
            <span>
              {poleBaseProposal.status === 'loading' ? (
                <LoaderCircle className="spin" size={16} />
              ) : readyPoleBaseResult?.status === 'auto' ? (
                <Check size={16} />
              ) : (
                <AlertTriangle size={16} />
              )}
              <strong>
                {poleBaseProposal.status === 'loading'
                  ? '지주 하단 계산 중'
                  : poleBaseProposal.status === 'error'
                    ? '산출 오류'
                    : poleBaseStatusLabel(readyPoleBaseResult!.status)}
              </strong>
            </span>
            <small>POLE BASE · DATASET XYZ</small>
          </header>

          {poleBaseProposal.status === 'loading' && (
            <p className="pole-base-result-message">
              원본 점군에서 지주 축과 국지 지면을 계산하고 있습니다.
            </p>
          )}

          {poleBaseProposal.status === 'error' && (
            <>
              <p className="pole-base-result-message error">{poleBaseProposal.message}</p>
              {poleBaseProposal.reasonCodes.length > 0 && (
                <small className="pole-base-result-warning">
                  {poleBaseReasonMessage(poleBaseProposal.reasonCodes[0])}
                </small>
              )}
            </>
          )}

          {readyPoleBaseResult && (
            <>
              <dl>
                <div className="pole-base-coordinate-row">
                  <dt>최종 X / Y / Z</dt>
                  <dd>
                    {readyPoleBaseResult.base_position
                      ? readyPoleBaseResult.base_position.map((value) => value.toFixed(3)).join(' / ')
                      : '산출 결과 없음'}
                  </dd>
                </div>
                <div><dt>품질 점수</dt><dd>{Math.round(readyPoleBaseResult.quality.score * 100)}%</dd></div>
                <div><dt>축 RMSE</dt><dd>{formatMetric(readyPoleBaseResult.axis?.rmse_m)}</dd></div>
                <div><dt>지면 RMSE</dt><dd>{formatMetric(readyPoleBaseResult.ground?.rmse_m)}</dd></div>
                <div><dt>바닥 외삽</dt><dd>{formatMetric(readyPoleBaseResult.quality.bottom_gap_m)}</dd></div>
              </dl>
              {poleBaseWarning && (
                <p className="pole-base-result-warning" role="alert">
                  <AlertTriangle size={13} /> {poleBaseWarning}
                </p>
              )}
              {poleBaseProposal.status === 'ready' &&
                poleBaseProposal.templateValidation?.duplicate.exact_duplicate && (
                  <p className="pole-base-result-warning" role="alert">
                    <AlertTriangle size={13} /> 동일 지주 객체가 이미 존재합니다.
                  </p>
                )}
              {poleBaseProposal.status === 'ready' &&
                poleBaseProposal.templateValidation &&
                poleBaseProposal.templateValidation.duplicate.warning_count > 0 &&
                !poleBaseProposal.templateValidation.duplicate.exact_duplicate && (
                  <p className="pole-base-result-warning" role="alert">
                    <AlertTriangle size={13} /> 근접 지주 확인과 3자 이상의 저장 사유가 필요합니다.
                  </p>
                )}
              {poleBaseProposal.status === 'ready' &&
                poleBaseProposal.templateValidation &&
                poleBaseProposal.templateValidation.missingRequiredFields.length > 0 && (
                  <p className="pole-base-result-warning" role="alert">
                    <AlertTriangle size={13} /> 필수 속성: {poleBaseProposal.templateValidation.missingRequiredFields.join(', ')}
                  </p>
                )}
            </>
          )}

          <footer>
            {readyPoleBaseResult && (
              <button
                type="button"
                className="button primary compact"
                disabled={
                  readyPoleBaseResult.status === 'failed' ||
                  !readyPoleBaseResult.base_position ||
                  (poleBaseProposal.status === 'ready' &&
                    poleBaseTemplateValidationBlocksSave(poleBaseProposal.templateValidation))
                }
                title="산출한 PointZ를 저장 (Enter)"
                onClick={() => void overlay?.confirmPoleBaseProposal()}
              >
                <Check size={13} /> 저장
              </button>
            )}
            {poleBaseProposal.status !== 'loading' &&
              !(
                poleBaseProposal.status === 'error' &&
                poleBaseProposal.reasonCodes.includes('TASK_RESOLUTION_PENDING')
              ) && (
              <button
                type="button"
                className="button secondary compact"
                title="같은 대상에서 지주점 다시 선택 (R)"
                onClick={() => overlay?.retryPoleBasePick()}
              >
                <RefreshCcw size={13} /> 다시 선택
              </button>
            )}
            <button
              type="button"
              className="button ghost compact"
              title="지주 하단 산출 취소 (Esc)"
              onClick={() => overlay?.cancelPoleBaseProposal()}
            >
              <X size={13} /> 취소
            </button>
          </footer>
        </section>
      )}
      <div className="point-size-control">
        <Scan size={14} />
        <input
          type="range"
          min="0.7"
          max="3"
          step="0.1"
          value={pointSize}
          aria-label="포인트 크기"
          onChange={(event) => setPointSize(Number(event.target.value))}
        />
      </div>
      {frame && payload && !loading && (
        <div className="capture-pose-legend" aria-label="현재 촬영 위치와 방향">
          <Camera size={14} />
          <span>
            <strong>현재 촬영 위치</strong>
            <small>
              {Number.isFinite(frame.heading)
                ? `진행 방향 ${Math.round(Number(frame.heading))}°`
                : '진행 방향 정보 없음'}
            </small>
          </span>
        </div>
      )}
      {payload && !loading && (
        <div className="viewer-data-card">
          <span>LIVE SAMPLE · MMSP</span>
          <strong>{formatCount(displayPayload?.pointCount ?? payload.pointCount)} points</strong>
          <small>{displayPayload && displayPayload.pointCount !== payload.pointCount ? `경량 샘플 ${formatCount(payload.pointCount)}개에서 로컬 필터` : '원본 LAS 대신 프레임 주변 경량 바이너리'}</small>
        </div>
      )}
    </div>
  )
}
