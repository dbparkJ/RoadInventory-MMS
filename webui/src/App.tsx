import {
  Bell,
  CircleHelp,
  CloudOff,
  ListChecks,
  Menu,
  Plus,
  ScanSearch,
  Server,
  Settings2,
  Shapes,
  SlidersHorizontal,
  X,
} from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Brand } from './components/Brand'
import { ActivityPanel, HelpPanel } from './components/ActivityHelpPanels'
import { DatasetPanel } from './components/DatasetPanel'
import { DetachablePanel, type DetachablePanelHandle } from './components/DetachablePanel'
import { GeneralSettingsPanel } from './components/GeneralSettingsPanel'
import { OVERLAY_DETAILS_EVENT } from './components/OverlayHoverTooltip'
import { OverlayProvider } from './components/OverlayContext'
import { OverlayPanel } from './components/OverlayPanel'
import { OptimizationPanel, DEFAULT_PARAMETERS } from './components/OptimizationPanel'
import { RunQueue } from './components/RunQueue'
import { StorageDialog } from './components/StorageDialog'
import { ToastRegion, type Toast } from './components/ToastRegion'
import { Workspace } from './components/Workspace'
import { api } from './lib/api'
import { useUserSettings } from './lib/userSettings'
import {
  createDemoRun,
  demoBootstrap,
  demoDataset,
  demoRoute,
  getDemoFrames,
} from './lib/demo'
import type {
  BootstrapResponse,
  DatasetDetail,
  DatasetSummary,
  Frame,
  FrameRange,
  ManualParameters,
  OverlayFeature,
  RoutePoint,
  RunEvent,
  RunRecord,
  RunRequest,
} from './types'

function App() {
  const [boot, setBoot] = useState<BootstrapResponse | null>(null)
  const [booting, setBooting] = useState(true)
  const [demoMode, setDemoMode] = useState(false)
  const [connectionIssue, setConnectionIssue] = useState<string | null>(null)
  const [datasets, setDatasets] = useState<DatasetSummary[]>([])
  const [datasetId, setDatasetId] = useState('')
  const [trackId, setTrackId] = useState('')
  const [frames, setFrames] = useState<Frame[]>([])
  const [selectedFrame, setSelectedFrame] = useState<Frame | null>(null)
  const [frameRange, setFrameRange] = useState<FrameRange | null>(null)
  const [framesLoading, setFramesLoading] = useState(false)
  const [framesLoadingMore, setFramesLoadingMore] = useState(false)
  const [frameNextOffset, setFrameNextOffset] = useState<number | null>(null)
  const [frameTotal, setFrameTotal] = useState(0)
  const [route, setRoute] = useState<RoutePoint[]>([])
  const [routeLoading, setRouteLoading] = useState(false)
  const [panoramaOpen, setPanoramaOpen] = useState(false)
  const [pointCloudOpen, setPointCloudOpen] = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [settingsDetached, setSettingsDetached] = useState(false)
  const [settingsSection, setSettingsSection] = useState<'general' | 'process'>('general')
  const [overlayOpen, setOverlayOpen] = useState(false)
  const [overlayDetached, setOverlayDetached] = useState(false)
  const [overlayFocusLayerId, setOverlayFocusLayerId] = useState('')
  const [notificationsOpen, setNotificationsOpen] = useState(false)
  const [notificationsDetached, setNotificationsDetached] = useState(false)
  const [helpOpen, setHelpOpen] = useState(false)
  const [helpDetached, setHelpDetached] = useState(false)
  const [dataPanelCollapsed, setDataPanelCollapsed] = useState(false)
  const [sourceOpen, setSourceOpen] = useState(false)
  const [queueOpen, setQueueOpen] = useState(false)
  const [runs, setRuns] = useState<RunRecord[]>([])
  const [submitting, setSubmitting] = useState(false)
  const [removingDatasetId, setRemovingDatasetId] = useState<string | null>(null)
  const [toasts, setToasts] = useState<Toast[]>([])
  const [notificationLog, setNotificationLog] = useState<Toast[]>([])
  const { settings, updateSettings, resetSettings } = useUserSettings()
  const frameScopeRef = useRef('')
  const pendingFrameFocusRef = useRef<{
    datasetId: string
    frame: Frame
    offset: number
  } | null>(null)
  const overlayFocusRequestRef = useRef(0)
  const overlayFocusControllerRef = useRef<AbortController | null>(null)
  const overlayPanelRef = useRef<DetachablePanelHandle>(null)
  const [frameFocusToken, setFrameFocusToken] = useState(0)
  frameScopeRef.current = `${datasetId}::${trackId}`

  const selectedDataset = datasets.find((dataset) => dataset.id === datasetId) ?? null
  const activeRuns = runs.filter((run) =>
    ['queued', 'preparing', 'running', 'cancelling'].includes(run.status),
  )
  const detectionRevisionKey = useMemo(
    () => completedDetectionRevision(runs, datasetId),
    [datasetId, runs],
  )

  const toast = useCallback((entry: Omit<Toast, 'id'>) => {
    const notification = {
      ...entry,
      id: `${Date.now()}-${Math.random().toString(36).slice(2)}`,
    }
    setToasts((current) => [...current, notification])
    setNotificationLog((current) => [notification, ...current].slice(0, 30))
  }, [])
  const dismissToast = useCallback((id: string) => {
    setToasts((current) => current.filter((toastItem) => toastItem.id !== id))
  }, [])

  const useDemo = useCallback(() => {
    setBoot(demoBootstrap)
    setDemoMode(true)
    setDatasets([demoDataset])
    setDatasetId(demoDataset.id)
    setTrackId('')
    setFrameRange(null)
    setRuns(demoBootstrap.recent_runs ?? [])
    setConnectionIssue((current) => current ?? '데모 모드에서는 서버 데이터가 변경되지 않습니다.')
  }, [])

  useEffect(() => {
    setFrameRange(null)
  }, [datasetId, trackId])

  useEffect(() => {
    const openImportedOverlay = (event: Event) => {
      const detail = (
        event as CustomEvent<{ open?: boolean; datasetId?: string; layerId?: string }>
      ).detail
      if (!detail?.open) return
      if (detail.datasetId && datasets.some((dataset) => dataset.id === detail.datasetId)) {
        setDatasetId(detail.datasetId)
        setTrackId('')
      }
      if (detail.layerId) setOverlayFocusLayerId(detail.layerId)
      setDataPanelCollapsed(false)
    }
    window.addEventListener('mms-overlay-changed', openImportedOverlay)
    return () => window.removeEventListener('mms-overlay-changed', openImportedOverlay)
  }, [datasets])

  useEffect(() => {
    const openOverlayDetails = (event: Event) => {
      const detail = (
        event as CustomEvent<{
          datasetId?: string
          layerId?: string
          featureId?: string | number
        }>
      ).detail
      if (!detail?.layerId) return
      if (
        detail.datasetId &&
        detail.datasetId !== datasetId &&
        datasets.some((dataset) => dataset.id === detail.datasetId)
      ) {
        setDatasetId(detail.datasetId)
        setTrackId('')
      }
      setOverlayFocusLayerId(detail.layerId)
      setDataPanelCollapsed(false)
      setOverlayOpen(true)
      if (overlayDetached) overlayPanelRef.current?.focus()
      else (document.defaultView ?? window).focus()
    }
    window.addEventListener(OVERLAY_DETAILS_EVENT, openOverlayDetails)
    return () => window.removeEventListener(OVERLAY_DETAILS_EVENT, openOverlayDetails)
  }, [datasetId, datasets, overlayDetached])

  useEffect(() => {
    const openSelectedOverlay = (event: Event) => {
      setDataPanelCollapsed(false)
      const detail = (
        event as CustomEvent<{
          datasetId?: string
          selection?: { layerId: string; featureId: string | number }
        }>
      ).detail
      if (
        demoMode ||
        !detail?.datasetId ||
        detail.datasetId !== datasetId ||
        !detail.selection
      ) {
        return
      }
      const requestId = overlayFocusRequestRef.current + 1
      overlayFocusRequestRef.current = requestId
      overlayFocusControllerRef.current?.abort()
      const controller = new AbortController()
      overlayFocusControllerRef.current = controller
      void api
        .overlayFeature(
          detail.datasetId,
          detail.selection.layerId,
          detail.selection.featureId,
          'dataset',
          controller.signal,
        )
        .then((response) => {
          const datasetPosition = overlayPointXY(response.feature)
          const imageName = overlayImageName(response.feature.properties)
          if (!datasetPosition && !imageName) {
            // Road-ledger line/polygon layers are still valid table and map
            // selections even though they have no single panorama target.
            return null
          }
          return api.locateFrame(
            detail.datasetId!,
            {
              ...(imageName ? { image_name: imageName } : {}),
              ...(datasetPosition ? { dataset_position: datasetPosition } : {}),
            },
            controller.signal,
          )
        })
        .then((located) => {
          if (
            !located ||
            controller.signal.aborted ||
            overlayFocusRequestRef.current !== requestId
          ) return
          pendingFrameFocusRef.current = {
            datasetId: detail.datasetId!,
            frame: located.frame,
            offset: located.page_offset,
          }
          setFrameRange(null)
          setTrackId(located.frame.track_id)
          setSelectedFrame(located.frame)
          setPanoramaOpen(true)
          setFrameFocusToken((value) => value + 1)
        })
        .catch((reason: unknown) => {
          if (controller.signal.aborted || overlayFocusRequestRef.current !== requestId) return
          toast({
            tone: 'error',
            title: 'SHP 피처와 MMS 프레임을 연결하지 못했습니다',
            message: reason instanceof Error ? reason.message : undefined,
          })
        })
        .finally(() => {
          if (overlayFocusControllerRef.current === controller) {
            overlayFocusControllerRef.current = null
          }
        })
    }
    window.addEventListener('mms-overlay-selected', openSelectedOverlay)
    return () => {
      window.removeEventListener('mms-overlay-selected', openSelectedOverlay)
      overlayFocusControllerRef.current?.abort()
      overlayFocusControllerRef.current = null
    }
  }, [datasetId, demoMode, toast])

  useEffect(() => {
    const controller = new AbortController()
    setBooting(true)
    void api
      .bootstrap(controller.signal)
      .then((response) => {
        setBoot(response)
        setDatasets(response.datasets ?? [])
        setDatasetId(response.datasets?.[0]?.id ?? '')
        setRuns(response.recent_runs ?? [])
        setDemoMode(false)
        setConnectionIssue(null)
        if (!response.datasets?.length) setSourceOpen(true)
      })
      .catch((reason: unknown) => {
        if (controller.signal.aborted) return
        setConnectionIssue(
          reason instanceof Error
            ? `서버에 연결하지 못했습니다: ${reason.message}`
            : '서버에 연결하지 못했습니다.',
        )
        useDemo()
      })
      .finally(() => {
        if (!controller.signal.aborted) setBooting(false)
      })
    return () => controller.abort()
  }, [useDemo])

  useEffect(() => {
    if (!datasetId || selectedDataset?.status !== 'ready') {
      setFrames([])
      setSelectedFrame(null)
      setFrameNextOffset(null)
      setFrameTotal(0)
      return
    }
    const controller = new AbortController()
    const pendingFocus =
      pendingFrameFocusRef.current?.datasetId === datasetId &&
      pendingFrameFocusRef.current.frame.track_id === trackId
        ? pendingFrameFocusRef.current
        : null
    const requestOffset = pendingFocus?.offset ?? 0
    setFrames([])
    if (!pendingFocus) setSelectedFrame(null)
    setFrameNextOffset(null)
    setFrameTotal(0)
    setFramesLoadingMore(false)
    setFramesLoading(true)
    const frameRequest = demoMode
      ? Promise.resolve(getDemoFrames(requestOffset, 240, trackId || undefined))
      : api.frames(datasetId, requestOffset, 240, trackId || undefined, controller.signal)
    void frameRequest
      .then((page) => {
        setFrames(page.items)
        setFrameTotal(page.total)
        setFrameNextOffset(resolveNextOffset(page))
        setSelectedFrame((current) => {
          if (pendingFocus) return page.items.find((frame) => frame.id === pendingFocus.frame.id) ?? pendingFocus.frame
          if (current && page.items.some((frame) => frame.id === current.id)) return current
          return page.items[0] ?? null
        })
        if (pendingFocus && pendingFrameFocusRef.current === pendingFocus) {
          pendingFrameFocusRef.current = null
        }
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) {
          setFrames([])
          setSelectedFrame(null)
          toast({
            tone: 'error',
            title: '프레임 목록을 불러오지 못했습니다',
            message: reason instanceof Error ? reason.message : undefined,
          })
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setFramesLoading(false)
      })
    return () => controller.abort()
  }, [datasetId, demoMode, frameFocusToken, selectedDataset?.status, toast, trackId])

  const loadMoreFrames = useCallback(async (): Promise<Frame[]> => {
    if (!datasetId || frameNextOffset === null || framesLoadingMore) return []
    const scope = `${datasetId}::${trackId}`
    const offset = frameNextOffset
    const controller = new AbortController()
    setFramesLoadingMore(true)
    try {
      const page = demoMode
        ? getDemoFrames(offset, 240, trackId || undefined)
        : await api.frames(datasetId, offset, 240, trackId || undefined, controller.signal)
      if (frameScopeRef.current !== scope) return []
      setFrames((current) => {
        const byId = new Map(current.map((frame) => [frame.id, frame]))
        page.items.forEach((frame) => byId.set(frame.id, frame))
        return [...byId.values()]
      })
      setFrameTotal(page.total)
      setFrameNextOffset(resolveNextOffset(page))
      return page.items
    } catch (reason) {
      if (!controller.signal.aborted && frameScopeRef.current === scope) {
        toast({
          tone: 'error',
          title: '추가 프레임을 불러오지 못했습니다',
          message: reason instanceof Error ? reason.message : undefined,
        })
      }
      return []
    } finally {
      if (frameScopeRef.current === scope) setFramesLoadingMore(false)
    }
  }, [datasetId, demoMode, frameNextOffset, framesLoadingMore, toast, trackId])

  const moveFrame = useCallback(
    async (direction: -1 | 1) => {
      if (!selectedFrame) return
      const currentIndex = frames.findIndex((frame) => frame.id === selectedFrame.id)
      const next = frames[currentIndex + direction]
      if (next) {
        setSelectedFrame(next)
        return
      }
      if (direction === 1 && frameNextOffset !== null) {
        const more = await loadMoreFrames()
        const following = more.find((frame) => frame.index > selectedFrame.index) ?? more[0]
        if (following) setSelectedFrame(following)
      }
    },
    [frameNextOffset, frames, loadMoreFrames, selectedFrame],
  )

  useEffect(() => {
    if (!datasetId || selectedDataset?.status !== 'ready') {
      setRoute([])
      return
    }
    const controller = new AbortController()
    setRoute([])
    setRouteLoading(true)
    const request = demoMode ? Promise.resolve(demoRoute) : api.route(datasetId, controller.signal)
    void request
      .then((response) => setRoute(response.points ?? []))
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) {
          setRoute([])
          toast({
            tone: 'error',
            title: '주행 경로를 표시하지 못했습니다',
            message: reason instanceof Error ? reason.message : undefined,
          })
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setRouteLoading(false)
      })
    return () => controller.abort()
  }, [datasetId, demoMode, selectedDataset?.status, toast])

  useEffect(() => {
    if (demoMode || !boot) return
    const controller = new AbortController()
    void api
      .runs(controller.signal)
      .then((response) => setRuns(response.items))
      .catch(() => {
        // Bootstrap history remains useful when this optional refresh fails.
      })
    return () => controller.abort()
  }, [boot, demoMode])

  const activeRunKey = useMemo(
    () => activeRuns.map((run) => run.id).sort().join(','),
    [activeRuns],
  )
  useEffect(() => {
    if (demoMode || !activeRunKey) return
    const closeStreams = activeRunKey.split(',').map((runId) =>
      api.subscribeToRun(runId, (event) => {
        setRuns((current) => updateRunFromEvent(current, runId, event))
      }),
    )
    return () => closeStreams.forEach((close) => close())
  }, [activeRunKey, demoMode])

  useEffect(() => {
    if (!demoMode || !activeRuns.length) return
    const timer = window.setInterval(() => {
      setRuns((current) =>
        current.map((run) => {
          if (!run.id.startsWith('local-demo-') || run.status !== 'running') return run
          const nextProgress = Math.min(100, run.progress + 3 + Math.random() * 5)
          const stages =
            nextProgress < 24
              ? '데이터 준비'
              : nextProgress < 58
                ? '객체 후보 검출'
                : nextProgress < 88
                  ? '공간 군집 최적화'
                  : '결과 패키징'
          return {
            ...run,
            progress: nextProgress,
            stage: stages,
            message: stages,
            eta_seconds: Math.max(0, Math.round((100 - nextProgress) * 0.9)),
            ...(nextProgress >= 100
              ? {
                  status: 'completed' as const,
                  finished_at: new Date().toISOString(),
                  result_url: '#demo-result',
                }
              : {}),
          }
        }),
      )
    }, 1_400)
    return () => window.clearInterval(timer)
  }, [activeRunKey, activeRuns.length, demoMode])

  const startRun = async (request: RunRequest) => {
    setSubmitting(true)
    try {
      const run = demoMode ? createDemoRun(runs.length + 1) : await api.createRun(request)
      setRuns((current) => [run, ...current.filter((entry) => entry.id !== run.id)])
      setQueueOpen(true)
      toast({
        tone: 'success',
        title: '작업이 실행 큐에 등록되었습니다',
        message: demoMode ? '데모 진행률을 실시간으로 표시합니다.' : '창을 닫아도 서버에서 계속 처리됩니다.',
      })
    } catch (reason) {
      toast({
        tone: 'error',
        title: '작업을 시작하지 못했습니다',
        message: reason instanceof Error ? reason.message : undefined,
      })
    } finally {
      setSubmitting(false)
    }
  }

  const optimize = async (request: RunRequest): Promise<ManualParameters | undefined> => {
    try {
      if (demoMode) {
        await new Promise((resolve) => window.setTimeout(resolve, 850))
        toast({
          tone: 'success',
          title: '자동 설정을 확인했습니다',
          message: '검증된 알고리즘 기준은 유지하고 처리 자원만 조정합니다.',
        })
        return { ...DEFAULT_PARAMETERS }
      }
      const response = await api.optimize(request)
      toast({
        tone: 'success',
        title: '자동 설정을 확인했습니다',
        message: '서버가 작업 규모에 맞는 처리 자원 프로필을 준비했습니다.',
      })
      return response.parameters
    } catch (reason) {
      toast({
        tone: 'error',
        title: '자동 설정을 확인하지 못했습니다',
        message: reason instanceof Error ? reason.message : undefined,
      })
      return undefined
    }
  }

  const cancelRun = async (id: string) => {
    setRuns((current) =>
      current.map((run) => (run.id === id ? { ...run, status: 'cancelling' } : run)),
    )
    try {
      const updated = demoMode
        ? ({ ...runs.find((run) => run.id === id)!, status: 'cancelled', progress: 0 } as RunRecord)
        : await api.cancelRun(id)
      setRuns((current) => current.map((run) => (run.id === id ? updated : run)))
      toast({ tone: 'info', title: '작업을 취소했습니다' })
    } catch (reason) {
      toast({
        tone: 'error',
        title: '작업을 취소하지 못했습니다',
        message: reason instanceof Error ? reason.message : undefined,
      })
    }
  }

  const dismissRun = async (id: string) => {
    try {
      const detail = demoMode
        ? '데모 실행 기록을 목록에서 제거했습니다. 산출물은 삭제하지 않았습니다.'
        : (await api.deleteRun(id)).detail
      setRuns((current) => current.filter((run) => run.id !== id))
      toast({
        tone: 'success',
        title: '실행 기록을 목록에서 제거했습니다',
        message: detail,
      })
    } catch (reason) {
      toast({
        tone: 'error',
        title: '실행 기록을 제거하지 못했습니다',
        message: reason instanceof Error ? reason.message : undefined,
      })
    }
  }

  const acceptDataset = (dataset: DatasetDetail) => {
    setDemoMode(false)
    setConnectionIssue(null)
    setDatasets((current) => [dataset, ...current.filter((entry) => entry.id !== dataset.id)])
    setFrameRange(null)
    setDatasetId(dataset.id)
    setTrackId('')
    setPanoramaOpen(false)
    setPointCloudOpen(false)
    toast({
      tone: 'success',
      title: '데이터셋 준비가 완료되었습니다',
      message: `${dataset.name} · ${dataset.frame_count.toLocaleString('ko-KR')} 프레임`,
    })
  }

  const removeDataset = async (dataset: DatasetSummary) => {
    const ownerWindow = document.defaultView ?? window
    const confirmed = ownerWindow.confirm(
      `${dataset.name}을(를) 작업 목록에서 제거할까요?\n\n서버의 원본 폴더와 파일은 삭제되지 않습니다.`,
    )
    if (!confirmed) return

    setRemovingDatasetId(dataset.id)
    try {
      const detail = demoMode
        ? '데모 데이터를 현재 작업 목록에서 제거했습니다.'
        : (await api.unregisterDataset(dataset.id)).detail
      const remaining = datasets.filter((entry) => entry.id !== dataset.id)
      setDatasets(remaining)
      if (datasetId === dataset.id) {
        setDatasetId(remaining[0]?.id ?? '')
        setTrackId('')
        setFrames([])
        setSelectedFrame(null)
        setFrameRange(null)
        setRoute([])
        setPanoramaOpen(false)
        setPointCloudOpen(false)
      }
      toast({
        tone: 'success',
        title: '작업 데이터 등록을 해제했습니다',
        message: detail || '원본 데이터는 그대로 보존됩니다.',
      })
    } catch (reason) {
      toast({
        tone: 'error',
        title: '작업 데이터를 제거하지 못했습니다',
        message: reason instanceof Error ? reason.message : undefined,
      })
    } finally {
      setRemovingDatasetId(null)
    }
  }

  if (booting) return <BootScreen />

  return (
    <OverlayProvider datasetId={datasetId} demoMode={demoMode} notify={toast}>
      <div className={`app-shell ${dataPanelCollapsed ? 'data-collapsed' : ''}`}>
      <header className="topbar">
        <div className="topbar-left">
          <button type="button" className="icon-button mobile-menu">
            <Menu size={18} />
          </button>
          <Brand />
          <span className="topbar-separator" />
          <div className={`server-state ${connectionIssue ? 'offline' : ''}`}>
            {connectionIssue ? <CloudOff size={14} /> : <Server size={14} />}
            <span>
              <strong>{connectionIssue ? '데모 세션' : boot?.server_name ?? 'MMS Server'}</strong>
              <small>{connectionIssue ? '서버 미연결' : `API ${boot?.api_version ?? '—'}`}</small>
            </span>
          </div>
        </div>
        <div className="topbar-actions">
          <button
            type="button"
            className="queue-button overlay-button"
            title="SHP 업로드와 고급 편집 관리"
            aria-label="SHP 관리 열기"
            aria-expanded={overlayOpen || overlayDetached}
            onClick={() => setOverlayOpen(true)}
          >
            <Shapes size={17} />
            SHP 관리
          </button>
          <button
            type="button"
            className="button primary compact detection-start-button"
            onClick={() => {
              setSettingsSection('process')
              setSettingsOpen(true)
            }}
          >
            <ScanSearch size={16} />
            자동 검출
          </button>
          <button
            type="button"
            className="queue-button settings-button"
            title="일반 설정"
            aria-label="일반 설정 열기"
            onClick={() => {
              setSettingsSection('general')
              setSettingsOpen(true)
            }}
          >
            <Settings2 size={17} />
            설정
          </button>
          <button
            type="button"
            className="icon-button"
            title="도움말"
            aria-label="도움말 열기"
            aria-expanded={helpOpen || helpDetached}
            aria-controls="help-panel-title"
            onClick={() => setHelpOpen(true)}
          >
            <CircleHelp size={17} />
          </button>
          <button
            type="button"
            className="icon-button notification-button"
            title="알림"
            aria-label="알림 열기"
            aria-expanded={notificationsOpen || notificationsDetached}
            aria-controls="activity-panel-title"
            onClick={() => setNotificationsOpen(true)}
          >
            <Bell size={17} />
            {activeRuns.length > 0 && <i />}
          </button>
          <button type="button" className="queue-button" onClick={() => setQueueOpen(true)}>
            <ListChecks size={16} />
            실행 큐
            {activeRuns.length > 0 && <em>{activeRuns.length}</em>}
          </button>
          <button type="button" className="button primary compact" onClick={() => setSourceOpen(true)}>
            <Plus size={16} />
            데이터 추가
          </button>
        </div>
      </header>

      {connectionIssue && (
        <div className="offline-banner">
          <CloudOff size={14} />
          <span>
            서버 응답이 없어 <strong>읽기 전용 데모 데이터</strong>를 표시하고 있습니다.
          </span>
          <button type="button" onClick={() => setSourceOpen(true)}>
            서버 데이터 다시 연결
          </button>
        </div>
      )}

      <div className="app-grid">
        <DetachablePanel id="data-explorer" title="작업 데이터" placeholderClassName="data-panel-slot">
          {({ action, detached }) => (
            <DatasetPanel
              datasets={datasets}
              selectedDataset={selectedDataset}
              selectedTrack={trackId}
              frames={frames}
              selectedFrame={selectedFrame}
              framesLoading={framesLoading}
              framesLoadingMore={framesLoadingMore}
              frameTotal={frameTotal}
              hasMoreFrames={frameNextOffset !== null}
              frameRange={frameRange}
              focusOverlayLayerId={overlayFocusLayerId}
              removingDataset={removingDatasetId === selectedDataset?.id}
              externalAction={action}
              collapsed={detached ? false : dataPanelCollapsed}
              onDatasetChange={(id) => {
                setFrameRange(null)
                setDatasetId(id)
                setTrackId('')
              }}
              onTrackChange={(id) => {
                setFrameRange(null)
                setTrackId(id)
              }}
              onFrameChange={setSelectedFrame}
              onSetFrameRangeStart={(ordinal) =>
                setFrameRange((current) => [
                  ordinal,
                  current && current[1] >= ordinal ? current[1] : ordinal,
                ])
              }
              onSetFrameRangeEnd={(ordinal) =>
                setFrameRange((current) => [
                  current && current[0] <= ordinal ? current[0] : ordinal,
                  ordinal,
                ])
              }
              onFrameRangeChange={setFrameRange}
              onClearFrameRange={() => setFrameRange(null)}
              onLoadMoreFrames={() => void loadMoreFrames()}
              onOpenSource={() => setSourceOpen(true)}
              onRemoveDataset={(dataset) => void removeDataset(dataset)}
              onToggleCollapsed={
                detached ? undefined : () => setDataPanelCollapsed((value) => !value)
              }
            />
          )}
        </DetachablePanel>
        <DetachablePanel id="workspace" title="공간 데이터 뷰어" placeholderClassName="workspace-slot">
          {({ action, detached }) => (
            <Workspace
              dataset={selectedDataset}
              frames={frames}
              frame={selectedFrame}
              selectedTrack={trackId}
              frameRange={frameRange}
              route={route}
              routeLoading={routeLoading}
              demoMode={demoMode}
              detectionRevisionKey={detectionRevisionKey}
              panoramaOpen={panoramaOpen}
              pointCloudOpen={pointCloudOpen}
              hasMoreFrames={frameNextOffset !== null}
              detached={detached}
              settings={settings}
              externalAction={action}
              onTogglePanorama={() => setPanoramaOpen((value) => !value)}
              onTogglePointCloud={() => setPointCloudOpen((value) => !value)}
              onFrameChange={setSelectedFrame}
              onMoveFrame={moveFrame}
              onOpenSource={() => setSourceOpen(true)}
              onOpenOverlay={() => setOverlayOpen(true)}
              onUseDemo={useDemo}
              onSettingsChange={updateSettings}
            />
          )}
        </DetachablePanel>
      </div>

      {(settingsOpen || settingsDetached) && (
        <div
          className="settings-layer"
          hidden={!settingsOpen || settingsDetached}
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) setSettingsOpen(false)
          }}
        >
          <DetachablePanel
            id="general-settings"
            title="일반 설정"
            placeholderClassName="settings-panel-slot"
            onDetachedChange={setSettingsDetached}
          >
            {({ action, detached, returnToMain }) => {
              const closeSettings = () => {
                if (detached) returnToMain()
                setSettingsOpen(false)
              }
              return (
                <section className="settings-workspace" aria-label="설정 및 작업 설정">
                  <nav className="settings-workspace-tabs" aria-label="설정 항목">
                    <button
                      type="button"
                      className={settingsSection === 'general' ? 'active' : ''}
                      aria-pressed={settingsSection === 'general'}
                      onClick={() => setSettingsSection('general')}
                    >
                      <Settings2 size={15} /> 일반 설정
                    </button>
                    <button
                      type="button"
                      className={settingsSection === 'process' ? 'active' : ''}
                      aria-pressed={settingsSection === 'process'}
                      onClick={() => setSettingsSection('process')}
                    >
                      <SlidersHorizontal size={15} /> 작업 설정
                    </button>
                  </nav>
                  <div className="settings-workspace-content">
                    {settingsSection === 'general' ? (
                      <GeneralSettingsPanel
                        settings={settings}
                        externalAction={action}
                        onChange={updateSettings}
                        onReset={resetSettings}
                        onClose={closeSettings}
                      />
                    ) : (
                      <OptimizationPanel
                        dataset={selectedDataset}
                        selectedTrack={trackId}
                        frameRange={frameRange}
                        busy={submitting}
                        externalAction={
                          <>
                            {action}
                            <button
                              type="button"
                              className="icon-button"
                              aria-label="작업 설정 닫기"
                              title="작업 설정 닫기"
                              onClick={closeSettings}
                            >
                              <X size={16} />
                            </button>
                          </>
                        }
                        onStart={startRun}
                        onOptimize={optimize}
                      />
                    )}
                  </div>
                </section>
              )
            }}
          </DetachablePanel>
        </div>
      )}

      {(overlayOpen || overlayDetached) && (
        <div
          className="utility-layer overlay-manager-layer"
          hidden={!overlayOpen || overlayDetached}
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) setOverlayOpen(false)
          }}
        >
          <DetachablePanel
            ref={overlayPanelRef}
            id="shp-overlay-manager"
            title="SHP 레이어 · 속성표"
            placeholderClassName="overlay-panel-slot"
            onDetachedChange={setOverlayDetached}
          >
            {({ action, detached, returnToMain }) => (
                <OverlayPanel
                  focusLayerId={overlayFocusLayerId}
                  externalAction={action}
                onClose={() => {
                  if (detached) returnToMain()
                  setOverlayOpen(false)
                }}
              />
            )}
          </DetachablePanel>
        </div>
      )}

      {(notificationsOpen || notificationsDetached) && (
        <div
          className="utility-layer"
          hidden={!notificationsOpen || notificationsDetached}
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) setNotificationsOpen(false)
          }}
        >
          <DetachablePanel
            id="activity-center"
            title="알림"
            placeholderClassName="utility-panel-slot"
            onDetachedChange={setNotificationsDetached}
          >
            {({ action, detached, returnToMain }) => {
              const close = () => {
                if (detached) returnToMain()
                setNotificationsOpen(false)
              }
              return (
                <ActivityPanel
                  runs={runs}
                  alerts={notificationLog}
                  detached={detached}
                  externalAction={action}
                  onClose={close}
                  onOpenQueue={() => {
                    close()
                    setQueueOpen(true)
                  }}
                />
              )
            }}
          </DetachablePanel>
        </div>
      )}

      {(helpOpen || helpDetached) && (
        <div
          className="utility-layer"
          hidden={!helpOpen || helpDetached}
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) setHelpOpen(false)
          }}
        >
          <DetachablePanel
            id="operator-help"
            title="도움말"
            placeholderClassName="utility-panel-slot"
            onDetachedChange={setHelpDetached}
          >
            {({ action, detached, returnToMain }) => (
              <HelpPanel
                detached={detached}
                externalAction={action}
                onClose={() => {
                  if (detached) returnToMain()
                  setHelpOpen(false)
                }}
              />
            )}
          </DetachablePanel>
        </div>
      )}

      <StorageDialog
        open={sourceOpen}
        demoMode={demoMode}
        onClose={() => setSourceOpen(false)}
        onDatasetReady={acceptDataset}
        onUseDemo={useDemo}
      />
      <RunQueue
        runs={runs}
        open={queueOpen}
        onClose={() => setQueueOpen(false)}
        onCancel={cancelRun}
        onDelete={dismissRun}
      />
      <ToastRegion toasts={toasts} dismiss={dismissToast} />
      </div>
    </OverlayProvider>
  )
}

export function completedDetectionRevision(runs: RunRecord[], datasetId: string): string {
  return runs
    .filter((run) => run.dataset_id === datasetId && run.status === 'completed')
    .map((run) => `${run.created_at}:${run.id}:${run.finished_at ?? ''}`)
    .sort()
    .join('|')
}

function updateRunFromEvent(current: RunRecord[], runId: string, event: RunEvent): RunRecord[] {
  if (event.run) {
    return [event.run, ...current.filter((run) => run.id !== event.run!.id)]
  }
  return current.map((run) => {
    if (run.id !== runId) return run
    const terminalStatus =
      event.type === 'completed'
        ? 'completed'
        : event.type === 'failed'
          ? 'failed'
          : event.type === 'cancelled'
            ? 'cancelled'
            : run.status
    return {
      ...run,
      status: terminalStatus,
      progress: event.progress ?? (event.type === 'completed' ? 100 : run.progress),
      stage: event.stage ?? run.stage,
      message: event.message ?? run.message,
      result_url: event.result_url ?? run.result_url,
      ...(['completed', 'failed', 'cancelled'].includes(terminalStatus)
        ? { finished_at: new Date().toISOString() }
        : {}),
    }
  })
}

function overlayImageName(properties: Record<string, unknown>): string | undefined {
  const aliases = new Set(['img_name', 'image_name', 'image', 'filename'])
  const match = Object.entries(properties).find(([key, value]) => {
    return aliases.has(key.toLocaleLowerCase('en-US')) && String(value ?? '').trim().length > 0
  })
  return match ? String(match[1]).trim().split(/[\\/]/).at(-1) : undefined
}

function overlayPointXY(feature: OverlayFeature): [number, number] | undefined {
  if (feature.geometry?.type !== 'Point' || !Array.isArray(feature.geometry.coordinates)) {
    return undefined
  }
  const [x, y] = feature.geometry.coordinates
  return Number.isFinite(Number(x)) && Number.isFinite(Number(y))
    ? [Number(x), Number(y)]
    : undefined
}

function resolveNextOffset(page: {
  offset: number
  items: Frame[]
  total: number
  next_offset?: number | null
}): number | null {
  if (page.next_offset !== undefined) return page.next_offset
  const next = page.offset + page.items.length
  return next < page.total ? next : null
}

function BootScreen() {
  return (
    <div className="boot-screen">
      <Brand />
      <span className="boot-orbit">
        <i />
      </span>
      <strong>작업 공간 준비 중</strong>
      <small>서버 기능과 데이터 인덱스를 확인하고 있습니다.</small>
    </div>
  )
}

export default App
