import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { api } from '../lib/api'
import { isTextEntryTarget } from '../lib/frameNavigation'
import type {
  Frame,
  FrameRange,
  ReviewCandidateSources,
  ReviewSession,
  ReviewSessionStatus,
  ReviewTask,
  ReviewTaskResolution,
  ReviewTaskStatus,
} from '../types'
import './ReviewWorkspace.css'

const TERMINAL_TASK_STATUSES: ReadonlySet<ReviewTaskStatus> = new Set([
  'confirmed',
  'corrected',
  'manual_added',
  'false_positive',
  'skipped',
  'field_survey',
])

const REVIEW_SESSION_STORAGE_PREFIX = 'mms.review-session'
const REVIEW_QUEUE_FILTER_STORAGE_PREFIX = 'mms.review-queue-filters'
const REVIEW_TASK_STATUSES: readonly ReviewTaskStatus[] = [
  'todo',
  'in_progress',
  'confirmed',
  'corrected',
  'manual_added',
  'false_positive',
  'skipped',
  'field_survey',
]
const REVIEW_TASK_TYPES: readonly ReviewTask['task_type'][] = [
  'MANUAL_SCAN',
  'LOW_CONFIDENCE',
  'PROJECTION_FAILED',
  'GEOMETRY_REVIEW',
  'POLE_BASE_REVIEW',
  'SPACING_ANOMALY',
  'UNREVIEWED_INTERVAL',
  'MANUAL_FLAG',
]

export function isReviewTaskComplete(task: ReviewTask): boolean {
  return TERMINAL_TASK_STATUSES.has(task.status)
}

export function reviewSessionStorageKey(datasetId: string): string {
  return `${REVIEW_SESSION_STORAGE_PREFIX}:${datasetId}`
}

export function reviewQueueFilterStorageKey(datasetId: string): string {
  return `${REVIEW_QUEUE_FILTER_STORAGE_PREFIX}:${datasetId}`
}

function storedReviewQueueFilters(datasetId: string): {
  status: ReviewTaskStatus | ''
  taskType: ReviewTask['task_type'] | ''
} {
  try {
    const parsed = JSON.parse(
      window.localStorage.getItem(reviewQueueFilterStorageKey(datasetId)) ?? '{}',
    ) as { status?: unknown; taskType?: unknown }
    const status = REVIEW_TASK_STATUSES.includes(parsed.status as ReviewTaskStatus)
      ? (parsed.status as ReviewTaskStatus)
      : ''
    const taskType = REVIEW_TASK_TYPES.includes(parsed.taskType as ReviewTask['task_type'])
      ? (parsed.taskType as ReviewTask['task_type'])
      : ''
    return { status, taskType }
  } catch {
    return { status: '', taskType: '' }
  }
}

function rememberReviewQueueFilters(
  datasetId: string,
  filters: { status: ReviewTaskStatus | ''; taskType: ReviewTask['task_type'] | '' },
): void {
  try {
    window.localStorage.setItem(reviewQueueFilterStorageKey(datasetId), JSON.stringify(filters))
  } catch {
    // Filters remain usable for the current mounted workspace.
  }
}

function storedSessionId(datasetId: string): string {
  try {
    return window.localStorage.getItem(reviewSessionStorageKey(datasetId)) ?? ''
  } catch {
    return ''
  }
}

function rememberSession(datasetId: string, sessionId: string): void {
  try {
    window.localStorage.setItem(reviewSessionStorageKey(datasetId), sessionId)
  } catch {
    // The server-side last_task_id still restores useful progress when storage is unavailable.
  }
}

function preferredSession(
  sessions: ReviewSession[],
  preferredId: string,
): ReviewSession | null {
  return (
    sessions.find((candidate) => candidate.id === preferredId) ??
    sessions.find((candidate) => candidate.status === 'active') ??
    sessions.find((candidate) => candidate.status === 'paused') ??
    sessions.find((candidate) => candidate.status === 'draft') ??
    sessions[0] ??
    null
  )
}

interface ReviewContextValue {
  enabled: boolean
  datasetId: string
  activeFrame: Frame | null
  frameRange: FrameRange | null
  sessions: ReviewSession[]
  session: ReviewSession | null
  tasks: ReviewTask[]
  currentTask: ReviewTask | null
  currentTaskIndex: number
  completedCount: number
  totalCount: number
  progress: number
  loading: boolean
  updatingTaskId: string
  error: string | null
  queueOpen: boolean
  taskStatusFilter: ReviewTaskStatus | ''
  setTaskStatusFilter: (status: ReviewTaskStatus | '') => void
  taskTypeFilter: ReviewTask['task_type'] | ''
  setTaskTypeFilter: (type: ReviewTask['task_type'] | '') => void
  hasMoreTasks: boolean
  loadMoreTasks: () => Promise<ReviewTask[]>
  creatingSession: boolean
  updatingSession: boolean
  generatingCandidates: boolean
  sourceRuns: Array<{ id: string; label: string }>
  setQueueOpen: (open: boolean) => void
  reload: () => void
  selectSession: (sessionId: string) => void
  selectTask: (taskId: string) => void
  navigateFrame: (frameId: string) => Promise<void>
  moveTask: (direction: -1 | 1) => void
  createDefaultSession: (targetLayerId: string, sourceRunIds: string[]) => Promise<void>
  generateCandidates: (sources: ReviewCandidateSources) => Promise<void>
  flagCurrentFrame: (targetLayerId?: string) => Promise<void>
  reopenCurrent: () => Promise<void>
  completeSession: () => Promise<void>
  setSessionStatus: (status: 'active' | 'paused') => Promise<void>
  resolveCurrent: (
    resolution: Extract<ReviewTaskResolution, 'confirmed' | 'false_positive' | 'skipped' | 'field_survey'>,
  ) => Promise<void>
}

const ReviewContext = createContext<ReviewContextValue | null>(null)

export function ReviewProvider({
  enabled,
  datasetId,
  activeFrame = null,
  frameRange = null,
  sourceRuns = [],
  onNavigateFrame,
  notify,
  children,
}: {
  enabled: boolean
  datasetId: string
  activeFrame?: Frame | null
  frameRange?: FrameRange | null
  sourceRuns?: Array<{ id: string; label: string }>
  onNavigateFrame: (frame: Frame, pageOffset: number) => void
  notify?: (entry: { tone: 'success' | 'error' | 'info'; title: string; message?: string }) => void
  children: ReactNode
}) {
  const [sessions, setSessions] = useState<ReviewSession[]>([])
  const [session, setSession] = useState<ReviewSession | null>(null)
  const [tasks, setTasks] = useState<ReviewTask[]>([])
  const [currentTaskId, setCurrentTaskId] = useState('')
  const [loading, setLoading] = useState(false)
  const [updatingTaskId, setUpdatingTaskId] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [queueOpen, setQueueOpen] = useState(true)
  const [taskStatusFilter, setTaskStatusFilterState] = useState<ReviewTaskStatus | ''>(
    () => storedReviewQueueFilters(datasetId).status,
  )
  const [taskTypeFilter, setTaskTypeFilterState] = useState<ReviewTask['task_type'] | ''>(
    () => storedReviewQueueFilters(datasetId).taskType,
  )
  const [taskNextCursor, setTaskNextCursor] = useState<string | null>(null)
  const [taskTotal, setTaskTotal] = useState(0)
  const [statusCounts, setStatusCounts] = useState<Partial<Record<ReviewTaskStatus, number>>>({})
  const [creatingSession, setCreatingSession] = useState(false)
  const [updatingSession, setUpdatingSession] = useState(false)
  const [generatingCandidates, setGeneratingCandidates] = useState(false)
  const [reloadToken, setReloadToken] = useState(0)
  const preferredSessionRef = useRef<{ datasetId: string; sessionId: string } | null>(null)
  const loadRequestRef = useRef(0)
  const loadControllerRef = useRef<AbortController | null>(null)
  const navigationRequestRef = useRef(0)
  const navigationControllerRef = useRef<AbortController | null>(null)
  const taskPageLoadingRef = useRef(false)
  const taskQueryGenerationRef = useRef(0)
  const activeDatasetIdRef = useRef(datasetId)
  activeDatasetIdRef.current = datasetId
  const filterDatasetIdRef = useRef(datasetId)

  useEffect(() => {
    if (filterDatasetIdRef.current === datasetId) return
    filterDatasetIdRef.current = datasetId
    const stored = storedReviewQueueFilters(datasetId)
    taskQueryGenerationRef.current += 1
    setTaskStatusFilterState(stored.status)
    setTaskTypeFilterState(stored.taskType)
    setCurrentTaskId('')
    setTasks([])
    setTaskNextCursor(null)
  }, [datasetId])

  const navigateFrame = useCallback(
    async (frameId: string) => {
      navigationRequestRef.current += 1
      const requestId = navigationRequestRef.current
      navigationControllerRef.current?.abort()
      navigationControllerRef.current = null
      if (!enabled || !datasetId || !frameId) return
      const controller = new AbortController()
      navigationControllerRef.current = controller
      try {
        const response = await api.reviewTaskFrame(datasetId, frameId, controller.signal)
        if (
          controller.signal.aborted ||
          navigationRequestRef.current !== requestId ||
          activeDatasetIdRef.current !== datasetId
        ) {
          return
        }
        onNavigateFrame(response.frame, response.page_offset)
      } catch (reason) {
        if (controller.signal.aborted || navigationRequestRef.current !== requestId) return
        const message = reason instanceof Error ? reason.message : '검수 항목의 프레임을 찾지 못했습니다.'
        setError(message)
        notify?.({ tone: 'error', title: '검수 프레임으로 이동하지 못했습니다', message })
      } finally {
        if (navigationControllerRef.current === controller) navigationControllerRef.current = null
      }
    },
    [datasetId, enabled, notify, onNavigateFrame],
  )

  const navigateToTask = useCallback(
    async (task: ReviewTask) => {
      if (!task.frame_id) return
      await navigateFrame(task.frame_id)
    },
    [navigateFrame],
  )

  const claimTask = useCallback(
    (task: ReviewTask, sessionStatus: ReviewSessionStatus) => {
      if (sessionStatus !== 'active' || task.status !== 'todo') return
      setUpdatingTaskId(task.id)
      setTasks((current) =>
        current.map((candidate) =>
          candidate.id === task.id ? { ...candidate, status: 'in_progress' } : candidate,
        ),
      )
      void api
        .patchReviewTask(task.id, { status: 'in_progress', claimed_by: 'operator-local' })
        .then((response) => {
          setTasks((current) =>
            current.map((candidate) =>
              candidate.id === response.task.id ? response.task : candidate,
            ),
          )
        })
        .catch((reason: unknown) => {
          setTasks((current) =>
            current.map((candidate) => (candidate.id === task.id ? task : candidate)),
          )
          notify?.({
            tone: 'error',
            title: '검수 항목을 시작하지 못했습니다',
            message: reason instanceof Error ? reason.message : undefined,
          })
        })
        .finally(() => {
          setUpdatingTaskId((current) => (current === task.id ? '' : current))
        })
    },
    [notify],
  )

  useEffect(() => {
    taskQueryGenerationRef.current += 1
    loadRequestRef.current += 1
    const requestId = loadRequestRef.current
    loadControllerRef.current?.abort()
    navigationRequestRef.current += 1
    navigationControllerRef.current?.abort()
    navigationControllerRef.current = null

    if (!enabled || !datasetId) {
      setSessions([])
      setSession(null)
      setTasks([])
      setTaskNextCursor(null)
      setTaskTotal(0)
      setStatusCounts({})
      setCurrentTaskId('')
      setLoading(false)
      setUpdatingTaskId('')
      setError(null)
      return
    }

    const controller = new AbortController()
    loadControllerRef.current = controller
    setLoading(true)
    setError(null)
    void (async () => {
      try {
        const sessionPage = await api.reviewSessions(datasetId, 0, 100, controller.signal)
        if (controller.signal.aborted || loadRequestRef.current !== requestId) return
        setSessions(sessionPage.items)
        const requested = preferredSessionRef.current
        const rememberedId =
          requested?.datasetId === datasetId ? requested.sessionId : storedSessionId(datasetId)
        const selectedSession = preferredSession(sessionPage.items, rememberedId)
        if (!selectedSession) {
          setSession(null)
          setTasks([])
          setTaskNextCursor(null)
          setTaskTotal(0)
          setStatusCounts({})
          setCurrentTaskId('')
          return
        }
        const [sessionResponse, taskPage] = await Promise.all([
          api.reviewSession(selectedSession.id, controller.signal),
          api.reviewTasks(
            selectedSession.id,
            0,
            200,
            controller.signal,
            {
              status: taskStatusFilter || undefined,
              task_type: taskTypeFilter || undefined,
            },
          ),
        ])
        if (
          controller.signal.aborted ||
          loadRequestRef.current !== requestId ||
          activeDatasetIdRef.current !== datasetId
        ) {
          return
        }
        const restoredSession = sessionResponse.session
        let loadedTasks = taskPage.items
        if (
          !taskStatusFilter &&
          !taskTypeFilter &&
          restoredSession.last_task_id &&
          !loadedTasks.some((task) => task.id === restoredSession.last_task_id)
        ) {
          try {
            const restored = await api.reviewTask(restoredSession.last_task_id, controller.signal)
            if (restored.task.session_id === restoredSession.id) {
              loadedTasks = [restored.task, ...loadedTasks]
            }
          } catch {
            // A stale last_task_id must not block the first bounded queue page.
          }
        }
        const restoredTask =
          (!taskStatusFilter && !taskTypeFilter
            ? loadedTasks.find((task) => task.id === restoredSession.last_task_id)
            : null) ??
          loadedTasks.find((task) => !isReviewTaskComplete(task)) ??
          loadedTasks[0] ??
          null
        setSession(restoredSession)
        setTasks(loadedTasks)
        setTaskNextCursor(taskPage.next_cursor ?? null)
        setTaskTotal(taskPage.total)
        setStatusCounts(taskPage.status_counts ?? {})
        setCurrentTaskId(restoredTask?.id ?? '')
        rememberSession(datasetId, restoredSession.id)
        if (restoredTask) {
          claimTask(restoredTask, restoredSession.status)
          void navigateToTask(restoredTask)
        }
      } catch (reason) {
        if (controller.signal.aborted || loadRequestRef.current !== requestId) return
        const message = reason instanceof Error ? reason.message : '검수 작업 목록을 불러오지 못했습니다.'
        setSessions([])
        setSession(null)
        setTasks([])
        setTaskNextCursor(null)
        setTaskTotal(0)
        setStatusCounts({})
        setCurrentTaskId('')
        setError(message)
        notify?.({ tone: 'error', title: '검수 작업을 불러오지 못했습니다', message })
      } finally {
        if (loadRequestRef.current === requestId) setLoading(false)
        if (loadControllerRef.current === controller) loadControllerRef.current = null
      }
    })()

    return () => controller.abort()
  }, [claimTask, datasetId, enabled, navigateToTask, notify, reloadToken, taskStatusFilter, taskTypeFilter])

  useEffect(
    () => () => {
      loadControllerRef.current?.abort()
      navigationControllerRef.current?.abort()
    },
    [],
  )

  const currentTaskIndex = tasks.findIndex((task) => task.id === currentTaskId)
  const currentTask = currentTaskIndex >= 0 ? tasks[currentTaskIndex] : null
  const aggregateTotal = Object.values(statusCounts).reduce((sum, count) => sum + (count ?? 0), 0)
  const aggregateCompleted = [...TERMINAL_TASK_STATUSES].reduce(
    (sum, status) => sum + (statusCounts[status] ?? 0),
    0,
  )
  const completedCount = aggregateTotal > 0 ? aggregateCompleted : tasks.filter(isReviewTaskComplete).length
  const totalCount = aggregateTotal > 0 ? aggregateTotal : taskTotal
  const progress = totalCount > 0 ? completedCount / totalCount : 0

  const persistTaskSelection = useCallback(
    (selectedSession: ReviewSession, task: ReviewTask) => {
      setSession((current) =>
        current?.id === selectedSession.id ? { ...current, last_task_id: task.id } : current,
      )
      void api
        .patchReviewSession(selectedSession.id, { last_task_id: task.id })
        .then((response) => {
          setSession((current) =>
            current?.id === response.session.id ? response.session : current,
          )
        })
        .catch((reason: unknown) => {
          notify?.({
            tone: 'error',
            title: '마지막 검수 위치를 저장하지 못했습니다',
            message: reason instanceof Error ? reason.message : undefined,
          })
        })
    },
    [notify],
  )

  const activateTask = useCallback(
    (task: ReviewTask) => {
      if (!session) return
      setCurrentTaskId(task.id)
      setError(null)
      rememberSession(datasetId, session.id)
      persistTaskSelection(session, task)
      void navigateToTask(task)
      claimTask(task, session.status)
    },
    [claimTask, datasetId, navigateToTask, persistTaskSelection, session],
  )

  const selectTask = useCallback(
    (taskId: string) => {
      const task = tasks.find((candidate) => candidate.id === taskId)
      if (task) activateTask(task)
    },
    [activateTask, tasks],
  )

  const loadMoreTasks = useCallback(async (): Promise<ReviewTask[]> => {
    if (!session || taskNextCursor === null || taskPageLoadingRef.current) return []
    const queryGeneration = taskQueryGenerationRef.current
    taskPageLoadingRef.current = true
    try {
      const page = await api.reviewTasks(
        session.id,
        0,
        200,
        undefined,
        {
          status: taskStatusFilter || undefined,
          task_type: taskTypeFilter || undefined,
          cursor: taskNextCursor,
        },
      )
      if (taskQueryGenerationRef.current !== queryGeneration) return []
      setTasks((current) => {
        const known = new Set(current.map((task) => task.id))
        return [...current, ...page.items.filter((task) => !known.has(task.id))]
      })
      setTaskNextCursor(page.next_cursor ?? null)
      setTaskTotal(page.total)
      if (page.status_counts) setStatusCounts(page.status_counts)
      return page.items
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : '다음 검수 작업을 불러오지 못했습니다.'
      setError(message)
      return []
    } finally {
      taskPageLoadingRef.current = false
    }
  }, [session, taskNextCursor, taskStatusFilter, taskTypeFilter])

  const setTaskStatusFilter = useCallback((status: ReviewTaskStatus | '') => {
    taskQueryGenerationRef.current += 1
    setTaskStatusFilterState(status)
    rememberReviewQueueFilters(datasetId, { status, taskType: taskTypeFilter })
    setCurrentTaskId('')
    setTasks([])
    setTaskNextCursor(null)
  }, [datasetId, taskTypeFilter])

  const setTaskTypeFilter = useCallback((type: ReviewTask['task_type'] | '') => {
    taskQueryGenerationRef.current += 1
    setTaskTypeFilterState(type)
    rememberReviewQueueFilters(datasetId, { status: taskStatusFilter, taskType: type })
    setCurrentTaskId('')
    setTasks([])
    setTaskNextCursor(null)
  }, [datasetId, taskStatusFilter])

  const moveTask = useCallback(
    (direction: -1 | 1) => {
      if (!tasks.length || updatingTaskId) return
      const index = currentTaskIndex < 0 ? (direction === 1 ? -1 : tasks.length) : currentTaskIndex
      const next = tasks[index + direction]
      if (next) {
        activateTask(next)
      } else if (direction === 1 && taskNextCursor !== null) {
        void loadMoreTasks().then((loaded) => {
          const first = loaded[0]
          if (first) activateTask(first)
        })
      }
    },
    [activateTask, currentTaskIndex, loadMoreTasks, taskNextCursor, tasks, updatingTaskId],
  )

  const resolveCurrent = useCallback(
    async (
      resolution: Extract<ReviewTaskResolution, 'confirmed' | 'false_positive' | 'skipped' | 'field_survey'>,
    ) => {
      if (!currentTask || !session || session.status !== 'active' || updatingTaskId) return
      const task = currentTask
      setUpdatingTaskId(task.id)
      setError(null)
      try {
        const response = await api.resolveReviewTask(task.id, { resolution })
        const resolvedTask = response.task
        setStatusCounts((current) => ({
          ...current,
          [task.status]: Math.max(0, (current[task.status] ?? 0) - 1),
          [resolvedTask.status]: (current[resolvedTask.status] ?? 0) + 1,
        }))
        const nextTasks = tasks.map((candidate) =>
          candidate.id === resolvedTask.id ? resolvedTask : candidate,
        )
        setTasks(nextTasks)
        const resolvedIndex = nextTasks.findIndex((candidate) => candidate.id === resolvedTask.id)
        const nextTask =
          nextTasks.slice(resolvedIndex + 1).find((candidate) => !isReviewTaskComplete(candidate)) ??
          nextTasks.slice(0, resolvedIndex).find((candidate) => !isReviewTaskComplete(candidate)) ??
          null
        if (nextTask && session) {
          setCurrentTaskId(nextTask.id)
          persistTaskSelection(session, nextTask)
          claimTask(nextTask, session.status)
          void navigateToTask(nextTask)
        } else if (taskNextCursor !== null) {
          const loaded = await loadMoreTasks()
          const nextLoaded = loaded.find((candidate) => !isReviewTaskComplete(candidate))
          if (nextLoaded) activateTask(nextLoaded)
          else setCurrentTaskId(resolvedTask.id)
        } else {
          setCurrentTaskId(resolvedTask.id)
        }
        notify?.({
          tone: 'success',
          title:
            resolution === 'confirmed'
              ? '검수 항목을 완료했습니다'
              : resolution === 'false_positive'
                ? '오검출 항목으로 처리했습니다'
              : resolution === 'skipped'
                ? '검수 항목을 건너뛰었습니다'
                : '현장조사 필요 항목으로 분류했습니다',
        })
      } catch (reason) {
        const message = reason instanceof Error ? reason.message : '검수 상태를 저장하지 못했습니다.'
        setError(message)
        notify?.({ tone: 'error', title: '검수 상태를 저장하지 못했습니다', message })
      } finally {
        setUpdatingTaskId((current) => (current === task.id ? '' : current))
      }
    },
    [
      currentTask,
      activateTask,
      claimTask,
      loadMoreTasks,
      navigateToTask,
      notify,
      persistTaskSelection,
      session,
      tasks,
      taskNextCursor,
      updatingTaskId,
    ],
  )

  const createDefaultSession = useCallback(async (targetLayerId: string, sourceRunIds: string[]) => {
    if (
      !enabled ||
      !datasetId ||
      !activeFrame ||
      !targetLayerId ||
      sourceRunIds.length === 0 ||
      creatingSession
    ) return
    setCreatingSession(true)
    setError(null)
    try {
      const response = await api.createReviewSession(datasetId, {
        source_run_ids: sourceRunIds,
        target_layer_ids: [targetLayerId],
        track_ids: [activeFrame.track_id],
        frame_range: frameRange ?? [activeFrame.index, activeFrame.index],
        class_filters: ['TRAFFIC_SIGN', 'SIGN_SUPPORT_POLE'],
        status: 'active',
        created_by: 'operator-local',
      })
      preferredSessionRef.current = { datasetId, sessionId: response.session.id }
      rememberSession(datasetId, response.session.id)
      setReloadToken((value) => value + 1)
      notify?.({ tone: 'success', title: '현재 작업 범위로 검수 세션을 만들었습니다.' })
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : '검수 세션을 만들지 못했습니다.'
      setError(message)
      notify?.({ tone: 'error', title: '검수 세션을 만들지 못했습니다', message })
    } finally {
      setCreatingSession(false)
    }
  }, [activeFrame, creatingSession, datasetId, enabled, frameRange, notify])

  const generateCandidates = useCallback(async (sources: ReviewCandidateSources) => {
    if (!session || session.status !== 'active' || generatingCandidates) return
    setGeneratingCandidates(true)
    setError(null)
    try {
      const response = await api.generateReviewTasks(session.id, {
        sources,
        low_confidence_threshold: 0.5,
        unreviewed_interval_frames: 50,
      })
      setReloadToken((value) => value + 1)
      notify?.({
        tone: 'success',
        title: `검수 후보 ${response.created.toLocaleString('ko-KR')}개를 추가했습니다`,
        message: response.existing ? `기존 후보 ${response.existing.toLocaleString('ko-KR')}개는 유지했습니다.` : undefined,
      })
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : '검수 후보를 생성하지 못했습니다.'
      setError(message)
      notify?.({ tone: 'error', title: '검수 후보를 생성하지 못했습니다', message })
    } finally {
      setGeneratingCandidates(false)
    }
  }, [generatingCandidates, notify, session])

  const flagCurrentFrame = useCallback(async (targetLayerId?: string) => {
    if (
      !session ||
      session.status !== 'active' ||
      !activeFrame ||
      generatingCandidates
    ) return
    setGeneratingCandidates(true)
    setError(null)
    try {
      await api.generateReviewTasks(session.id, {
        tasks: [{
          task_type: 'MANUAL_FLAG',
          priority: 80,
          frame_id: activeFrame.id,
          track_id: activeFrame.track_id,
          target_layer_id: targetLayerId ?? session.target_layer_ids[0],
          class_hint: session.class_filters[0],
          reason_codes: ['OPERATOR_FLAGGED'],
        }],
      })
      setReloadToken((value) => value + 1)
      notify?.({ tone: 'success', title: '현재 프레임을 나중에 확인할 항목으로 추가했습니다.' })
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : '현재 프레임을 표시하지 못했습니다.'
      setError(message)
      notify?.({ tone: 'error', title: '나중에 확인 항목을 만들지 못했습니다', message })
    } finally {
      setGeneratingCandidates(false)
    }
  }, [activeFrame, generatingCandidates, notify, session])

  const reopenCurrent = useCallback(async () => {
    if (
      !session ||
      session.status !== 'active' ||
      !currentTask ||
      !isReviewTaskComplete(currentTask) ||
      updatingTaskId
    ) return
    setUpdatingTaskId(currentTask.id)
    setError(null)
    try {
      const response = await api.reopenReviewTask(currentTask.id)
      setTasks((current) => current.map((task) => task.id === response.task.id ? response.task : task))
      setStatusCounts((current) => ({
        ...current,
        [currentTask.status]: Math.max(0, (current[currentTask.status] ?? 0) - 1),
        todo: (current.todo ?? 0) + 1,
      }))
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : '검수 항목을 다시 열지 못했습니다.'
      setError(message)
    } finally {
      setUpdatingTaskId((current) => current === currentTask.id ? '' : current)
    }
  }, [currentTask, session, updatingTaskId])

  const completeSession = useCallback(async () => {
    if (!session || updatingSession) return
    setUpdatingSession(true)
    setError(null)
    try {
      const response = await api.patchReviewSession(session.id, { status: 'completed' })
      setSession(response.session)
      setSessions((current) => current.map((candidate) => candidate.id === response.session.id ? response.session : candidate))
      notify?.({ tone: 'success', title: '검수 세션을 완료했습니다.' })
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : '검수 세션을 완료하지 못했습니다.'
      setError(message)
      notify?.({ tone: 'error', title: '세션 완료 조건을 확인해 주세요', message })
    } finally {
      setUpdatingSession(false)
    }
  }, [notify, session, updatingSession])

  const setSessionStatus = useCallback(async (status: 'active' | 'paused') => {
    if (!session || updatingSession || session.status === status) return
    setUpdatingSession(true)
    setError(null)
    try {
      const response = await api.patchReviewSession(session.id, { status })
      setSession(response.session)
      setSessions((current) => current.map((candidate) =>
        candidate.id === response.session.id ? response.session : candidate,
      ))
      notify?.({
        tone: 'success',
        title: status === 'paused' ? '검수 세션을 일시 정지했습니다.' : '검수 세션을 재개했습니다.',
      })
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : '검수 세션 상태를 바꾸지 못했습니다.'
      setError(message)
      notify?.({ tone: 'error', title: '검수 세션 상태를 바꾸지 못했습니다', message })
    } finally {
      setUpdatingSession(false)
    }
  }, [notify, session, updatingSession])

  const shortcutStateRef = useRef({ enabled, moveTask, resolveCurrent, flagCurrentFrame, currentTask })
  shortcutStateRef.current = { enabled, moveTask, resolveCurrent, flagCurrentFrame, currentTask }
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (
        event.defaultPrevented ||
        event.repeat ||
        event.altKey ||
        event.ctrlKey ||
        event.metaKey ||
        event.shiftKey ||
        isTextEntryTarget(event.target) ||
        !shortcutStateRef.current.enabled
      ) {
        return
      }
      if (event.code === 'KeyJ') {
        event.preventDefault()
        shortcutStateRef.current.moveTask(1)
      } else if (event.code === 'KeyK') {
        event.preventDefault()
        shortcutStateRef.current.moveTask(-1)
      } else if (event.code === 'KeyX' && shortcutStateRef.current.currentTask?.status === 'in_progress') {
        event.preventDefault()
        void shortcutStateRef.current.resolveCurrent('false_positive')
      } else if (event.code === 'KeyF') {
        event.preventDefault()
        void shortcutStateRef.current.flagCurrentFrame()
      }
    }
    window.addEventListener('keydown', onKeyDown, true)
    return () => window.removeEventListener('keydown', onKeyDown, true)
  }, [])

  const value = useMemo<ReviewContextValue>(
    () => ({
      enabled,
      datasetId,
      activeFrame,
      frameRange,
      sessions,
      session,
      tasks,
      currentTask,
      currentTaskIndex,
      completedCount,
      totalCount,
      progress,
      loading,
      updatingTaskId,
      error,
      queueOpen,
      taskStatusFilter,
      setTaskStatusFilter,
      taskTypeFilter,
      setTaskTypeFilter,
      hasMoreTasks: taskNextCursor !== null,
      loadMoreTasks,
      creatingSession,
      updatingSession,
      generatingCandidates,
      sourceRuns,
      setQueueOpen,
      reload: () => {
        taskQueryGenerationRef.current += 1
        setReloadToken((value) => value + 1)
      },
      selectSession: (sessionId: string) => {
        taskQueryGenerationRef.current += 1
        preferredSessionRef.current = { datasetId, sessionId }
        rememberSession(datasetId, sessionId)
        setReloadToken((value) => value + 1)
      },
      selectTask,
      navigateFrame,
      moveTask,
      createDefaultSession,
      generateCandidates,
      flagCurrentFrame,
      reopenCurrent,
      completeSession,
      setSessionStatus,
      resolveCurrent,
    }),
    [
      completedCount,
      completeSession,
      createDefaultSession,
      creatingSession,
      currentTask,
      currentTaskIndex,
      datasetId,
      enabled,
      error,
      flagCurrentFrame,
      generateCandidates,
      generatingCandidates,
      activeFrame,
      frameRange,
      sourceRuns,
      loadMoreTasks,
      loading,
      moveTask,
      navigateFrame,
      progress,
      queueOpen,
      reopenCurrent,
      resolveCurrent,
      selectTask,
      setTaskStatusFilter,
      setTaskTypeFilter,
      session,
      sessions,
      tasks,
      taskNextCursor,
      taskStatusFilter,
      taskTypeFilter,
      totalCount,
      setSessionStatus,
      updatingSession,
      updatingTaskId,
    ],
  )

  return <ReviewContext.Provider value={value}>{children}</ReviewContext.Provider>
}

export function useReviewWorkspace(): ReviewContextValue {
  const value = useContext(ReviewContext)
  if (!value) throw new Error('useReviewWorkspace must be used inside ReviewProvider.')
  return value
}

export function useOptionalReviewWorkspace(): ReviewContextValue | null {
  return useContext(ReviewContext)
}
