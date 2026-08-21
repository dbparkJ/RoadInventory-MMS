import { Box, Camera, CircleGauge, Crosshair, LoaderCircle, MapPin, RefreshCcw, Rotate3D, Scan } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import * as THREE from 'three'
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js'
import {
  openOverlayFeatureDetails,
  OverlayHoverTooltip,
  type OverlayHoverState,
} from '../components/OverlayHoverTooltip'
import { useOptionalOverlayWorkspace } from '../components/OverlayContext'
import { api, ApiError } from '../lib/api'
import { createDemoPointCloud } from '../lib/demo'
import { formatCount } from '../lib/format'
import { parseMmsp } from '../lib/mmsp'
import {
  projectFrameLocalPointToPanorama,
  type PanoramaHoverProjection,
} from '../lib/panoramaProjection'
import type {
  Frame,
  OverlayFeature,
  PanoramaDetectionBoxObservation,
  PanoramaProjectionMetadata,
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

export interface PointCloudViewState {
  position: [number, number, number]
  target: [number, number, number]
  zoom: number
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
  onHoverPanoramaPoint,
}: {
  datasetId: string
  frame: Frame | null
  demoMode: boolean
  maxPointBudget?: number
  detectionRevisionKey?: string
  onHoverPanoramaPoint?: (point: PanoramaHoverProjection | null) => void
}) {
  const overlay = useOptionalOverlayWorkspace()
  const hostRef = useRef<HTMLDivElement>(null)
  const pointMaterialRef = useRef<THREE.PointsMaterial | null>(null)
  const viewStateRef = useRef<PointCloudViewState | null>(null)
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

  useEffect(() => {
    setBudget((current) => Math.min(current, maximumAvailableBudget))
  }, [maximumAvailableBudget])
  const overlayHoverRef = useRef<OverlayHoverState | null>(null)
  const selectedOverlay = overlay?.selected
  const visibleOverlayLayers = useMemo(
    () => (overlay?.layers ?? []).filter((layer) => overlay?.visibleLayerIds.has(layer.id)),
    [overlay?.layers, overlay?.visibleLayerIds],
  )
  const visibleOverlayLayerKey = visibleOverlayLayers
    .map((layer) => `${layer.id}:${layer.revision}`)
    .join('|')
  const overlayLayerColor = overlay?.layerColor
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
          : parseMmsp(await api.points(datasetId, frame.id, budget, controller.signal))
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
  }, [budget, datasetId, demoMode, frame, reloadKey])

  useEffect(() => {
    const host = hostRef.current
    if (!host || !payload) return

    const scene = new THREE.Scene()
    scene.background = new THREE.Color(0x07111f)
    scene.fog = new THREE.FogExp2(0x07111f, 0.009)

    const camera = new THREE.PerspectiveCamera(52, host.clientWidth / host.clientHeight, 0.05, 2000)
    camera.up.set(0, 0, 1)
    const spanX = payload.bounds.max[0] - payload.bounds.min[0]
    const spanY = payload.bounds.max[1] - payload.bounds.min[1]
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
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 1.75))
    renderer.setSize(host.clientWidth, host.clientHeight)
    renderer.outputColorSpace = THREE.SRGBColorSpace
    host.appendChild(renderer.domElement)

    const controls = new OrbitControls(camera, renderer.domElement)
    controls.enableDamping = true
    controls.dampingFactor = 0.07
    controls.target.set(
      (payload.bounds.min[0] + payload.bounds.max[0]) / 2,
      (payload.bounds.min[1] + payload.bounds.max[1]) / 2,
      (payload.bounds.min[2] + payload.bounds.max[2]) / 2,
    )
    if (savedView) restorePointCloudViewState(camera, controls.target, savedView)
    controls.update()

    const rememberView = () => {
      viewStateRef.current = capturePointCloudViewState(camera, controls.target)
    }
    controls.addEventListener('change', rememberView)

    const geometry = new THREE.BufferGeometry()
    geometry.setAttribute('position', new THREE.BufferAttribute(payload.positions, 3))
    if (payload.colors) {
      geometry.setAttribute('color', new THREE.Uint8BufferAttribute(payload.colors, 3, true))
    }
    geometry.computeBoundingSphere()

    const material = new THREE.PointsMaterial({
      size: pointSizeRef.current * 0.045,
      sizeAttenuation: true,
      vertexColors: Boolean(payload.colors),
      color: payload.colors ? 0xffffff : 0x69e0be,
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

    const grid = new THREE.GridHelper(Math.ceil(span * 1.4), 24, 0x2bcfa8, 0x213548)
    grid.rotation.x = Math.PI / 2
    grid.position.z = payload.bounds.min[2] - 0.1
    ;(grid.material as THREE.Material).opacity = 0.22
    ;(grid.material as THREE.Material).transparent = true
    scene.add(grid)

    let animationFrame = 0
    const render = () => {
      controls.update()
      renderer.render(scene, camera)
      animationFrame = requestAnimationFrame(render)
    }
    render()

    const resizeObserver = new ResizeObserver(() => {
      const width = host.clientWidth
      const height = host.clientHeight
      camera.aspect = width / height
      camera.updateProjectionMatrix()
      renderer.setSize(width, height)
    })
    resizeObserver.observe(host)
    const onContextLost = (event: Event) => {
      event.preventDefault()
      cancelAnimationFrame(animationFrame)
      animationFrame = 0
      setError('그래픽 컨텍스트가 중단되었습니다. 다시 불러오기를 눌러 주세요.')
    }
    renderer.domElement.addEventListener('webglcontextlost', onContextLost)
    const pointRaycaster = new THREE.Raycaster()
    pointRaycaster.params.Points = { threshold: 0.18 }
    const overlayRaycaster = new THREE.Raycaster()
    overlayRaycaster.params.Points = { threshold: 0.48 }
    const detectionRaycaster = new THREE.Raycaster()
    detectionRaycaster.params.Points = { threshold: POINT_CLOUD_YOLO_RAYCAST_THRESHOLD }
    const pointer = new THREE.Vector2()
    const ownerWindow = host.ownerDocument.defaultView ?? window
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
        [payload.positions[offset], payload.positions[offset + 1], payload.positions[offset + 2]],
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
      if (actions.pickMode && frame?.dataset_position) {
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
          void actions.applyPickedCoordinate(
            [
              payload.positions[offset] + frame.dataset_position[0],
              payload.positions[offset + 1] + frame.dataset_position[1],
              payload.positions[offset + 2] + frame.dataset_position[2],
            ],
            'dataset',
          )
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
      cancelAnimationFrame(animationFrame)
      resizeObserver.disconnect()
      renderer.domElement.removeEventListener('webglcontextlost', onContextLost)
      renderer.domElement.removeEventListener('pointerdown', onPointerDown)
      renderer.domElement.removeEventListener('pointermove', onPointerMove)
      renderer.domElement.removeEventListener('pointerleave', onPointerLeave)
      renderer.domElement.removeEventListener('click', onCanvasClick)
      if (hoverFrame) ownerWindow.cancelAnimationFrame(hoverFrame)
      clearHover()
      rememberView()
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
      renderer.dispose()
      renderer.domElement.remove()
    }
  }, [detectionPoints, frame?.dataset_position, frame?.heading, overlayPoints, payload])

  useEffect(() => {
    const material = pointMaterialRef.current
    if (!material) return
    material.size = pointSize * 0.045
  }, [pointSize])

  return (
    <div
      className={`pointcloud-view ${overlay?.pickMode ? 'coordinate-pick-active' : ''}`}
      data-shp-point-count={overlayPoints.length}
      data-yolo-point-count={detectionPoints.length}
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
        {overlay?.pickMode && (
          <strong className="viewer-pick-indicator">
            <Crosshair size={14} /> 실제 포인트를 클릭해 좌표 적용
          </strong>
        )}
      </div>
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
          <strong>{formatCount(payload.pointCount)} points</strong>
          <small>원본 LAS 대신 프레임 주변 경량 바이너리</small>
        </div>
      )}
    </div>
  )
}
