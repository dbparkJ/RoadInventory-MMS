import {
  ChevronLeft,
  ChevronRight,
  CircleGauge,
  CloudOff,
  Eye,
  Image,
  Keyboard,
  Layers3,
  Map,
  PanelRightClose,
  PanelRightOpen,
  X,
} from 'lucide-react'
import { lazy, Suspense, useEffect, useRef, type ReactNode } from 'react'
import { DEFAULT_USER_SETTINGS, type UserSettings, type UserSettingsPatch } from '../lib/userSettings'
import type { DatasetSummary, Frame, FrameRange, RoutePoint } from '../types'
import { DetachablePanel } from './DetachablePanel'

const MapView = lazy(() =>
  import('../views/MapView').then((module) => ({ default: module.MapView })),
)
const PanoramaView = lazy(() => import('../views/PanoramaView'))
const PointCloudView = lazy(() => import('../views/PointCloudView'))

interface WorkspaceProps {
  dataset: DatasetSummary | null
  frames: Frame[]
  frame: Frame | null
  selectedTrack?: string
  frameRange: FrameRange | null
  route: RoutePoint[]
  routeLoading: boolean
  mapStyleUrl?: string
  demoMode: boolean
  panoramaOpen: boolean
  pointCloudOpen: boolean
  hasMoreFrames: boolean
  inspectorOpen: boolean
  detached?: boolean
  settings?: UserSettings
  externalAction?: ReactNode
  onTogglePanorama: () => void
  onTogglePointCloud: () => void
  onFrameChange: (frame: Frame) => void
  onMoveFrame: (direction: -1 | 1) => void
  onToggleInspector: () => void
  onOpenSource: () => void
  onUseDemo: () => void
  onSettingsChange?: (patch: UserSettingsPatch) => void
}

export function Workspace({
  dataset,
  frames,
  frame,
  selectedTrack,
  frameRange,
  route,
  routeLoading,
  mapStyleUrl,
  demoMode,
  panoramaOpen,
  pointCloudOpen,
  hasMoreFrames,
  inspectorOpen,
  detached = false,
  settings = DEFAULT_USER_SETTINGS,
  externalAction,
  onTogglePanorama,
  onTogglePointCloud,
  onFrameChange,
  onMoveFrame,
  onToggleInspector,
  onOpenSource,
  onUseDemo,
  onSettingsChange,
}: WorkspaceProps) {
  const workspaceRef = useRef<HTMLElement>(null)
  const currentIndex = frame ? frames.findIndex((candidate) => candidate.id === frame.id) : -1
  const canMovePrevious = currentIndex > 0
  const canMoveNext = currentIndex >= 0 && (currentIndex < frames.length - 1 || hasMoreFrames)
  const overlayCount = Number(panoramaOpen) + Number(pointCloudOpen)

  useEffect(() => {
    const ownerWindow = workspaceRef.current?.ownerDocument.defaultView ?? window
    const onKeyDown = (event: KeyboardEvent) => {
      if (
        event.defaultPrevented ||
        event.altKey ||
        event.ctrlKey ||
        event.metaKey ||
        event.shiftKey
      ) {
        return
      }
      const target = event.target as HTMLElement | null
      if (
        target &&
        (target.isContentEditable || ['INPUT', 'SELECT', 'TEXTAREA'].includes(target.tagName))
      ) {
        return
      }

      const previous = event.key === 'ArrowLeft' || event.code === 'KeyA'
      const next = event.key === 'ArrowRight' || event.code === 'KeyD'
      if (previous && canMovePrevious) {
        event.preventDefault()
        onMoveFrame(-1)
      } else if (next && canMoveNext) {
        event.preventDefault()
        onMoveFrame(1)
      }
    }
    ownerWindow.addEventListener('keydown', onKeyDown)
    return () => ownerWindow.removeEventListener('keydown', onKeyDown)
  }, [canMoveNext, canMovePrevious, detached, onMoveFrame])

  return (
    <main className="workspace" ref={workspaceRef}>
      <header className="workspace-bar">
        <nav className="view-tabs layer-toggles" aria-label="표시 데이터 선택">
          <button type="button" className="active base-layer" aria-pressed="true" title="기본 지도는 항상 표시됩니다.">
            <Map size={15} />
            지도
          </button>
          <button
            type="button"
            className={panoramaOpen ? 'active' : ''}
            aria-pressed={panoramaOpen}
            onClick={onTogglePanorama}
            title="파노라마 오버레이 켜기/끄기"
          >
            <Image size={15} />
            파노라마
          </button>
          <button
            type="button"
            className={pointCloudOpen ? 'active' : ''}
            aria-pressed={pointCloudOpen}
            onClick={onTogglePointCloud}
            title="3D 포인트 오버레이 켜기/끄기"
          >
            <Layers3 size={15} />
            3D 포인트
          </button>
          <span className="lazy-layer-note">
            <Eye size={12} /> 필요한 데이터만 로드
          </span>
        </nav>
        <div className="workspace-context">
          <span className="shortcut-hint" title="이전/다음 프레임 단축키">
            <Keyboard size={13} /> ← A&nbsp;&nbsp;D →
          </span>
          {demoMode && (
            <span className="context-badge demo">
              <CloudOff size={13} />
              데모 데이터
            </span>
          )}
          {frame && (
            <span className="context-badge">
              <CircleGauge size={13} />
              Frame {String(frame.index + 1).padStart(4, '0')}
            </span>
          )}
          {externalAction}
          <button
            type="button"
            className="icon-button"
            onClick={onToggleInspector}
            title={inspectorOpen ? '작업 설정 닫기' : '작업 설정 열기'}
          >
            {inspectorOpen ? <PanelRightClose size={16} /> : <PanelRightOpen size={16} />}
          </button>
        </div>
      </header>

      <div className="viewport layered-viewport">
        {!dataset ? (
          <div className="viewport-empty">
            <div className="empty-orbit orbit-one" />
            <div className="empty-orbit orbit-two" />
            <Layers3 size={38} />
            <h2>공간 데이터를 연결해 주세요</h2>
            <p>지도, 파노라마, 포인트 클라우드를 한 공간에서 함께 확인할 수 있습니다.</p>
            <div className="empty-actions">
              <button type="button" className="button primary" onClick={onOpenSource}>
                데이터 연결
              </button>
              <button type="button" className="button secondary" onClick={onUseDemo}>
                데모 둘러보기
              </button>
            </div>
          </div>
        ) : (
          <>
            <Suspense fallback={<ViewerLoading label="지도 엔진 준비 중" />}>
              <MapView
                route={route}
                frames={frames}
                selectedFrame={frame}
                activeTrackId={selectedTrack}
                showAllTracks={settings.showAllMapTracks}
                frameRange={frameRange}
                loading={routeLoading}
                mapStyleUrl={mapStyleUrl}
                onSelectFrame={onFrameChange}
              />
            </Suspense>

            {overlayCount > 0 && (
              <div className={`viewer-overlay-stack count-${overlayCount}`}>
                {panoramaOpen && (
                  <DetachablePanel
                    id={`panorama-${dataset.id}`}
                    title="파노라마 뷰어"
                    placeholderClassName="viewer-pane-slot"
                  >
                    {({ action }) => (
                      <section className="viewer-overlay-card panorama-pane" aria-label="파노라마 오버레이">
                        <header className="viewer-pane-header">
                          <span><Image size={14} /> 파노라마</span>
                          <div className="viewer-pane-actions">
                            {action}
                            <button type="button" onClick={onTogglePanorama} aria-label="파노라마 닫기" title="파노라마 닫기">
                              <X size={14} />
                            </button>
                          </div>
                        </header>
                        <div className="viewer-pane-content">
                          <Suspense fallback={<ViewerLoading label="파노라마 엔진 준비 중" />}>
                            <PanoramaView
                              datasetId={dataset.id}
                              frame={frame}
                              demoMode={demoMode}
                              forwardOffsetDeg={settings.panoramaForwardOffsetDeg}
                              quality={settings.panoramaDefaultQuality}
                              pointOverlayEnabled={settings.panoramaPointOverlayEnabled}
                              pointOverlayOpacity={settings.panoramaPointOverlayOpacity}
                              onQualityChange={(quality) =>
                                onSettingsChange?.({ panoramaDefaultQuality: quality })
                              }
                              onPointOverlayEnabledChange={(enabled) =>
                                onSettingsChange?.({ panoramaPointOverlayEnabled: enabled })
                              }
                              onPointOverlayOpacityChange={(opacity) =>
                                onSettingsChange?.({ panoramaPointOverlayOpacity: opacity })
                              }
                              onPreviousFrame={() => onMoveFrame(-1)}
                              onNextFrame={() => onMoveFrame(1)}
                              hasPreviousFrame={canMovePrevious}
                              hasNextFrame={canMoveNext}
                            />
                          </Suspense>
                        </div>
                      </section>
                    )}
                  </DetachablePanel>
                )}
                {pointCloudOpen && (
                  <DetachablePanel
                    id={`pointcloud-${dataset.id}`}
                    title="3D 포인트 뷰어"
                    placeholderClassName="viewer-pane-slot"
                  >
                    {({ action }) => (
                      <section className="viewer-overlay-card pointcloud-pane" aria-label="3D 포인트 오버레이">
                        <header className="viewer-pane-header">
                          <span><Layers3 size={14} /> 3D 포인트</span>
                          <div className="viewer-pane-actions">
                            {action}
                            <button type="button" onClick={onTogglePointCloud} aria-label="3D 포인트 닫기" title="3D 포인트 닫기">
                              <X size={14} />
                            </button>
                          </div>
                        </header>
                        <div className="viewer-pane-content">
                          <Suspense fallback={<ViewerLoading label="3D 엔진 준비 중" />}>
                            <PointCloudView datasetId={dataset.id} frame={frame} demoMode={demoMode} />
                          </Suspense>
                        </div>
                      </section>
                    )}
                  </DetachablePanel>
                )}
              </div>
            )}
          </>
        )}

        {dataset && frame && (
          <div className="frame-navigator">
            <button
              type="button"
              disabled={!canMovePrevious}
              onClick={() => onMoveFrame(-1)}
              aria-label="이전 프레임"
              title="이전 프레임 (← 또는 A)"
            >
              <ChevronLeft size={17} />
            </button>
            <span>
              <strong>{String(frame.index + 1).padStart(4, '0')}</strong>
              <small>/ {dataset.frame_count.toLocaleString('ko-KR')}</small>
            </span>
            <button
              type="button"
              disabled={!canMoveNext}
              onClick={() => onMoveFrame(1)}
              aria-label="다음 프레임"
              title="다음 프레임 (→ 또는 D)"
            >
              <ChevronRight size={17} />
            </button>
          </div>
        )}
      </div>
    </main>
  )
}

function ViewerLoading({ label }: { label: string }) {
  return (
    <div className="viewer-loading">
      <span className="loader-rings" />
      <strong>{label}</strong>
      <small>필요한 모듈만 불러오고 있습니다.</small>
    </div>
  )
}
