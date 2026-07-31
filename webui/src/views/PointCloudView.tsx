import { Box, CircleGauge, LoaderCircle, RefreshCcw, Rotate3D, Scan } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import * as THREE from 'three'
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js'
import { api, ApiError } from '../lib/api'
import { createDemoPointCloud } from '../lib/demo'
import { formatCount } from '../lib/format'
import { parseMmsp } from '../lib/mmsp'
import type { Frame, PointCloudPayload } from '../types'

const BUDGETS = [
  { value: 60_000, label: '빠름 · 6만' },
  { value: 120_000, label: '균형 · 12만' },
  { value: 250_000, label: '정밀 · 25만' },
]

export default function PointCloudView({
  datasetId,
  frame,
  demoMode,
}: {
  datasetId: string
  frame: Frame | null
  demoMode: boolean
}) {
  const hostRef = useRef<HTMLDivElement>(null)
  const pointMaterialRef = useRef<THREE.PointsMaterial | null>(null)
  const pointSizeRef = useRef(1.4)
  const [payload, setPayload] = useState<PointCloudPayload | null>(null)
  const [budget, setBudget] = useState(120_000)
  const [radius, setRadius] = useState(40)
  const [pointSize, setPointSize] = useState(1.4)
  const [loading, setLoading] = useState(false)
  const [indexing, setIndexing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  pointSizeRef.current = pointSize

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
    camera.position.set(span * 0.38, -span * 0.52, span * 0.32)

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
    controls.update()

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

    return () => {
      cancelAnimationFrame(animationFrame)
      resizeObserver.disconnect()
      renderer.domElement.removeEventListener('webglcontextlost', onContextLost)
      controls.dispose()
      geometry.dispose()
      material.dispose()
      if (pointMaterialRef.current === material) pointMaterialRef.current = null
      renderer.dispose()
      renderer.domElement.remove()
    }
  }, [payload])

  useEffect(() => {
    const material = pointMaterialRef.current
    if (!material) return
    material.size = pointSize * 0.045
  }, [pointSize])

  return (
    <div className="pointcloud-view">
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
