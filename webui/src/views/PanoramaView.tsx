import {
  ChevronLeft,
  ChevronRight,
  Expand,
  LoaderCircle,
  Maximize2,
  Minus,
  MousePointer2,
  Plus,
  RefreshCcw,
} from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import * as THREE from 'three'
import demoPanorama from '../assets/demo-panorama.svg'
import { api } from '../lib/api'
import type { Frame } from '../types'

export type PanoramaQuality = 'fast' | 'high' | 'ultra'

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

export default function PanoramaView({
  datasetId,
  frame,
  demoMode,
  onPreviousFrame,
  onNextFrame,
  hasPreviousFrame = true,
  hasNextFrame = true,
}: {
  datasetId: string
  frame: Frame | null
  demoMode: boolean
  onPreviousFrame?: () => void
  onNextFrame?: () => void
  hasPreviousFrame?: boolean
  hasNextFrame?: boolean
}) {
  const stageRef = useRef<HTMLDivElement>(null)
  const [source, setSource] = useState<string | null>(demoMode ? demoPanorama : null)
  const [loading, setLoading] = useState(!demoMode)
  const [error, setError] = useState<string | null>(null)
  const [fov, setFov] = useState(72)
  const [yaw, setYaw] = useState(0)
  const [pitch, setPitch] = useState(0)
  const [quality, setQuality] = useState<PanoramaQuality>('high')
  const viewRef = useRef({ fov, yaw, pitch })
  viewRef.current = { fov, yaw, pitch }
  const [dragStart, setDragStart] = useState<{ x: number; y: number; yaw: number; pitch: number } | null>(
    null,
  )
  const [reloadKey, setReloadKey] = useState(0)

  useEffect(() => {
    setFov(72)
    setYaw((frame?.heading ?? 0) - 180)
    setPitch(0)
    setError(null)
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
    let objectUrl: string | undefined
    setLoading(true)
    setSource(null)
    const containerWidth = stageRef.current?.clientWidth ?? 1280
    // Request a bounded viewport-sized derivative, never the multi-gigapixel source image.
    const width = panoramaRequestWidth(containerWidth, window.devicePixelRatio, quality)
    void api
      .panorama(datasetId, frame.id, width, controller.signal)
      .then((result) => {
        if (result.kind === 'url') {
          setSource(result.value)
        } else {
          objectUrl = URL.createObjectURL(result.value)
          setSource(objectUrl)
        }
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) {
          setError(reason instanceof Error ? reason.message : '파노라마를 불러오지 못했습니다.')
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false)
      })
    return () => {
      controller.abort()
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [datasetId, demoMode, frame, quality, reloadKey])

  useEffect(() => {
    const host = stageRef.current
    if (!host || !source) return
    const scene = new THREE.Scene()
    scene.background = new THREE.Color(0x07111f)
    const camera = new THREE.PerspectiveCamera(
      viewRef.current.fov,
      host.clientWidth / host.clientHeight,
      0.05,
      100,
    )
    const renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: 'high-performance' })
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 1.6))
    renderer.setSize(host.clientWidth, host.clientHeight)
    renderer.outputColorSpace = THREE.SRGBColorSpace
    renderer.domElement.className = 'panorama-canvas'
    host.prepend(renderer.domElement)

    const geometry = new THREE.SphereGeometry(10, 64, 40)
    geometry.scale(-1, 1, 1)
    const texture = new THREE.TextureLoader().load(
      source,
      () => {
        texture.colorSpace = THREE.SRGBColorSpace
        texture.needsUpdate = true
      },
      undefined,
      () => setError('파노라마 텍스처를 디코딩하지 못했습니다.'),
    )
    texture.colorSpace = THREE.SRGBColorSpace
    const material = new THREE.MeshBasicMaterial({ map: texture })
    scene.add(new THREE.Mesh(geometry, material))

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
      geometry.dispose()
      material.dispose()
      texture.dispose()
      renderer.dispose()
      renderer.domElement.remove()
    }
  }, [source])

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

  return (
    <div
      ref={stageRef}
      className={`panorama-view ${dragStart ? 'dragging' : ''}`}
      tabIndex={0}
      role="region"
      aria-label="파노라마 뷰어"
      onKeyDown={(event) => {
        if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey) return
        const target = event.target as HTMLElement | null
        if (
          target &&
          (target.isContentEditable || ['INPUT', 'SELECT', 'TEXTAREA'].includes(target.tagName))
        ) {
          return
        }
        if (event.key === 'ArrowLeft' && canGoPrevious) {
          event.preventDefault()
          goToPreviousFrame()
        } else if (event.key === 'ArrowRight' && canGoNext) {
          event.preventDefault()
          goToNextFrame()
        }
      }}
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
      onPointerUp={() => setDragStart(null)}
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
      <div className="viewer-toolbar panorama-toolbar">
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
            onPointerDown={(event) => event.stopPropagation()}
            onChange={(event) => setQuality(event.target.value as PanoramaQuality)}
          >
            <option value="fast">빠름</option>
            <option value="high">고화질 · 4K</option>
            <option value="ultra">최고화질 · 8K</option>
          </select>
        </label>
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
