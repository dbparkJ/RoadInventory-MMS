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
import { hasOpenModalDialog, isWorkspaceShortcutBlockedTarget } from '../lib/frameNavigation'
import type {
  Frame,
  FrameRange,
  QaRunResponse,
  ReviewCandidateSources,
  ReviewCompletionStatus,
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

interface ReviewSessionScopeToken {
  datasetId: string
  sessionId: string
  generation: number
}

export function isReviewTaskComplete(task: ReviewTask): boolean {
  return TERMINAL_TASK_STATUSES.has(task.status)
}

export function reviewCompletionBlockerMessages(blockers: Record<string, number>): string[] {
  return Object.entries(blockers).flatMap(([name, count]) => {
    if (!count) return []
    switch (name) {
      case 'open_tasks':
        return [`미처리 검수 항목 ${count.toLocaleString('ko-KR')}개`]
      case 'open_error_qa_issues':
        return [`미해결 QA 오류 ${count.toLocaleString('ko-KR')}개`]
      case 'qa_not_run':
        return ['QA 검사를 아직 실행하지 않음']
      case 'stale_qa_target_layers':
        return [`QA 이후 변경된 레이어 ${count.toLocaleString('ko-KR')}개`]
      case 'pending_task_resolutions':
        return [`저장 동기화 대기 ${count.toLocaleString('ko-KR')}건`]
      case 'task_resolution_errors':
        return [`저장 동기화 오류 ${count.toLocaleString('ko-KR')}건`]
      case 'task_resolution_scan_truncated':
        return ['저장 동기화 상태를 모두 확인하지 못함']
      default:
        return [`${name} ${count.toLocaleString('ko-KR')}건`]
    }
  })
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
  completionStatus: ReviewCompletionStatus | null
  checkingCompletion: boolean
  startGuideOpen: boolean
  setStartGuideOpen: (open: boolean) => void
  candidateGuideOpen: boolean
  setCandidateGuideOpen: (open: boolean) => void
  sourceRuns: Array<{ id: string; label: string }>
  setQueueOpen: (open: boolean) => void
  reload: () => void
  selectSession: (sessionId: string) => void
  selectTask: (taskId: string) => void
  navigateFrame: (frameId: string) => Promise<void>
  moveTask: (direction: -1 | 1) => void
  createDefaultSession: (targetLayerId: string, sourceRunIds: string[]) => Promise<void>
  startReviewWork: (
    targetLayerId: string,
    sourceRunIds: string[],
    sources: ReviewCandidateSources,
  ) => Promise<boolean>
  generateCandidates: (sources: ReviewCandidateSources) => Promise<boolean>
  flagCurrentFrame: (targetLayerId?: string) => Promise<void>
  reopenCurrent: () => Promise<void>
  refreshCompletionStatus: () => Promise<ReviewCompletionStatus | null>
  recordQaRun: (result: QaRunResponse, sessionId?: string) => Promise<void>
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
  const [completionStatus, setCompletionStatus] = useState<ReviewCompletionStatus | null>(null)
  const [checkingCompletion, setCheckingCompletion] = useState(false)
  const [startGuideOpen, setStartGuideOpen] = useState(false)
  const [candidateGuideOpen, setCandidateGuideOpen] = useState(false)
  const [reloadToken, setReloadToken] = useState(0)
  const preferredSessionRef = useRef<{ datasetId: string; sessionId: string } | null>(null)
  const loadRequestRef = useRef(0)
  const loadControllerRef = useRef<AbortController | null>(null)
  const navigationRequestRef = useRef(0)
  const navigationControllerRef = useRef<AbortController | null>(null)
  const taskPageLoadingRef = useRef(false)
  const taskQueryGenerationRef = useRef(0)
  const completionRequestRef = useRef(0)
  const completionControllerRef = useRef<AbortController | null>(null)
  const startWorkBusyRef = useRef(false)
  const startWorkRequestRef = useRef(0)
  const startWorkControllerRef = useRef<AbortController | null>(null)
  const reopenRequestRef = useRef(0)
  const reopenControllerRef = useRef<AbortController | null>(null)
  const taskSelectionSequenceRef = useRef(0)
  const latestTaskSelectionRef = useRef(new Map<string, { datasetId: string; taskId: string; sequence: number }>())
  const taskSelectionWriteChainRef = useRef<Promise<void>>(Promise.resolve())
  const activeDatasetIdRef = useRef(datasetId)
  activeDatasetIdRef.current = datasetId
  const activeCurrentTaskIdRef = useRef(currentTaskId)
  activeCurrentTaskIdRef.current = currentTaskId
  const selectedSessionScopeRef = useRef({ datasetId, sessionId: '' })
  const sessionScopeGenerationRef = useRef(0)
  if (selectedSessionScopeRef.current.datasetId !== datasetId) {
    sessionScopeGenerationRef.current += 1
    selectedSessionScopeRef.current = { datasetId, sessionId: '' }
  }
  const filterDatasetIdRef = useRef(datasetId)

  const selectSessionScope = useCallback((nextDatasetId: string, nextSessionId: string, force = false) => {
    const current = selectedSessionScopeRef.current
    if (force || current.datasetId !== nextDatasetId || current.sessionId !== nextSessionId) {
      sessionScopeGenerationRef.current += 1
    }
    selectedSessionScopeRef.current = { datasetId: nextDatasetId, sessionId: nextSessionId }
  }, [])

  const captureSessionScope = useCallback((scopeDatasetId: string, scopeSessionId: string): ReviewSessionScopeToken => ({
    datasetId: scopeDatasetId,
    sessionId: scopeSessionId,
    generation: sessionScopeGenerationRef.current,
  }), [])

  const isCurrentSessionScope = useCallback((scope: ReviewSessionScopeToken) => (
    sessionScopeGenerationRef.current === scope.generation &&
    activeDatasetIdRef.current === scope.datasetId &&
    selectedSessionScopeRef.current.datasetId === scope.datasetId &&
    selectedSessionScopeRef.current.sessionId === scope.sessionId
  ), [])

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
    setCompletionStatus(null)
    setStartGuideOpen(false)
    setCandidateGuideOpen(false)
  }, [datasetId])

  useEffect(() => {
    startWorkRequestRef.current += 1
    startWorkControllerRef.current?.abort()
    startWorkControllerRef.current = null
    startWorkBusyRef.current = false
    setCreatingSession(false)
    setGeneratingCandidates(false)
  }, [datasetId, enabled])

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
    (task: ReviewTask, sessionStatus: ReviewSessionStatus, sessionId: string) => {
      if (
        sessionStatus !== 'active' ||
        task.status !== 'todo' ||
        startWorkBusyRef.current
      ) return
      const scope = captureSessionScope(datasetId, sessionId)
      const queryGeneration = taskQueryGenerationRef.current
      const isCurrentRequest = () => (
        isCurrentSessionScope(scope) && taskQueryGenerationRef.current === queryGeneration
      )
      setUpdatingTaskId(task.id)
      setTasks((current) =>
        current.map((candidate) =>
          candidate.id === task.id ? { ...candidate, status: 'in_progress' } : candidate,
        ),
      )
      setStatusCounts((current) => ({
        ...current,
        todo: Math.max(0, (current.todo ?? 0) - 1),
        in_progress: (current.in_progress ?? 0) + 1,
      }))
      void api
        .patchReviewTask(task.id, { status: 'in_progress', claimed_by: 'operator-local' })
        .then((response) => {
          if (!isCurrentRequest()) return
          setTasks((current) =>
            current.map((candidate) =>
              candidate.id === response.task.id ? response.task : candidate,
            ),
          )
        })
        .catch((reason: unknown) => {
          if (!isCurrentRequest()) return
          setTasks((current) =>
            current.map((candidate) => (candidate.id === task.id ? task : candidate)),
          )
          setStatusCounts((current) => ({
            ...current,
            todo: (current.todo ?? 0) + 1,
            in_progress: Math.max(0, (current.in_progress ?? 0) - 1),
          }))
          notify?.({
            tone: 'error',
            title: '검수 항목을 시작하지 못했습니다',
            message: reason instanceof Error ? reason.message : undefined,
          })
        })
        .finally(() => {
          if (isCurrentRequest()) {
            setUpdatingTaskId((current) => (current === task.id ? '' : current))
          }
        })
    },
    [captureSessionScope, datasetId, isCurrentSessionScope, notify],
  )

  useEffect(() => {
    taskQueryGenerationRef.current += 1
    completionRequestRef.current += 1
    completionControllerRef.current?.abort()
    completionControllerRef.current = null
    reopenRequestRef.current += 1
    reopenControllerRef.current?.abort()
    reopenControllerRef.current = null
    setUpdatingTaskId('')
    setUpdatingSession(false)
    setGeneratingCandidates(false)
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
      setCompletionStatus(null)
      setCheckingCompletion(false)
      selectSessionScope(datasetId, '')
      return
    }

    const controller = new AbortController()
    loadControllerRef.current = controller
    setLoading(true)
    setCompletionStatus(null)
    setCheckingCompletion(false)
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
          selectSessionScope(datasetId, '')
          setSession(null)
          setTasks([])
          setTaskNextCursor(null)
          setTaskTotal(0)
          setStatusCounts({})
          setCurrentTaskId('')
          setCompletionStatus(null)
          return
        }
        selectSessionScope(datasetId, selectedSession.id)
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
        selectSessionScope(datasetId, restoredSession.id)
        setSession(restoredSession)
        setTasks(loadedTasks)
        setTaskNextCursor(taskPage.next_cursor ?? null)
        setTaskTotal(taskPage.total)
        setStatusCounts(taskPage.status_counts ?? {})
        setCurrentTaskId(restoredTask?.id ?? '')
        rememberSession(datasetId, restoredSession.id)
        if (restoredTask) {
          claimTask(restoredTask, restoredSession.status, restoredSession.id)
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
        selectSessionScope(datasetId, '')
        setError(message)
        notify?.({ tone: 'error', title: '검수 작업을 불러오지 못했습니다', message })
      } finally {
        if (loadRequestRef.current === requestId) setLoading(false)
        if (loadControllerRef.current === controller) loadControllerRef.current = null
      }
    })()

    return () => controller.abort()
  }, [claimTask, datasetId, enabled, navigateToTask, notify, reloadToken, selectSessionScope, taskStatusFilter, taskTypeFilter])

  useEffect(
    () => () => {
      loadControllerRef.current?.abort()
      navigationControllerRef.current?.abort()
      completionControllerRef.current?.abort()
      startWorkControllerRef.current?.abort()
      reopenControllerRef.current?.abort()
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

  const refreshCompletionStatus = useCallback(async (): Promise<ReviewCompletionStatus | null> => {
    if (!session) {
      setCompletionStatus(null)
      return null
    }
    const requestDatasetId = datasetId
    const sessionId = session.id
    const scope = captureSessionScope(requestDatasetId, sessionId)
    if (!isCurrentSessionScope(scope)) return null
    const requestId = completionRequestRef.current + 1
    completionRequestRef.current = requestId
    completionControllerRef.current?.abort()
    const controller = new AbortController()
    completionControllerRef.current = controller
    setCheckingCompletion(true)
    try {
      const result = await api.reviewCompletionStatus(sessionId, controller.signal)
      if (
        controller.signal.aborted ||
        completionRequestRef.current !== requestId ||
        !isCurrentSessionScope(scope)
      ) return null
      setCompletionStatus(result)
      return result
    } catch (reason) {
      if (
        controller.signal.aborted ||
        completionRequestRef.current !== requestId ||
        !isCurrentSessionScope(scope)
      ) return null
      const message = reason instanceof Error
        ? reason.message
        : '검수 작업의 완료 조건을 확인하지 못했습니다.'
      setCompletionStatus(null)
      setError(message)
      return null
    } finally {
      if (
        completionRequestRef.current === requestId &&
        isCurrentSessionScope(scope)
      ) setCheckingCompletion(false)
      if (completionControllerRef.current === controller) completionControllerRef.current = null
    }
  }, [captureSessionScope, datasetId, isCurrentSessionScope, session])

  useEffect(() => {
    if (
      !session ||
      !['active', 'paused'].includes(session.status) ||
      completedCount < totalCount
    ) {
      setCompletionStatus(null)
      return
    }
    void refreshCompletionStatus()
  }, [completedCount, refreshCompletionStatus, session, totalCount])

  const recordQaRun = useCallback(async (result: QaRunResponse, expectedSessionId?: string) => {
    if (!session) return
    const requestDatasetId = datasetId
    const sessionId = expectedSessionId ?? session.id
    const scope = captureSessionScope(requestDatasetId, sessionId)
    if (session.id !== sessionId || !isCurrentSessionScope(scope)) return
    const qaPatch = {
      qa_ran_at: result.ran_at,
      qa_layer_revisions: result.layer_revisions,
    }
    setSession((current) => current?.id === sessionId ? { ...current, ...qaPatch } : current)
    setSessions((current) => current.map((candidate) =>
      candidate.id === sessionId ? { ...candidate, ...qaPatch } : candidate,
    ))
    if (!isCurrentSessionScope(scope)) return
    await refreshCompletionStatus()
  }, [captureSessionScope, datasetId, isCurrentSessionScope, refreshCompletionStatus, session])

  const persistTaskSelection = useCallback(
    (selectedSession: ReviewSession, task: ReviewTask) => {
      const scope = captureSessionScope(datasetId, selectedSession.id)
      const selection = {
        datasetId,
        taskId: task.id,
        sequence: taskSelectionSequenceRef.current + 1,
      }
      taskSelectionSequenceRef.current = selection.sequence
      latestTaskSelectionRef.current.set(selectedSession.id, selection)
      setSession((current) =>
        current?.id === selectedSession.id ? { ...current, last_task_id: task.id } : current,
      )
      // Serialize selection writes so a slow A request can never reach the
      // server after a newer B request and restore the wrong resume position.
      // Coalescing is deliberately avoided: every session retains its own
      // latest selection even when the operator switches sessions mid-write.
      taskSelectionWriteChainRef.current = taskSelectionWriteChainRef.current.then(async () => {
        try {
          await api.patchReviewSession(selectedSession.id, { last_task_id: task.id })
          const latest = latestTaskSelectionRef.current.get(selectedSession.id)
          if (
            !latest ||
            latest.datasetId !== selection.datasetId ||
            latest.taskId !== selection.taskId ||
            latest.sequence !== selection.sequence ||
            activeCurrentTaskIdRef.current !== task.id ||
            !isCurrentSessionScope(scope)
          ) return
          // A session response may have been captured before a concurrent QA
          // or status update. Merge only the field owned by this mutation.
          setSession((current) =>
            current?.id === selectedSession.id ? { ...current, last_task_id: task.id } : current,
          )
          setSessions((current) => current.map((candidate) =>
            candidate.id === selectedSession.id ? { ...candidate, last_task_id: task.id } : candidate,
          ))
        } catch (reason) {
          const latest = latestTaskSelectionRef.current.get(selectedSession.id)
          if (
            !latest ||
            latest.datasetId !== selection.datasetId ||
            latest.taskId !== selection.taskId ||
            latest.sequence !== selection.sequence ||
            !isCurrentSessionScope(scope)
          ) return
          notify?.({
            tone: 'error',
            title: '마지막 검수 위치를 저장하지 못했습니다',
            message: reason instanceof Error ? reason.message : undefined,
          })
        }
      })
    },
    [captureSessionScope, datasetId, isCurrentSessionScope, notify],
  )

  const activateTask = useCallback(
    (task: ReviewTask) => {
      if (!session || startWorkBusyRef.current || creatingSession || generatingCandidates) return
      setCurrentTaskId(task.id)
      setError(null)
      rememberSession(datasetId, session.id)
      persistTaskSelection(session, task)
      void navigateToTask(task)
      claimTask(task, session.status, session.id)
    },
    [claimTask, creatingSession, datasetId, generatingCandidates, navigateToTask, persistTaskSelection, session],
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
    const scope = captureSessionScope(datasetId, session.id)
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
      if (taskQueryGenerationRef.current !== queryGeneration || !isCurrentSessionScope(scope)) return []
      setTasks((current) => {
        const known = new Set(current.map((task) => task.id))
        return [...current, ...page.items.filter((task) => !known.has(task.id))]
      })
      setTaskNextCursor(page.next_cursor ?? null)
      setTaskTotal(page.total)
      if (page.status_counts) setStatusCounts(page.status_counts)
      return page.items
    } catch (reason) {
      if (taskQueryGenerationRef.current !== queryGeneration || !isCurrentSessionScope(scope)) return []
      const message = reason instanceof Error ? reason.message : '다음 검수 작업을 불러오지 못했습니다.'
      setError(message)
      return []
    } finally {
      taskPageLoadingRef.current = false
    }
  }, [captureSessionScope, datasetId, isCurrentSessionScope, session, taskNextCursor, taskStatusFilter, taskTypeFilter])

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
      if (
        !tasks.length ||
        updatingTaskId ||
        startWorkBusyRef.current ||
        creatingSession ||
        generatingCandidates
      ) return
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
    [activateTask, creatingSession, currentTaskIndex, generatingCandidates, loadMoreTasks, taskNextCursor, tasks, updatingTaskId],
  )

  const resolveCurrent = useCallback(
    async (
      resolution: Extract<ReviewTaskResolution, 'confirmed' | 'false_positive' | 'skipped' | 'field_survey'>,
    ) => {
      if (
        !currentTask ||
        !session ||
        session.status !== 'active' ||
        updatingTaskId ||
        startWorkBusyRef.current ||
        creatingSession ||
        generatingCandidates
      ) return
      const task = currentTask
      const scope = captureSessionScope(datasetId, session.id)
      const queryGeneration = taskQueryGenerationRef.current
      const isCurrentRequest = () => (
        isCurrentSessionScope(scope) && taskQueryGenerationRef.current === queryGeneration
      )
      setUpdatingTaskId(task.id)
      setError(null)
      try {
        const response = await api.resolveReviewTask(task.id, { resolution })
        if (!isCurrentRequest()) return
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
          claimTask(nextTask, session.status, session.id)
          void navigateToTask(nextTask)
        } else if (taskNextCursor !== null) {
          const loaded = await loadMoreTasks()
          if (!isCurrentRequest()) return
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
        if (!isCurrentRequest()) return
        const message = reason instanceof Error ? reason.message : '검수 상태를 저장하지 못했습니다.'
        setError(message)
        notify?.({ tone: 'error', title: '검수 상태를 저장하지 못했습니다', message })
      } finally {
        if (isCurrentRequest()) {
          setUpdatingTaskId((current) => (current === task.id ? '' : current))
        }
      }
    },
    [
      currentTask,
      activateTask,
      captureSessionScope,
      claimTask,
      creatingSession,
      datasetId,
      generatingCandidates,
      isCurrentSessionScope,
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
    const requestDatasetId = datasetId
    const requestId = startWorkRequestRef.current + 1
    startWorkRequestRef.current = requestId
    startWorkControllerRef.current?.abort()
    const controller = new AbortController()
    startWorkControllerRef.current = controller
    const isCurrentRequest = () => (
      !controller.signal.aborted &&
      startWorkRequestRef.current === requestId &&
      activeDatasetIdRef.current === requestDatasetId
    )
    setCreatingSession(true)
    setError(null)
    try {
      const response = await api.createReviewSession(requestDatasetId, {
        source_run_ids: sourceRunIds,
        target_layer_ids: [targetLayerId],
        track_ids: [activeFrame.track_id],
        frame_range: frameRange ?? [activeFrame.index, activeFrame.index],
        class_filters: ['TRAFFIC_SIGN', 'SIGN_SUPPORT_POLE'],
        status: 'active',
        created_by: 'operator-local',
      }, controller.signal)
      if (!isCurrentRequest()) return
      preferredSessionRef.current = { datasetId: requestDatasetId, sessionId: response.session.id }
      selectSessionScope(requestDatasetId, response.session.id)
      rememberSession(requestDatasetId, response.session.id)
      setReloadToken((value) => value + 1)
      notify?.({ tone: 'success', title: '현재 범위로 검수 작업을 만들었습니다.' })
    } catch (reason) {
      if (!isCurrentRequest()) return
      const message = reason instanceof Error ? reason.message : '검수 작업을 만들지 못했습니다.'
      setError(message)
      notify?.({ tone: 'error', title: '검수 작업을 만들지 못했습니다', message })
    } finally {
      if (startWorkRequestRef.current === requestId) setCreatingSession(false)
      if (startWorkControllerRef.current === controller) startWorkControllerRef.current = null
    }
  }, [activeFrame, creatingSession, datasetId, enabled, frameRange, notify, selectSessionScope])

  const startReviewWork = useCallback(async (
    targetLayerId: string,
    sourceRunIds: string[],
    sources: ReviewCandidateSources,
  ): Promise<boolean> => {
    if (
      !enabled ||
      !datasetId ||
      !activeFrame ||
      !targetLayerId ||
      sourceRunIds.length === 0 ||
      !Object.values(sources).some(Boolean) ||
      creatingSession ||
      generatingCandidates ||
      startWorkBusyRef.current
    ) return false
    const requestDatasetId = datasetId
    const requestId = startWorkRequestRef.current + 1
    startWorkRequestRef.current = requestId
    startWorkControllerRef.current?.abort()
    const controller = new AbortController()
    startWorkControllerRef.current = controller
    let createdScope: ReviewSessionScopeToken | null = null
    const isCurrentRequest = () => (
      !controller.signal.aborted &&
      startWorkRequestRef.current === requestId &&
      activeDatasetIdRef.current === requestDatasetId &&
      (createdScope === null || isCurrentSessionScope(createdScope))
    )
    startWorkBusyRef.current = true
    setCreatingSession(true)
    setGeneratingCandidates(true)
    setError(null)
    let createdSession: ReviewSession | null = null
    try {
      const created = await api.createReviewSession(requestDatasetId, {
        source_run_ids: sourceRunIds,
        target_layer_ids: [targetLayerId],
        track_ids: [activeFrame.track_id],
        frame_range: frameRange ?? [activeFrame.index, activeFrame.index],
        class_filters: ['TRAFFIC_SIGN', 'SIGN_SUPPORT_POLE'],
        status: 'active',
        created_by: 'operator-local',
      }, controller.signal)
      if (!isCurrentRequest()) return false
      createdSession = created.session
      preferredSessionRef.current = { datasetId: requestDatasetId, sessionId: created.session.id }
      selectSessionScope(requestDatasetId, created.session.id)
      createdScope = captureSessionScope(requestDatasetId, created.session.id)
      taskQueryGenerationRef.current += 1
      loadRequestRef.current += 1
      loadControllerRef.current?.abort()
      loadControllerRef.current = null
      navigationRequestRef.current += 1
      navigationControllerRef.current?.abort()
      navigationControllerRef.current = null
      completionRequestRef.current += 1
      completionControllerRef.current?.abort()
      completionControllerRef.current = null
      reopenRequestRef.current += 1
      reopenControllerRef.current?.abort()
      reopenControllerRef.current = null
      setCompletionStatus(null)
      setCheckingCompletion(false)
      setUpdatingTaskId('')
      setLoading(false)
      rememberSession(requestDatasetId, created.session.id)
      setSessions((current) => [
        created.session,
        ...current.filter((candidate) => candidate.id !== created.session.id),
      ])
      setSession(created.session)
      setTasks([])
      setCurrentTaskId('')
      setTaskNextCursor(null)
      setTaskTotal(0)
      setStatusCounts({})
      setQueueOpen(true)
      const generated = await api.generateReviewTasks(created.session.id, {
        sources,
        low_confidence_threshold: 0.5,
        unreviewed_interval_frames: 50,
      }, controller.signal)
      if (!isCurrentRequest()) return false
      setStartGuideOpen(false)
      setQueueOpen(true)
      setReloadToken((value) => value + 1)
      notify?.({
        tone: 'success',
        title: `검수 작업을 시작하고 후보 ${generated.created.toLocaleString('ko-KR')}개를 준비했습니다`,
        message: generated.existing
          ? `기존 후보 ${generated.existing.toLocaleString('ko-KR')}개는 유지했습니다.`
          : undefined,
      })
      return true
    } catch (reason) {
      if (!isCurrentRequest()) return false
      if (createdSession) {
        const recoveredSession = createdSession
        preferredSessionRef.current = { datasetId: requestDatasetId, sessionId: recoveredSession.id }
        selectSessionScope(requestDatasetId, recoveredSession.id)
        rememberSession(requestDatasetId, recoveredSession.id)
        setSessions((current) => [
          recoveredSession,
          ...current.filter((candidate) => candidate.id !== recoveredSession.id),
        ])
        setSession(recoveredSession)
        setTasks([])
        setCurrentTaskId('')
        setTaskNextCursor(null)
        setTaskTotal(0)
        setStatusCounts({})
        setQueueOpen(true)
        setStartGuideOpen(false)
        setCandidateGuideOpen(true)
      }
      const message = reason instanceof Error ? reason.message : '검수 작업을 시작하지 못했습니다.'
      setError(message)
      notify?.({
        tone: 'error',
        title: createdSession
          ? '검수 작업은 만들었지만 후보를 준비하지 못했습니다'
          : '검수 작업을 시작하지 못했습니다',
        message,
      })
      return false
    } finally {
      if (startWorkRequestRef.current === requestId) {
        startWorkBusyRef.current = false
        setCreatingSession(false)
        setGeneratingCandidates(false)
      }
      if (startWorkControllerRef.current === controller) startWorkControllerRef.current = null
    }
  }, [
    activeFrame,
    captureSessionScope,
    creatingSession,
    datasetId,
    enabled,
    frameRange,
    generatingCandidates,
    isCurrentSessionScope,
    notify,
    selectSessionScope,
  ])

  const generateCandidates = useCallback(async (sources: ReviewCandidateSources): Promise<boolean> => {
    if (
      !session ||
      session.status !== 'active' ||
      creatingSession ||
      generatingCandidates ||
      startWorkBusyRef.current
    ) return false
    const scope = captureSessionScope(datasetId, session.id)
    setGeneratingCandidates(true)
    setError(null)
    try {
      const response = await api.generateReviewTasks(session.id, {
        sources,
        low_confidence_threshold: 0.5,
        unreviewed_interval_frames: 50,
      })
      if (!isCurrentSessionScope(scope)) return false
      setReloadToken((value) => value + 1)
      notify?.({
        tone: 'success',
        title: `검수 후보 ${response.created.toLocaleString('ko-KR')}개를 추가했습니다`,
        message: response.existing ? `기존 후보 ${response.existing.toLocaleString('ko-KR')}개는 유지했습니다.` : undefined,
      })
      return true
    } catch (reason) {
      if (!isCurrentSessionScope(scope)) return false
      const message = reason instanceof Error ? reason.message : '검수 후보를 생성하지 못했습니다.'
      setError(message)
      notify?.({ tone: 'error', title: '검수 후보를 생성하지 못했습니다', message })
      return false
    } finally {
      if (isCurrentSessionScope(scope)) setGeneratingCandidates(false)
    }
  }, [captureSessionScope, creatingSession, datasetId, generatingCandidates, isCurrentSessionScope, notify, session])

  const flagCurrentFrame = useCallback(async (targetLayerId?: string) => {
    if (
      !session ||
      session.status !== 'active' ||
      !activeFrame ||
      creatingSession ||
      generatingCandidates ||
      startWorkBusyRef.current
    ) return
    const scope = captureSessionScope(datasetId, session.id)
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
      if (!isCurrentSessionScope(scope)) return
      setReloadToken((value) => value + 1)
      notify?.({ tone: 'success', title: '현재 프레임을 나중에 확인할 항목으로 추가했습니다.' })
    } catch (reason) {
      if (!isCurrentSessionScope(scope)) return
      const message = reason instanceof Error ? reason.message : '현재 프레임을 표시하지 못했습니다.'
      setError(message)
      notify?.({ tone: 'error', title: '나중에 확인 항목을 만들지 못했습니다', message })
    } finally {
      if (isCurrentSessionScope(scope)) setGeneratingCandidates(false)
    }
  }, [activeFrame, captureSessionScope, creatingSession, datasetId, generatingCandidates, isCurrentSessionScope, notify, session])

  const reopenCurrent = useCallback(async () => {
    if (
      !session ||
      session.status !== 'active' ||
      !currentTask ||
      !isReviewTaskComplete(currentTask) ||
      updatingTaskId ||
      creatingSession ||
      generatingCandidates ||
      startWorkBusyRef.current
    ) return
    const requestDatasetId = datasetId
    const sessionId = session.id
    const task = currentTask
    const scope = captureSessionScope(requestDatasetId, sessionId)
    const requestId = reopenRequestRef.current + 1
    reopenRequestRef.current = requestId
    reopenControllerRef.current?.abort()
    const controller = new AbortController()
    reopenControllerRef.current = controller
    const isCurrentRequest = () => (
      !controller.signal.aborted &&
      reopenRequestRef.current === requestId &&
      isCurrentSessionScope(scope) &&
      activeCurrentTaskIdRef.current === task.id
    )
    setUpdatingTaskId(task.id)
    setError(null)
    try {
      const reopened = await api.reopenReviewTask(task.id, controller.signal)
      if (!isCurrentRequest()) return
      let activeTask = reopened.task
      try {
        const claimed = await api.patchReviewTask(reopened.task.id, {
          status: 'in_progress',
          claimed_by: 'operator-local',
        }, controller.signal)
        if (!isCurrentRequest()) return
        activeTask = claimed.task
      } catch (reason) {
        if (!isCurrentRequest()) return
        const message = reason instanceof Error
          ? reason.message
          : '다시 연 항목을 즉시 시작하지 못했습니다.'
        setError(`${message} 항목을 다시 선택하면 검수를 시작할 수 있습니다.`)
      }
      if (!isCurrentRequest()) return
      setTasks((current) => current.map((task) => task.id === activeTask.id ? activeTask : task))
      setStatusCounts((current) => ({
        ...current,
        [task.status]: Math.max(0, (current[task.status] ?? 0) - 1),
        [activeTask.status]: (current[activeTask.status] ?? 0) + 1,
      }))
      if (activeTask.status === 'in_progress') {
        notify?.({ tone: 'success', title: '이 항목을 다시 검수할 수 있습니다.' })
      }
    } catch (reason) {
      if (!isCurrentRequest()) return
      const message = reason instanceof Error ? reason.message : '검수 항목을 다시 열지 못했습니다.'
      setError(message)
    } finally {
      if (reopenRequestRef.current === requestId) {
        setUpdatingTaskId((current) => current === task.id ? '' : current)
      }
      if (reopenControllerRef.current === controller) reopenControllerRef.current = null
    }
  }, [captureSessionScope, creatingSession, currentTask, datasetId, generatingCandidates, isCurrentSessionScope, notify, session, updatingTaskId])

  const completeSession = useCallback(async () => {
    if (
      !session ||
      updatingSession ||
      creatingSession ||
      generatingCandidates ||
      startWorkBusyRef.current
    ) return
    const scope = captureSessionScope(datasetId, session.id)
    setUpdatingSession(true)
    setError(null)
    try {
      const completion = await refreshCompletionStatus()
      if (!isCurrentSessionScope(scope)) return
      if (!completion) return
      if (!completion.can_complete) {
        const message = reviewCompletionBlockerMessages(completion.blockers).join(' · ')
        setError(message)
        notify?.({
          tone: 'info',
          title: '검수 작업을 완료하려면 남은 조건을 처리해 주세요',
          message,
        })
        return
      }
      const response = await api.patchReviewSession(session.id, { status: 'completed' })
      if (!isCurrentSessionScope(scope)) return
      setSession(response.session)
      setSessions((current) => current.map((candidate) => candidate.id === response.session.id ? response.session : candidate))
      notify?.({ tone: 'success', title: '검수 작업을 완료했습니다.' })
    } catch (reason) {
      if (!isCurrentSessionScope(scope)) return
      const message = reason instanceof Error ? reason.message : '검수 작업을 완료하지 못했습니다.'
      setError(message)
      notify?.({ tone: 'error', title: '검수 작업 완료 조건을 확인해 주세요', message })
    } finally {
      if (isCurrentSessionScope(scope)) setUpdatingSession(false)
    }
  }, [captureSessionScope, creatingSession, datasetId, generatingCandidates, isCurrentSessionScope, notify, refreshCompletionStatus, session, updatingSession])

  const setSessionStatus = useCallback(async (status: 'active' | 'paused') => {
    if (
      !session ||
      updatingSession ||
      session.status === status ||
      creatingSession ||
      generatingCandidates ||
      startWorkBusyRef.current
    ) return
    const scope = captureSessionScope(datasetId, session.id)
    setUpdatingSession(true)
    setError(null)
    try {
      const response = await api.patchReviewSession(session.id, { status })
      if (!isCurrentSessionScope(scope)) return
      setSession(response.session)
      setSessions((current) => current.map((candidate) =>
        candidate.id === response.session.id ? response.session : candidate,
      ))
      notify?.({
        tone: 'success',
        title: status === 'paused' ? '검수 작업을 일시 정지했습니다.' : '검수 작업을 재개했습니다.',
      })
    } catch (reason) {
      if (!isCurrentSessionScope(scope)) return
      const message = reason instanceof Error ? reason.message : '검수 작업 상태를 바꾸지 못했습니다.'
      setError(message)
      notify?.({ tone: 'error', title: '검수 작업 상태를 바꾸지 못했습니다', message })
    } finally {
      if (isCurrentSessionScope(scope)) setUpdatingSession(false)
    }
  }, [captureSessionScope, creatingSession, datasetId, generatingCandidates, isCurrentSessionScope, notify, session, updatingSession])

  const shortcutStateRef = useRef({ enabled, moveTask, resolveCurrent, flagCurrentFrame, currentTask })
  shortcutStateRef.current = { enabled, moveTask, resolveCurrent, flagCurrentFrame, currentTask }
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (
        hasOpenModalDialog() ||
        event.defaultPrevented ||
        event.repeat ||
        event.altKey ||
        event.ctrlKey ||
        event.metaKey ||
        event.shiftKey ||
        isWorkspaceShortcutBlockedTarget(event.target) ||
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
      completionStatus,
      checkingCompletion,
      startGuideOpen,
      setStartGuideOpen,
      candidateGuideOpen,
      setCandidateGuideOpen,
      sourceRuns,
      setQueueOpen,
      reload: () => {
        taskQueryGenerationRef.current += 1
        setReloadToken((value) => value + 1)
      },
      selectSession: (sessionId: string) => {
        taskQueryGenerationRef.current += 1
        startWorkRequestRef.current += 1
        startWorkControllerRef.current?.abort()
        startWorkControllerRef.current = null
        startWorkBusyRef.current = false
        completionRequestRef.current += 1
        completionControllerRef.current?.abort()
        completionControllerRef.current = null
        reopenRequestRef.current += 1
        reopenControllerRef.current?.abort()
        reopenControllerRef.current = null
        navigationRequestRef.current += 1
        navigationControllerRef.current?.abort()
        navigationControllerRef.current = null
        taskPageLoadingRef.current = false
        selectSessionScope(datasetId, sessionId, true)
        setCompletionStatus(null)
        setCheckingCompletion(false)
        setUpdatingTaskId('')
        setUpdatingSession(false)
        setCreatingSession(false)
        setGeneratingCandidates(false)
        preferredSessionRef.current = { datasetId, sessionId }
        rememberSession(datasetId, sessionId)
        setReloadToken((value) => value + 1)
      },
      selectTask,
      navigateFrame,
      moveTask,
      createDefaultSession,
      startReviewWork,
      generateCandidates,
      flagCurrentFrame,
      reopenCurrent,
      refreshCompletionStatus,
      recordQaRun,
      completeSession,
      setSessionStatus,
      resolveCurrent,
    }),
    [
      completedCount,
      completionStatus,
      completeSession,
      createDefaultSession,
      creatingSession,
      checkingCompletion,
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
      candidateGuideOpen,
      recordQaRun,
      refreshCompletionStatus,
      reopenCurrent,
      resolveCurrent,
      selectTask,
      setTaskStatusFilter,
      setTaskTypeFilter,
      session,
      sessions,
      startGuideOpen,
      startReviewWork,
      tasks,
      taskNextCursor,
      taskStatusFilter,
      taskTypeFilter,
      totalCount,
      setSessionStatus,
      selectSessionScope,
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
