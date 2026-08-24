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
  Save,
  Shapes,
  Table2,
  Trash2,
  Undo2,
  X,
} from 'lucide-react'
import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { api } from '../lib/api'
import { frameNavigationDirection } from '../lib/frameNavigation'
import type { PanoramaHoverProjection } from '../lib/panoramaProjection'
import { TRACK_COLORS } from '../lib/route'
import { naturalSortTracks } from '../lib/tracks'
import { DEFAULT_USER_SETTINGS, type UserSettings, type UserSettingsPatch } from '../lib/userSettings'
import type { DatasetSummary, Frame, FrameRange, RoutePoint, SurveySegment } from '../types'
import type { MapMode } from '../views/MapView'
import { DetachablePanel, type DetachablePanelHandle } from './DetachablePanel'
import { useOptionalOverlayWorkspace } from './OverlayContext'
import { ReviewQueue } from './ReviewQueue'
import { ObjectTemplatePanel } from './ObjectTemplatePanel'
import { ProposalInspector } from './ProposalInspector'
import { QaIssuePanel } from './QaIssuePanel'

export const OPEN_POINT_CLOUD_EVENT = 'mms-open-pointcloud'

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
  const pointCloudOpenRequestRef = useRef(pointCloudOpen)
  pointCloudOpenRequestRef.current = pointCloudOpen
  const [mapMode, setMapMode] = useState<MapMode>('2d')
  const sortedTracks = useMemo(() => naturalSortTracks(dataset?.tracks ?? []), [dataset?.tracks])
  const trackCatalogueKey = `${dataset?.id ?? ''}:${sortedTracks.map((track) => track.id).join('\u0000')}`
  const [trackVisibility, setTrackVisibility] = useState<{
    catalogueKey: string
    hiddenTrackIds: ReadonlySet<string>
  }>(() => ({ catalogueKey: trackCatalogueKey, hiddenTrackIds: new Set() }))
  const hiddenTrackIds =
    trackVisibility.catalogueKey === trackCatalogueKey
      ? trackVisibility.hiddenTrackIds
      : new Set<string>()
  const visibleTrackIds = useMemo(
    () => new Set(sortedTracks.filter((track) => !hiddenTrackIds.has(track.id)).map((track) => track.id)),
    [hiddenTrackIds, sortedTracks],
  )
  const trackOrder = useMemo(() => sortedTracks.map((track) => track.id), [sortedTracks])
  const [layerCardCollapsed, setLayerCardCollapsed] = useState(false)
  const [surveySegments, setSurveySegments] = useState<SurveySegment[]>([])
  const [hiddenSurveySegmentIds, setHiddenSurveySegmentIds] = useState<ReadonlySet<string>>(new Set())
  const [surveyDrawing, setSurveyDrawing] = useState(false)
  const [surveyDraft, setSurveyDraft] = useState<[number, number][]>([])
  const [surveyDraftPreview, setSurveyDraftPreview] = useState<[number, number] | null>(null)
  const [surveyDraftName, setSurveyDraftName] = useState('현장조사 필요구간 1')
  const [surveyDraftColor, setSurveyDraftColor] = useState('#f59e0b')
  const [surveyBusy, setSurveyBusy] = useState(false)
  const [surveyError, setSurveyError] = useState<string | null>(null)
  const surveyLoadControllerRef = useRef<AbortController | null>(null)
  const surveyMutationControllerRef = useRef<AbortController | null>(null)
  const surveyGenerationRef = useRef(0)
  const surveyDatasetIdRef = useRef(dataset?.id ?? '')
  surveyDatasetIdRef.current = dataset?.id ?? ''
  const [hoveredPanoramaPoint, setHoveredPanoramaPoint] = useState<PanoramaHoverProjection | null>(null)
  const currentIndex = frame ? frames.findIndex((candidate) => candidate.id === frame.id) : -1
  const canMovePrevious = currentIndex > 0
  const canMoveNext = currentIndex >= 0 && (currentIndex < frames.length - 1 || hasMoreFrames)

  const visibleSurveySegments = useMemo(
    () => surveySegments.filter((segment) => !hiddenSurveySegmentIds.has(segment.id)),
    [hiddenSurveySegmentIds, surveySegments],
  )

  useEffect(() => setHoveredPanoramaPoint(null), [frame?.id])
  useEffect(() => {
    setTrackVisibility((current) =>
      current.catalogueKey === trackCatalogueKey
        ? current
        : { catalogueKey: trackCatalogueKey, hiddenTrackIds: new Set() },
    )
  }, [trackCatalogueKey])

  useEffect(() => {
    surveyGenerationRef.current += 1
    surveyLoadControllerRef.current?.abort()
    surveyMutationControllerRef.current?.abort()
    const controller = new AbortController()
    surveyLoadControllerRef.current = controller
    setSurveySegments([])
    setHiddenSurveySegmentIds(new Set())
    setSurveyDrawing(false)
    setSurveyDraft([])
    setSurveyDraftPreview(null)
    setSurveyBusy(false)
    setSurveyError(null)
    setSurveyDraftName('현장조사 필요구간 1')
    if (!dataset || demoMode) return () => controller.abort()

    void api.surveySegments(dataset.id, controller.signal).then(
      ({ items }) => {
        if (controller.signal.aborted) return
        setSurveySegments(items)
        setSurveyDraftName(`현장조사 필요구간 ${items.length + 1}`)
        if (surveyLoadControllerRef.current === controller) surveyLoadControllerRef.current = null
      },
      (reason) => {
        if (controller.signal.aborted) return
        setSurveyError(reason instanceof Error ? reason.message : '현장조사 구간을 불러오지 못했습니다.')
        if (surveyLoadControllerRef.current === controller) surveyLoadControllerRef.current = null
      },
    )
    return () => {
      controller.abort()
      surveyMutationControllerRef.current?.abort()
    }
  }, [dataset?.id, demoMode])

  useEffect(() => {
    if (!overlay?.pickMode || !surveyDrawing) return
    setSurveyDrawing(false)
    setSurveyDraft([])
    setSurveyDraftPreview(null)
    setSurveyError(null)
  }, [overlay?.pickMode, surveyDrawing])

  const addSurveyPoint = useCallback((coordinate: [number, number]) => {
    setSurveyDraft((current) => [...current, coordinate])
    setSurveyDraftPreview(null)
    setSurveyError(null)
  }, [])

  const startSurveyDrawing = () => {
    if (!dataset || overlay?.pickMode) return
    setSurveyDrawing(true)
    setSurveyDraft([])
    setSurveyDraftPreview(null)
    setSurveyDraftName(`현장조사 필요구간 ${surveySegments.length + 1}`)
    setSurveyError(null)
  }

  const cancelSurveyDrawing = () => {
    setSurveyDrawing(false)
    setSurveyDraft([])
    setSurveyDraftPreview(null)
    setSurveyError(null)
  }

  const saveSurveyDrawing = async () => {
    if (!dataset || surveyDraft.length < 2 || surveyBusy) return
    const name = surveyDraftName.trim()
    if (!name) {
      setSurveyError('구간 이름을 입력해 주세요.')
      return
    }
    setSurveyBusy(true)
    setSurveyDraftPreview(null)
    setSurveyError(null)
    surveyLoadControllerRef.current?.abort()
    const generation = surveyGenerationRef.current
    const controller = new AbortController()
    surveyMutationControllerRef.current = controller
    try {
      const created = demoMode
        ? {
            id: `survey-demo-${Date.now()}`,
            dataset_id: dataset.id,
            name,
            color: surveyDraftColor,
            geometry: { type: 'LineString' as const, coordinates: surveyDraft },
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(),
          }
        : (await api.createSurveySegment(dataset.id, {
            name,
            color: surveyDraftColor,
            coordinates: surveyDraft,
          }, controller.signal)).segment
      if (
        controller.signal.aborted ||
        surveyGenerationRef.current !== generation ||
        surveyDatasetIdRef.current !== dataset.id
      ) return
      setSurveySegments((current) => [...current, created])
      setSurveyDrawing(false)
      setSurveyDraft([])
      setSurveyDraftPreview(null)
      setSurveyDraftName(`현장조사 필요구간 ${surveySegments.length + 2}`)
    } catch (reason) {
      if (controller.signal.aborted || surveyGenerationRef.current !== generation) return
      setSurveyError(reason instanceof Error ? reason.message : '현장조사 구간을 저장하지 못했습니다.')
    } finally {
      if (surveyMutationControllerRef.current === controller) {
        surveyMutationControllerRef.current = null
      }
      if (surveyGenerationRef.current === generation) setSurveyBusy(false)
    }
  }

  const deleteSurveySegment = async (segment: SurveySegment, ownerWindow: Window | null) => {
    if (!dataset || surveyBusy) return
    if (!ownerWindow?.confirm(`“${segment.name}” 구간을 삭제할까요?`)) return
    setSurveyBusy(true)
    setSurveyError(null)
    surveyLoadControllerRef.current?.abort()
    const generation = surveyGenerationRef.current
    const controller = new AbortController()
    surveyMutationControllerRef.current = controller
    try {
      if (!demoMode) await api.deleteSurveySegment(dataset.id, segment.id, controller.signal)
      if (
        controller.signal.aborted ||
        surveyGenerationRef.current !== generation ||
        surveyDatasetIdRef.current !== dataset.id
      ) return
      setSurveySegments((current) => current.filter((candidate) => candidate.id !== segment.id))
      setHiddenSurveySegmentIds((current) => {
        const next = new Set(current)
        next.delete(segment.id)
        return next
      })
    } catch (reason) {
      if (controller.signal.aborted || surveyGenerationRef.current !== generation) return
      setSurveyError(reason instanceof Error ? reason.message : '현장조사 구간을 삭제하지 못했습니다.')
    } finally {
      if (surveyMutationControllerRef.current === controller) {
        surveyMutationControllerRef.current = null
      }
      if (surveyGenerationRef.current === generation) setSurveyBusy(false)
    }
  }

  const toggleTrackLayer = (trackId: string) => {
    setTrackVisibility((current) => {
      const nextHidden = new Set(
        current.catalogueKey === trackCatalogueKey ? current.hiddenTrackIds : [],
      )
      if (nextHidden.has(trackId)) nextHidden.delete(trackId)
      else nextHidden.add(trackId)
      return { catalogueKey: trackCatalogueKey, hiddenTrackIds: nextHidden }
    })
  }

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
    // Capture before focused map/viewer widgets can stop propagation.  A SHP
    // point pick leaves focus in those widgets, so a bubbling-only listener
    // made navigation appear dead immediately after create/delete workflows.
    window.addEventListener('keydown', onKeyDown, true)
    return () => window.removeEventListener('keydown', onKeyDown, true)
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

  useEffect(() => {
    const openPointCloudForTool = () => {
      const opened = pointCloudPanelRef.current?.detach() ?? false
      if (opened && !pointCloudOpenRequestRef.current) {
        pointCloudOpenRequestRef.current = true
        onTogglePointCloud()
      }
    }
    window.addEventListener(OPEN_POINT_CLOUD_EVENT, openPointCloudForTool)
    return () => window.removeEventListener(OPEN_POINT_CLOUD_EVENT, openPointCloudForTool)
  }, [onTogglePointCloud])

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
                visibleTrackIds={visibleTrackIds}
                trackOrder={trackOrder}
                frameRange={frameRange}
                loading={routeLoading}
                mapMode={mapMode}
                onMapModeChange={setMapMode}
                onSelectFrame={onFrameChange}
                surveySegments={visibleSurveySegments}
                surveyDraft={surveyDraft}
                surveyDraftColor={surveyDraftColor}
                surveyDraftPreview={surveyDraftPreview}
                surveyDrawing={surveyDrawing}
                onAddSurveyPoint={addSurveyPoint}
                onPreviewSurveyPoint={setSurveyDraftPreview}
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
                    {visibleTrackIds.size + (overlay?.visibleLayerIds.size ?? 0) + visibleSurveySegments.length} /{' '}
                    {sortedTracks.length + (overlay?.layers.length ?? 0) + surveySegments.length}
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
                  {sortedTracks.map((track, index) => {
                    const visible = visibleTrackIds.has(track.id)
                    const current = selectedTrack === track.id
                    const trackColor = TRACK_COLORS[index % TRACK_COLORS.length]
                    return (
                      <button
                        type="button"
                        key={`track:${track.id}`}
                        className={`${visible ? 'active' : ''} ${current ? 'current-track' : ''}`.trim()}
                        aria-pressed={visible}
                        onClick={() => toggleTrackLayer(track.id)}
                        title={`${track.name || track.id} 트랙 ${visible ? '숨기기' : '표시'}${
                          current ? ' (현재 작업 트랙)' : ''
                        }`}
                      >
                        <i
                          className="map-layer-track-swatch"
                          style={{ color: trackColor, borderColor: trackColor }}
                        >
                          <Route size={11} />
                        </i>
                        <span>{track.name || track.id}</span>
                        <small>
                          {current ? '현재 · ' : ''}{track.frame_count.toLocaleString('ko-KR')}
                        </small>
                        {visible ? <Eye size={13} /> : <EyeOff size={13} />}
                      </button>
                    )
                  })}
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
                  <div className="map-layer-group-title">
                    <span><Route size={11} /> 현장조사 구간</span>
                    <button
                      type="button"
                      className="map-layer-add-survey"
                      onClick={surveyDrawing ? cancelSurveyDrawing : startSurveyDrawing}
                      disabled={!dataset || surveyBusy || Boolean(overlay?.pickMode)}
                    >
                      {surveyDrawing ? <X size={11} /> : <Shapes size={11} />}
                      {surveyDrawing ? '그리기 취소' : '구간 그리기'}
                    </button>
                  </div>
                  {surveySegments.map((segment) => {
                    const visible = !hiddenSurveySegmentIds.has(segment.id)
                    return (
                      <div className={`map-layer-survey-row ${visible ? 'active' : ''}`} key={segment.id}>
                        <button
                          type="button"
                          aria-pressed={visible}
                          onClick={() => setHiddenSurveySegmentIds((current) => {
                            const next = new Set(current)
                            if (next.has(segment.id)) next.delete(segment.id)
                            else next.add(segment.id)
                            return next
                          })}
                          title={`${segment.name} ${visible ? '숨기기' : '표시'}`}
                        >
                          <i className="survey-line-swatch" style={{ background: segment.color }} />
                          <span>{segment.name}</span>
                          {visible ? <Eye size={13} /> : <EyeOff size={13} />}
                        </button>
                        <button
                          type="button"
                          className="map-layer-survey-delete"
                          aria-label={`${segment.name} 삭제`}
                          title={`${segment.name} 삭제`}
                          disabled={surveyBusy}
                          onClick={(event) => void deleteSurveySegment(
                            segment,
                            event.currentTarget.ownerDocument.defaultView,
                          )}
                        >
                          <Trash2 size={12} />
                        </button>
                      </div>
                    )
                  })}
                  {surveyDrawing && (
                    <div className="survey-drawing-editor" role="group" aria-label="현장조사 구간 그리기">
                      <label>
                        <span>구간 이름</span>
                        <input
                          value={surveyDraftName}
                          maxLength={120}
                          onChange={(event) => setSurveyDraftName(event.target.value)}
                        />
                      </label>
                      <label className="survey-color-field">
                        <span>색상</span>
                        <input
                          type="color"
                          value={surveyDraftColor}
                          onChange={(event) => setSurveyDraftColor(event.target.value)}
                        />
                      </label>
                      <p className="survey-drawing-guide">
                        <strong>완료 방법</strong>
                        지도에서 시작점과 끝점을 포함해 2개 이상 지점을 클릭한 뒤 저장을 누르세요.
                        마우스를 움직이면 다음 선이 미리 표시됩니다.
                      </p>
                      <p className="survey-drawing-progress">현재 {surveyDraft.length}개 지점</p>
                      <div>
                        <button
                          type="button"
                          onClick={() => {
                            setSurveyDraft((current) => current.slice(0, -1))
                            setSurveyDraftPreview(null)
                          }}
                          disabled={surveyDraft.length === 0 || surveyBusy}
                        >
                          <Undo2 size={11} /> 실행 취소
                        </button>
                        <button
                          type="button"
                          onClick={() => void saveSurveyDrawing()}
                          disabled={surveyDraft.length < 2 || surveyBusy}
                        >
                          <Save size={11} /> 저장
                        </button>
                      </div>
                    </div>
                  )}
                  {surveyError && <p className="survey-layer-error" role="alert">{surveyError}</p>}
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
                          poleBaseMarkerColor={settings.poleBaseMarkerColor}
                          poleBaseMarkerSizeM={settings.poleBaseMarkerSizeM}
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
                          poleBaseMarkerColor={settings.poleBaseMarkerColor}
                          poleBaseMarkerSizeM={settings.poleBaseMarkerSizeM}
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

        <ReviewQueue />
        <ObjectTemplatePanel />
        <ProposalInspector />
        <QaIssuePanel />

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
