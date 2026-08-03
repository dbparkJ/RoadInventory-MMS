import { Box, CircleGauge, Crosshair, LoaderCircle, MapPin, RefreshCcw, Rotate3D, Scan } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import * as THREE from 'three'
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js'
import { useOptionalOverlayWorkspace } from '../components/OverlayContext'
import { api, ApiError } from '../lib/api'
import { createDemoPointCloud } from '../lib/demo'
import { formatCount } from '../lib/format'
import { parseMmsp } from '../lib/mmsp'
import type { Frame, OverlayFeature, PointCloudPayload } from '../types'

const BUDGETS = [
  { value: 60_000, label: '빠름 · 6만' },
  { value: 120_000, label: '균형 · 12만' },
  { value: 250_000, label: '정밀 · 25만' },
]

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

interface RenderOverlayPoint {
  layerId: string
  featureId: string | number
  color: string
  position: [number, number, number]
  selected: boolean
}

interface NearbyOverlayFeature {
  layerId: string
  color: string
  feature: OverlayFeature
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

export default function PointCloudView({
  datasetId,
  frame,
  demoMode,
}: {
  datasetId: string
  frame: Frame | null
  demoMode: boolean
}) {
  const overlay = useOptionalOverlayWorkspace()
  const hostRef = useRef<HTMLDivElement>(null)
  const pointMaterialRef = useRef<THREE.PointsMaterial | null>(null)
  const viewStateRef = useRef<PointCloudViewState | null>(null)
  const viewScopeRef = useRef(`${demoMode}:${datasetId}`)
  const pointSizeRef = useRef(1.4)
  const [payload, setPayload] = useState<PointCloudPayload | null>(null)
  const [budget, setBudget] = useState(120_000)
  const [radius, setRadius] = useState(40)
  const [pointSize, setPointSize] = useState(1.4)
  const [loading, setLoading] = useState(false)
  const [indexing, setIndexing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  const [nearbyOverlayFeatures, setNearbyOverlayFeatures] = useState<NearbyOverlayFeature[]>([])
  const [nearbyOverlayTotal, setNearbyOverlayTotal] = useState(0)
  const [overlayLoading, setOverlayLoading] = useState(false)
  const [overlayError, setOverlayError] = useState<string | null>(null)
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
  const viewScope = `${demoMode}:${datasetId}`
  if (viewScopeRef.current !== viewScope) {
    viewScopeRef.current = viewScope
    viewStateRef.current = null
  }
  pointSizeRef.current = pointSize

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
              radius * 1.5,
              5_000,
              controller.signal,
            )
            totals[index] = page.total
            const color = overlayLayerColor?.(layer.id) ?? '#ffb84d'
            groups[index] = page.features.map((feature) => ({ layerId: layer.id, color, feature }))
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
    radius,
    visibleOverlayLayerKey,
    visibleOverlayLayers,
  ])

  const overlayPoints = useMemo<RenderOverlayPoint[]>(() => {
    const origin = frame?.dataset_position
    if (!origin) return []
    const maximumDistanceSquared = (radius * 1.5) ** 2
    return nearbyOverlayFeatures.flatMap(({ layerId, color, feature }) => {
      if (feature.geometry?.type !== 'Point') return []
      const position = datasetPointToFrameLocal(feature.geometry.coordinates, origin)
      if (!position || position[0] ** 2 + position[1] ** 2 > maximumDistanceSquared) return []
      return [{
        layerId,
        featureId: feature.id,
        color,
        position,
        selected:
          selectedOverlay?.layerId === layerId &&
          String(selectedOverlay.featureId) === String(feature.id),
      }]
    })
  }, [frame?.dataset_position, nearbyOverlayFeatures, radius, selectedOverlay])

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
          : parseMmsp(await api.points(datasetId, frame.id, budget, radius, controller.signal))
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
  }, [budget, datasetId, demoMode, frame, radius, reloadKey])

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

    let overlayGeometry: THREE.BufferGeometry | null = null
    let overlayMaterial: THREE.PointsMaterial | null = null
    let overlayObject: THREE.Points | null = null
    let selectedGeometry: THREE.BufferGeometry | null = null
    let selectedMaterial: THREE.PointsMaterial | null = null
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
        size: 0.28,
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
          size: 0.52,
          sizeAttenuation: true,
          color: 0xffffff,
          depthTest: false,
        })
        const selectedObject = new THREE.Points(selectedGeometry, selectedMaterial)
        selectedObject.renderOrder = 5
        scene.add(selectedObject)
      }
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
    const raycaster = new THREE.Raycaster()
    raycaster.params.Points = { threshold: 0.34 }
    const pointer = new THREE.Vector2()
    let pointerStart: { x: number; y: number } | null = null
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
      const bounds = renderer.domElement.getBoundingClientRect()
      if (!bounds.width || !bounds.height) return
      pointer.set(
        ((event.clientX - bounds.left) / bounds.width) * 2 - 1,
        -((event.clientY - bounds.top) / bounds.height) * 2 + 1,
      )
      raycaster.setFromCamera(pointer, camera)
      const actions = overlayActionsRef.current
      if (actions.pickMode && frame?.dataset_position) {
        const hit = raycaster.intersectObject(points, false)[0]
        if (hit?.index !== undefined) {
          const offset = hit.index * 3
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
      if (overlayObject) {
        const hit = raycaster.intersectObject(overlayObject, false)[0]
        const entry = hit?.index === undefined ? undefined : overlayPoints[hit.index]
        if (entry) actions.selectFeature({ layerId: entry.layerId, featureId: entry.featureId })
      }
    }
    renderer.domElement.addEventListener('pointerdown', onPointerDown)
    renderer.domElement.addEventListener('click', onCanvasClick)

    return () => {
      cancelAnimationFrame(animationFrame)
      resizeObserver.disconnect()
      renderer.domElement.removeEventListener('webglcontextlost', onContextLost)
      renderer.domElement.removeEventListener('pointerdown', onPointerDown)
      renderer.domElement.removeEventListener('click', onCanvasClick)
      rememberView()
      controls.removeEventListener('change', rememberView)
      controls.dispose()
      geometry.dispose()
      material.dispose()
      overlayGeometry?.dispose()
      overlayMaterial?.dispose()
      selectedGeometry?.dispose()
      selectedMaterial?.dispose()
      if (pointMaterialRef.current === material) pointMaterialRef.current = null
      renderer.dispose()
      renderer.domElement.remove()
    }
  }, [frame?.dataset_position, overlayPoints, payload])

  useEffect(() => {
    const material = pointMaterialRef.current
    if (!material) return
    material.size = pointSize * 0.045
  }, [pointSize])

  return (
    <div
      className={`pointcloud-view ${overlay?.pickMode ? 'coordinate-pick-active' : ''}`}
      data-shp-point-count={overlayPoints.length}
    >
      <div ref={hostRef} className="pointcloud-canvas" />
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
            {BUDGETS.map((entry) => (
              <option value={entry.value} key={entry.value}>
                {entry.label}
              </option>
            ))}
          </select>
        </label>
        <label>
          반경
          <select value={radius} onChange={(event) => setRadius(Number(event.target.value))}>
            <option value={20}>20 m</option>
            <option value={40}>40 m</option>
            <option value={70}>70 m</option>
          </select>
        </label>
        {overlayPoints.length > 0 && (
          <span title="현재 프레임 주변에 표시된 SHP 포인트">
            <MapPin size={14} /> SHP {overlayPoints.length.toLocaleString('ko-KR')}
            {nearbyOverlayTotal > overlayPoints.length ? '+' : ''}
          </span>
        )}
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
