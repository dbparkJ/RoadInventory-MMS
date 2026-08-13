import {
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ChevronUp,
  CircleGauge,
  CloudOff,
  Eye,
  EyeOff,
  Image,
  Keyboard,
  Layers3,
  Map,
  Route,
  Shapes,
  Table2,
  X,
} from 'lucide-react'
import { lazy, Suspense, useEffect, useRef, useState, type ReactNode } from 'react'
import { frameNavigationDirection } from '../lib/frameNavigation'
import type { PanoramaHoverProjection } from '../lib/panoramaProjection'
import { DEFAULT_USER_SETTINGS, type UserSettings, type UserSettingsPatch } from '../lib/userSettings'
import type { DatasetSummary, Frame, FrameRange, RoutePoint } from '../types'
import type { MapMode } from '../views/MapView'
import { DetachablePanel, type DetachablePanelHandle } from './DetachablePanel'
import { useOptionalOverlayWorkspace } from './OverlayContext'

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
  demoMode: boolean
  detectionRevisionKey?: string
  panoramaOpen: boolean
  pointCloudOpen: boolean
  maxPointBudget?: number
  attributeTableOpen?: boolean
  hasMoreFrames: boolean
  /** @deprecated 작업 설정은 통합 설정 패널로 이동했습니다. */
  inspectorOpen?: boolean
  detached?: boolean
  settings?: UserSettings
  externalAction?: ReactNode
  onTogglePanorama: () => void
  onTogglePointCloud: () => void
  onToggleAttributeTable?: () => void
  onFrameChange: (frame: Frame) => void
  onMoveFrame: (direction: -1 | 1) => void
  /** @deprecated 작업 설정은 통합 설정 패널로 이동했습니다. */
  onToggleInspector?: () => void
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
  demoMode,
  detectionRevisionKey = '',
  panoramaOpen,
  pointCloudOpen,
  maxPointBudget = 1_000_000,
  attributeTableOpen = false,
  hasMoreFrames,
  detached = false,
  settings = DEFAULT_USER_SETTINGS,
  externalAction,
  onTogglePanorama,
  onTogglePointCloud,
  onToggleAttributeTable,
  onFrameChange,
  onMoveFrame,
  onOpenSource,
  onUseDemo,
  onSettingsChange,
}: WorkspaceProps) {
  const overlay = useOptionalOverlayWorkspace()
  const panoramaPanelRef = useRef<DetachablePanelHandle>(null)
  const pointCloudPanelRef = useRef<DetachablePanelHandle>(null)
  const [mapMode, setMapMode] = useState<MapMode>('2d')
  const [trackLayerVisible, setTrackLayerVisible] = useState(true)
  const [layerCardCollapsed, setLayerCardCollapsed] = useState(false)
  const [hoveredPanoramaPoint, setHoveredPanoramaPoint] = useState<PanoramaHoverProjection | null>(null)
  const currentIndex = frame ? frames.findIndex((candidate) => candidate.id === frame.id) : -1
  const canMovePrevious = currentIndex > 0
  const canMoveNext = currentIndex >= 0 && (currentIndex < frames.length - 1 || hasMoreFrames)

  useEffect(() => setHoveredPanoramaPoint(null), [frame?.id])
  useEffect(() => setTrackLayerVisible(true), [dataset?.id])

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const direction = frameNavigationDirection(event)
      if (direction === -1 && canMovePrevious) {
        event.preventDefault()
        onMoveFrame(-1)
      } else if (direction === 1 && canMoveNext) {
        event.preventDefault()
        onMoveFrame(1)
      }
    }
    // Keep the canonical listener in the application window. DetachablePanel
    // relays the same keys from every child popup, including nested popouts.
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [canMoveNext, canMovePrevious, onMoveFrame])

  const togglePanoramaPopup = () => {
    if (panoramaOpen) {
      panoramaPanelRef.current?.returnToMain()
      return
    }
    // Open synchronously from the click so browser popup blockers permit it.
    if (panoramaPanelRef.current?.detach()) onTogglePanorama()
  }

  const togglePointCloudPopup = () => {
    if (pointCloudOpen) {
      pointCloudPanelRef.current?.returnToMain()
      return
    }
    if (pointCloudPanelRef.current?.detach()) onTogglePointCloud()
  }

  return (
    <main className="workspace">
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
            onClick={togglePanoramaPopup}
            title={panoramaOpen ? '파노라마 팝업 닫기' : '파노라마 팝업 열기'}
          >
            <Image size={15} />
            파노라마
          </button>
          <button
            type="button"
            className={pointCloudOpen ? 'active' : ''}
            aria-pressed={pointCloudOpen}
            onClick={togglePointCloudPopup}
            title={pointCloudOpen ? '3D 포인트 팝업 닫기' : '3D 포인트 팝업 열기'}
          >
            <Layers3 size={15} />
            3D 포인트
          </button>
          <button
            type="button"
            className={attributeTableOpen ? 'active' : ''}
            aria-pressed={attributeTableOpen}
            disabled={!dataset}
            onClick={onToggleAttributeTable}
            title={attributeTableOpen ? 'SHP 속성표 팝업 닫기' : 'SHP 속성표 팝업 열기'}
          >
            <Table2 size={15} />
            속성표
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
                key={detached ? 'popup' : 'main'}
                route={route}
                frames={frames}
                selectedFrame={frame}
                activeTrackId={selectedTrack}
                showAllTracks={settings.showAllMapTracks}
                trackLayerVisible={trackLayerVisible}
                frameRange={frameRange}
                loading={routeLoading}
                mapMode={mapMode}
                onMapModeChange={setMapMode}
                onSelectFrame={onFrameChange}
              />
            </Suspense>

            <section
              className={`overlay-quick-controls ${layerCardCollapsed ? 'collapsed' : ''}`}
              aria-label="지도 레이어 표시 설정"
            >
              <header>
                <span>
                  <Shapes size={14} /> 검출·트랙 레이어
                  <small>
                    {(trackLayerVisible ? 1 : 0) + (overlay?.visibleLayerIds.size ?? 0)} /{' '}
                    {1 + (overlay?.layers.length ?? 0)}
                  </small>
                </span>
                <button
                  type="button"
                  aria-expanded={!layerCardCollapsed}
                  aria-controls="map-layer-quick-list"
                  aria-label={
                    layerCardCollapsed ? '지도 레이어 카드 펼치기' : '지도 레이어 카드 최소화'
                  }
                  title={layerCardCollapsed ? '레이어 목록 펼치기' : '한 줄로 최소화'}
                  onClick={() => setLayerCardCollapsed((value) => !value)}
                >
                  {layerCardCollapsed ? <ChevronDown size={14} /> : <ChevronUp size={14} />}
                </button>
              </header>
              {!layerCardCollapsed && (
                <div id="map-layer-quick-list" className="map-layer-quick-list">
                  <button
                    type="button"
                    className={trackLayerVisible ? 'active' : ''}
                    aria-pressed={trackLayerVisible}
                    onClick={() => setTrackLayerVisible((visible) => !visible)}
                    title={`MMS 트랙 ${trackLayerVisible ? '숨기기' : '표시'}`}
                  >
                    <i className="map-layer-track-swatch">
                      <Route size={11} />
                    </i>
                    <span>MMS 트랙</span>
                    <small>경로 · 프레임</small>
                    {trackLayerVisible ? <Eye size={13} /> : <EyeOff size={13} />}
                  </button>
                  {overlay?.layers.map((layer) => {
                    const visible = overlay.visibleLayerIds.has(layer.id)
                    return (
                      <button
                        type="button"
                        key={layer.id}
                        className={visible ? 'active' : ''}
                        aria-pressed={visible}
                        onClick={() => overlay.toggleLayer(layer.id)}
                        title={`${layer.name} ${visible ? '숨기기' : '표시'}`}
                      >
                        <i style={{ background: overlay.layerColor(layer.id) }} />
                        <span>{layer.name}</span>
                        <small>{mapGeometryLabel(layer.geometry_type)}</small>
                        {visible ? <Eye size={13} /> : <EyeOff size={13} />}
                      </button>
                    )
                  })}
                </div>
              )}
            </section>

            <DetachablePanel
              ref={panoramaPanelRef}
              id={`panorama-${dataset.id}`}
              title="파노라마 뷰어"
              placeholderClassName="viewer-pane-slot"
              hostHidden
              onDetachedChange={(isDetached) => {
                if (!isDetached && panoramaOpen) onTogglePanorama()
              }}
            >
              {({ returnToMain }) =>
                panoramaOpen ? (
                  <section className="viewer-overlay-card panorama-pane" aria-label="파노라마 뷰어">
                    <header className="viewer-pane-header">
                      <span><Image size={14} /> 파노라마</span>
                      <div className="viewer-pane-actions">
                        <button
                          type="button"
                          onClick={returnToMain}
                          aria-label="파노라마 닫기"
                          title="파노라마 닫기"
                        >
                          <X size={14} />
                        </button>
                      </div>
                    </header>
                    <div className="viewer-pane-content">
                      <Suspense fallback={<ViewerLoading label="파노라마 엔진 준비 중" />}>
                        <PanoramaView
                          datasetId={dataset.id}
                          frame={frame}
                          frames={frames}
                          onFrameChange={onFrameChange}
                          demoMode={demoMode}
                          detectionRevisionKey={detectionRevisionKey}
                          forwardOffsetDeg={settings.panoramaForwardOffsetDeg}
                          quality={settings.panoramaDefaultQuality}
                          pointOverlayEnabled={settings.panoramaPointOverlayEnabled}
                          panoramaOpacity={settings.panoramaImageOpacity}
                          maxOverlayDistanceM={settings.detectionVisibilityDistanceM}
                          linkedHoverPoint={hoveredPanoramaPoint}
                          onQualityChange={(quality) =>
                            onSettingsChange?.({ panoramaDefaultQuality: quality })
                          }
                          onPointOverlayEnabledChange={(enabled) =>
                            onSettingsChange?.({ panoramaPointOverlayEnabled: enabled })
                          }
                          onPanoramaOpacityChange={(opacity) =>
                            onSettingsChange?.({ panoramaImageOpacity: opacity })
                          }
                          onPreviousFrame={() => onMoveFrame(-1)}
                          onNextFrame={() => onMoveFrame(1)}
                          hasPreviousFrame={canMovePrevious}
                          hasNextFrame={canMoveNext}
                        />
                      </Suspense>
                    </div>
                  </section>
                ) : null
              }
            </DetachablePanel>
            <DetachablePanel
              ref={pointCloudPanelRef}
              id={`pointcloud-${dataset.id}`}
              title="3D 포인트 뷰어"
              placeholderClassName="viewer-pane-slot"
              hostHidden
              onDetachedChange={(isDetached) => {
                if (!isDetached && pointCloudOpen) onTogglePointCloud()
              }}
            >
              {({ returnToMain }) =>
                pointCloudOpen ? (
                  <section className="viewer-overlay-card pointcloud-pane" aria-label="3D 포인트 뷰어">
                    <header className="viewer-pane-header">
                      <span><Layers3 size={14} /> 3D 포인트</span>
                      <div className="viewer-pane-actions">
                        <button
                          type="button"
                          onClick={returnToMain}
                          aria-label="3D 포인트 닫기"
                          title="3D 포인트 닫기"
                        >
                          <X size={14} />
                        </button>
                      </div>
                    </header>
                    <div className="viewer-pane-content">
                      <Suspense fallback={<ViewerLoading label="3D 엔진 준비 중" />}>
                        <PointCloudView
                          datasetId={dataset.id}
                          frame={frame}
                          demoMode={demoMode}
                          maxPointBudget={maxPointBudget}
                          detectionRevisionKey={detectionRevisionKey}
                          onHoverPanoramaPoint={setHoveredPanoramaPoint}
                        />
                      </Suspense>
                    </div>
                  </section>
                ) : null
              }
            </DetachablePanel>
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

function mapGeometryLabel(geometryType: string): string {
  const normalized = geometryType.replace(/^Multi/, '')
  if (normalized === 'Point') return '점'
  if (normalized === 'LineString') return '선'
  if (normalized === 'Polygon') return '면'
  return geometryType || '피처'
}
