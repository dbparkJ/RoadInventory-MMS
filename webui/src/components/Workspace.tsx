import {
  ChevronLeft,
  ChevronRight,
  CircleGauge,
  CloudOff,
  Image,
  Layers3,
  Map,
  PanelRightClose,
  PanelRightOpen,
} from 'lucide-react'
import { lazy, Suspense } from 'react'
import type { DatasetSummary, Frame, RoutePoint, ViewMode } from '../types'

const MapView = lazy(() =>
  import('../views/MapView').then((module) => ({ default: module.MapView })),
)
const PanoramaView = lazy(() => import('../views/PanoramaView'))
const PointCloudView = lazy(() => import('../views/PointCloudView'))

interface WorkspaceProps {
  dataset: DatasetSummary | null
  frames: Frame[]
  frame: Frame | null
  route: RoutePoint[]
  routeLoading: boolean
  mapStyleUrl?: string
  demoMode: boolean
  view: ViewMode
  inspectorOpen: boolean
  onViewChange: (view: ViewMode) => void
  onFrameChange: (frame: Frame) => void
  onToggleInspector: () => void
  onOpenSource: () => void
  onUseDemo: () => void
}

const tabs: Array<{ id: ViewMode; label: string; icon: typeof Map }> = [
  { id: 'map', label: '지도', icon: Map },
  { id: 'panorama', label: '파노라마', icon: Image },
  { id: 'pointcloud', label: '3D 포인트', icon: Layers3 },
]

export function Workspace({
  dataset,
  frames,
  frame,
  route,
  routeLoading,
  mapStyleUrl,
  demoMode,
  view,
  inspectorOpen,
  onViewChange,
  onFrameChange,
  onToggleInspector,
  onOpenSource,
  onUseDemo,
}: WorkspaceProps) {
  const currentIndex = frame ? frames.findIndex((candidate) => candidate.id === frame.id) : -1
  const move = (direction: -1 | 1) => {
    const next = frames[currentIndex + direction]
    if (next) onFrameChange(next)
  }

  return (
    <main className="workspace">
      <header className="workspace-bar">
        <nav className="view-tabs" aria-label="데이터 보기 방식">
          {tabs.map((tab) => {
            const Icon = tab.icon
            return (
              <button
                type="button"
                key={tab.id}
                className={view === tab.id ? 'active' : ''}
                onClick={() => onViewChange(tab.id)}
              >
                <Icon size={15} />
                {tab.label}
              </button>
            )
          })}
        </nav>
        <div className="workspace-context">
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

      <div className="viewport">
        {!dataset ? (
          <div className="viewport-empty">
            <div className="empty-orbit orbit-one" />
            <div className="empty-orbit orbit-two" />
            <Layers3 size={38} />
            <h2>공간 데이터를 연결해 주세요</h2>
            <p>지도, 파노라마, 포인트 클라우드가 이 공간에 함께 표시됩니다.</p>
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
          <Suspense
            fallback={
              <ViewerLoading
                label={`${view === 'map' ? '지도' : view === 'panorama' ? '파노라마' : '3D'} 엔진 준비 중`}
              />
            }
          >
            {view === 'map' ? (
              <MapView
                route={route}
                frames={frames}
                selectedFrame={frame}
                loading={routeLoading}
                mapStyleUrl={mapStyleUrl}
                onSelectFrame={onFrameChange}
              />
            ) : view === 'panorama' ? (
              <PanoramaView datasetId={dataset.id} frame={frame} demoMode={demoMode} />
            ) : (
              <PointCloudView datasetId={dataset.id} frame={frame} demoMode={demoMode} />
            )}
          </Suspense>
        )}

        {dataset && frame && (
          <div className="frame-navigator">
            <button
              type="button"
              disabled={currentIndex <= 0}
              onClick={() => move(-1)}
              aria-label="이전 프레임"
            >
              <ChevronLeft size={17} />
            </button>
            <span>
              <strong>{String(frame.index + 1).padStart(4, '0')}</strong>
              <small>/ {dataset.frame_count.toLocaleString('ko-KR')}</small>
            </span>
            <button
              type="button"
              disabled={currentIndex < 0 || currentIndex >= frames.length - 1}
              onClick={() => move(1)}
              aria-label="다음 프레임"
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
