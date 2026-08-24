import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../lib/api'
import type {
  Frame,
  QaRunResponse,
  ReviewSession,
  ReviewTask,
  ReviewTaskResolution,
  ReviewTaskStatus,
} from '../types'
import {
  ReviewProvider,
  reviewQueueFilterStorageKey,
  reviewSessionStorageKey,
  useReviewWorkspace,
} from './ReviewContext'
import { ReviewQueue } from './ReviewQueue'
import { ReviewSessionBar, reviewSessionScopeSummary } from './ReviewSessionBar'
import { reviewTaskEvidenceSummary, reviewTaskFrameSummary } from './ReviewTaskCard'

const SESSION: ReviewSession = {
  id: 'rvw_1',
  dataset_id: 'dataset-1',
  source_run_ids: ['run-1'],
  target_layer_ids: ['layer-1'],
  track_ids: ['Track01'],
  frame_range: [0, 100],
  class_filters: ['TRAFFIC_SIGN'],
  status: 'active',
  created_by: 'operator-local',
  created_at: '2026-08-24T00:00:00Z',
  updated_at: '2026-08-24T00:00:00Z',
  last_task_id: 'rvt_2',
  qa_layer_revisions: null,
  qa_ran_at: null,
}

const PAUSED_SESSION: ReviewSession = {
  ...SESSION,
  id: 'rvw_2',
  status: 'paused',
}

function task(id: string, status: ReviewTaskStatus, frameId: string): ReviewTask {
  return {
    id,
    session_id: PAUSED_SESSION.id,
    dataset_id: 'dataset-1',
    task_type: 'MANUAL_SCAN',
    status,
    priority: 70,
    frame_id: frameId,
    track_id: 'Track01',
    frame_start: null,
    frame_end: null,
    source_run_id: null,
    source_detection_id: null,
    target_layer_id: 'layer-1',
    class_hint: 'TRAFFIC_SIGN',
    reason_codes: [],
    location_hint: null,
    claimed_by: null,
    resolved_feature_ids: [],
    resolution:
      status === 'todo' || status === 'in_progress'
        ? null
        : (status as ReviewTaskResolution),
    created_at: '2026-08-24T00:00:00Z',
    updated_at: '2026-08-24T00:00:00Z',
  }
}

const TASKS = [
  task('rvt_1', 'todo', 'frame-1'),
  task('rvt_2', 'in_progress', 'frame-2'),
  task('rvt_3', 'confirmed', 'frame-3'),
]

const DEFAULT_SOURCES = {
  low_confidence: true,
  projection_failed: true,
  geometry_review: true,
  pole_base_review: true,
  unreviewed_interval: true,
  spacing_anomaly: false,
}

const QA_RUN_RESULT: QaRunResponse = {
  items: [],
  total: 0,
  counts: { info: 0, warning: 0, error: 0 },
  ran_at: '2026-08-24T00:01:00Z',
  layer_revisions: { 'layer-1': 3 },
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

function ReviewStartProbe() {
  const review = useReviewWorkspace()
  return (
    <div>
      <button type="button" onClick={() => void review.startReviewWork('layer-1', ['run-1'], DEFAULT_SOURCES)}>
        probe start
      </button>
      <button type="button" onClick={() => void review.generateCandidates(DEFAULT_SOURCES)}>
        probe retry candidates
      </button>
      <output aria-label="probe session">{review.session?.id ?? ''}</output>
      <output aria-label="probe error">{review.error ?? ''}</output>
      <output aria-label="probe candidate guide">{String(review.candidateGuideOpen)}</output>
      <output aria-label="probe creating">{String(review.creatingSession)}</output>
      <output aria-label="probe generating">{String(review.generatingCandidates)}</output>
    </div>
  )
}

function ReviewAsyncProbe({ switchToSessionId }: { switchToSessionId: string }) {
  const review = useReviewWorkspace()
  return (
    <div>
      <button type="button" onClick={() => void review.reopenCurrent()}>probe reopen</button>
      <button type="button" onClick={() => review.selectSession(switchToSessionId)}>probe switch session</button>
      <button
        type="button"
        onClick={() => void review.recordQaRun(QA_RUN_RESULT, review.session?.id)}
      >
        probe record qa
      </button>
      <button type="button" onClick={() => void review.resolveCurrent('confirmed')}>probe resolve</button>
      <button
        type="button"
        onClick={() => review.setTaskTypeFilter('POLE_BASE_REVIEW')}
      >
        probe pole filter
      </button>
      <output aria-label="probe current session">{review.session?.id ?? ''}</output>
      <output aria-label="probe current task">{review.currentTask?.id ?? ''}</output>
      <output aria-label="probe current status">{review.currentTask?.status ?? ''}</output>
      <output aria-label="probe current qa time">{review.session?.qa_ran_at ?? ''}</output>
      <output aria-label="probe completion">{String(review.completionStatus?.can_complete ?? false)}</output>
      <output aria-label="probe checking completion">{String(review.checkingCompletion)}</output>
      <output aria-label="probe updating task">{review.updatingTaskId}</output>
      <output aria-label="probe async error">{review.error ?? ''}</output>
    </div>
  )
}

function frame(id: string): Frame {
  return {
    id,
    index: Number(id.slice(-1)),
    track_id: 'Track01',
    timestamp: '2026-08-24T00:00:00Z',
    coordinate: null,
    has_panorama: true,
    has_points: true,
  }
}

function mockReviewApi(options: { sessions?: ReviewSession[]; tasks?: ReviewTask[] } = {}) {
  const sessions = options.sessions ?? [SESSION, PAUSED_SESSION]
  const tasks = options.tasks ?? TASKS
  vi.spyOn(api, 'reviewSessions').mockResolvedValue({
    items: sessions,
    total: sessions.length,
    offset: 0,
    limit: 100,
    next_offset: null,
  })
  vi.spyOn(api, 'reviewSession').mockImplementation(async (sessionId) => ({
    session: sessions.find((candidate) => candidate.id === sessionId) ?? sessions[0],
  }))
  vi.spyOn(api, 'reviewTasks').mockResolvedValue({
    items: tasks,
    total: tasks.length,
    offset: 0,
    limit: 200,
    next_offset: null,
  })
  vi.spyOn(api, 'reviewTaskFrame').mockImplementation(async (_datasetId, frameId) => ({
    frame: frame(frameId),
    page_offset: Number(frameId.slice(-1)) * 10,
  }))
  vi.spyOn(api, 'patchReviewSession').mockImplementation(async (sessionId, patch) => ({
    session: {
      ...(sessions.find((candidate) => candidate.id === sessionId) ?? sessions[0]),
      ...patch,
    },
  }))
  vi.spyOn(api, 'patchReviewTask').mockImplementation(async (taskId, patch) => ({
    task: { ...tasks.find((candidate) => candidate.id === taskId)!, ...patch },
  }))
  vi.spyOn(api, 'reviewCompletionStatus').mockResolvedValue({
    session_status: 'active',
    requirements_met: false,
    can_complete: false,
    blockers: {
      open_tasks: tasks.filter((item) => ['todo', 'in_progress'].includes(item.status)).length,
      open_error_qa_issues: 0,
      qa_not_run: 1,
      stale_qa_target_layers: 0,
      pending_task_resolutions: 0,
      task_resolution_errors: 0,
      task_resolution_scan_truncated: 0,
    },
    checked_at: '2026-08-24T00:00:00Z',
  })
  return { sessions, tasks }
}

function renderReview(
  onNavigateFrame = vi.fn(),
  enabled = true,
  activeLearningExportEnabled = false,
) {
  return {
    onNavigateFrame,
    ...render(
      <ReviewProvider
        enabled={enabled}
        datasetId="dataset-1"
        activeFrame={frame('frame-1')}
        frameRange={[0, 100]}
        sourceRuns={[{ id: 'run-1', label: '완료 run 1' }]}
        onNavigateFrame={onNavigateFrame}
      >
        <ReviewSessionBar activeLearningExportEnabled={activeLearningExportEnabled} />
        <ReviewQueue />
        <input aria-label="속성 입력" />
      </ReviewProvider>,
    ),
  }
}

afterEach(() => {
  cleanup()
  window.localStorage.clear()
  vi.restoreAllMocks()
})

describe('Review Workspace queue', () => {
  it('shows the active-learning export only when its independent capability is enabled', async () => {
    mockReviewApi()
    window.localStorage.setItem(reviewSessionStorageKey('dataset-1'), SESSION.id)
    renderReview(vi.fn(), true, true)

    await waitFor(() =>
      expect(screen.getByRole('link', { name: 'Active-learning ZIP' })).toHaveAttribute(
        'href',
        api.reviewActiveLearningExportUrl(SESSION.id),
      ),
    )
  })

  it('hides the active-learning export when its independent capability is disabled', async () => {
    mockReviewApi()
    window.localStorage.setItem(reviewSessionStorageKey('dataset-1'), SESSION.id)
    renderReview()

    await waitFor(() =>
      expect(screen.getByRole('option', { name: /완료 run 1.*진행 중/ })).toBeInTheDocument(),
    )
    expect(screen.queryByRole('link', { name: 'Active-learning ZIP' })).not.toBeInTheDocument()
  })

  it('exposes start and candidate controls as named nonmodal popover regions', async () => {
    mockReviewApi({ sessions: [SESSION], tasks: [] })
    window.localStorage.setItem(reviewSessionStorageKey('dataset-1'), SESSION.id)
    renderReview()

    const candidateToggle = await screen.findByRole(
      'button',
      { name: '후보 추가' },
      { timeout: 5_000 },
    )
    const startToggle = screen.getByRole('button', { name: '새 검수 작업' })
    expect(startToggle).toHaveAttribute('aria-controls', 'review-start-popover')
    expect(startToggle).toHaveAttribute('aria-expanded', 'false')
    expect(document.getElementById('review-start-popover')).toHaveAttribute('hidden')

    fireEvent.click(startToggle)

    const startRegion = screen.getByRole('region', { name: '새 검수 작업 시작' })
    expect(startRegion).toHaveAttribute('id', 'review-start-popover')
    expect(startToggle).toHaveAttribute('aria-expanded', 'true')
    expect(screen.queryByRole('dialog', { name: '새 검수 작업 시작' })).not.toBeInTheDocument()
    fireEvent.click(within(startRegion).getByRole('button', { name: '취소' }))
    expect(startToggle).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByRole('region', { name: '새 검수 작업 시작' })).not.toBeInTheDocument()
    expect(document.getElementById('review-start-popover')).toHaveAttribute('hidden')

    expect(candidateToggle).toHaveAttribute('aria-controls', 'review-candidate-popover')
    expect(candidateToggle).toHaveAttribute('aria-expanded', 'false')
    expect(document.getElementById('review-candidate-popover')).toHaveAttribute('hidden')

    fireEvent.click(candidateToggle)

    const candidateRegion = screen.getByRole('region', { name: '검수 후보 추가' })
    expect(candidateRegion).toHaveAttribute('id', 'review-candidate-popover')
    expect(candidateToggle).toHaveAttribute('aria-expanded', 'true')
    expect(screen.queryByRole('dialog', { name: '검수 후보 추가' })).not.toBeInTheDocument()
    fireEvent.click(within(candidateRegion).getByRole('button', { name: '취소' }))
    expect(candidateToggle).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByRole('region', { name: '검수 후보 추가' })).not.toBeInTheDocument()
    expect(document.getElementById('review-candidate-popover')).toHaveAttribute('hidden')
  })

  it('does not render or call review APIs when the capability is disabled', async () => {
    const reviewSessions = vi.spyOn(api, 'reviewSessions')
    renderReview(vi.fn(), false)

    expect(screen.queryByRole('region', { name: '검수 작업' })).not.toBeInTheDocument()
    expect(screen.queryByRole('complementary', { name: '검수 항목 목록' })).not.toBeInTheDocument()
    await Promise.resolve()
    expect(reviewSessions).not.toHaveBeenCalled()
  })

  it('starts one active work package and generates selected candidates in sequence', async () => {
    const sessions: ReviewSession[] = []
    mockReviewApi({ sessions, tasks: [] })
    const createdSession = { ...SESSION, id: 'rvw_started', last_task_id: null }
    const create = vi.spyOn(api, 'createReviewSession').mockImplementation(async () => {
      sessions.push(createdSession)
      return { session: createdSession }
    })
    const generate = vi.spyOn(api, 'generateReviewTasks').mockResolvedValue({
      created: 4,
      existing: 0,
      items: [],
      source_counts: {},
    })
    render(
      <ReviewProvider
        enabled
        datasetId="dataset-1"
        activeFrame={frame('frame-1')}
        frameRange={[0, 100]}
        sourceRuns={[{ id: 'run-1', label: '완료 run 1' }]}
        onNavigateFrame={vi.fn()}
      >
        <ReviewStartProbe />
      </ReviewProvider>,
    )

    fireEvent.click(screen.getByRole('button', { name: 'probe start' }))

    await waitFor(() => expect(generate).toHaveBeenCalledWith(createdSession.id, {
      sources: DEFAULT_SOURCES,
      low_confidence_threshold: 0.5,
      unreviewed_interval_frames: 50,
    }, expect.any(AbortSignal)))
    expect(create.mock.invocationCallOrder[0]).toBeLessThan(generate.mock.invocationCallOrder[0])
    await waitFor(() => expect(screen.getByLabelText('probe session')).toHaveTextContent(createdSession.id))
    expect(window.localStorage.getItem(reviewSessionStorageKey('dataset-1'))).toBe(createdSession.id)
  })

  it('blocks old-session mutations throughout deferred work-package creation and candidate generation', async () => {
    const oldTask = {
      ...task('rvt_old_start', 'in_progress', 'frame-1'),
      session_id: SESSION.id,
    }
    const oldSession = { ...SESSION, last_task_id: oldTask.id }
    const createdSession = {
      ...SESSION,
      id: 'rvw_start_transition',
      last_task_id: null,
    }
    mockReviewApi({ sessions: [oldSession], tasks: [oldTask] })
    window.localStorage.setItem(reviewSessionStorageKey('dataset-1'), oldSession.id)
    const pendingCreate = deferred<{ session: ReviewSession }>()
    const pendingGeneration = deferred<Awaited<ReturnType<typeof api.generateReviewTasks>>>()
    const create = vi.spyOn(api, 'createReviewSession').mockReturnValue(pendingCreate.promise)
    const generate = vi.spyOn(api, 'generateReviewTasks').mockReturnValue(pendingGeneration.promise)
    const resolveTask = vi.spyOn(api, 'resolveReviewTask')
    const patchSession = vi.spyOn(api, 'patchReviewSession')
    render(
      <ReviewProvider
        enabled
        datasetId="dataset-1"
        activeFrame={frame('frame-1')}
        frameRange={[0, 100]}
        sourceRuns={[{ id: 'run-1', label: '완료 run 1' }]}
        onNavigateFrame={vi.fn()}
      >
        <ReviewStartProbe />
        <ReviewSessionBar />
        <ReviewQueue />
      </ReviewProvider>,
    )

    await waitFor(() =>
      expect(screen.getByRole('button', { name: /frame-1/ })).toHaveAttribute('aria-current', 'true'),
    )
    patchSession.mockClear()
    fireEvent.click(screen.getByRole('button', { name: 'probe start' }))
    await waitFor(() => expect(create).toHaveBeenCalledOnce())

    expect(screen.getByRole('button', { name: '완료' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '일시 정지' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '후보 추가' })).toBeDisabled()
    fireEvent.keyDown(window, { key: 'x', code: 'KeyX' })
    fireEvent.keyDown(window, { key: 'f', code: 'KeyF' })
    expect(resolveTask).not.toHaveBeenCalled()
    expect(generate).not.toHaveBeenCalled()
    expect(patchSession).not.toHaveBeenCalledWith(oldSession.id, { status: 'paused' })

    await act(async () => pendingCreate.resolve({ session: createdSession }))
    await waitFor(() => expect(generate).toHaveBeenCalledOnce())
    expect(screen.getByLabelText('probe session')).toHaveTextContent(createdSession.id)
    expect(screen.queryByRole('button', { name: /frame-1/ })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '일시 정지' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '검수 후보 만들기' })).toBeDisabled()
    fireEvent.keyDown(window, { key: 'x', code: 'KeyX' })
    fireEvent.keyDown(window, { key: 'f', code: 'KeyF' })
    expect(resolveTask).not.toHaveBeenCalled()
    expect(generate).toHaveBeenCalledOnce()
    expect(patchSession).not.toHaveBeenCalledWith(oldSession.id, { status: 'paused' })

    await act(async () => pendingGeneration.resolve({
      created: 2,
      existing: 0,
      items: [],
      source_counts: {},
    }))
    await waitFor(() => expect(screen.getByLabelText('probe creating')).toHaveTextContent('false'))
    expect(screen.getByLabelText('probe generating')).toHaveTextContent('false')
  })

  it('keeps a created work package selected when initial candidate generation fails', async () => {
    const sessions: ReviewSession[] = []
    mockReviewApi({ sessions, tasks: [] })
    const createdSession = { ...SESSION, id: 'rvw_candidate_retry', last_task_id: null }
    const create = vi.spyOn(api, 'createReviewSession').mockResolvedValue({ session: createdSession })
    const generate = vi.spyOn(api, 'generateReviewTasks')
      .mockRejectedValueOnce(new Error('candidate source unavailable'))
      .mockResolvedValueOnce({ created: 3, existing: 0, items: [], source_counts: {} })
    render(
      <ReviewProvider
        enabled
        datasetId="dataset-1"
        activeFrame={frame('frame-1')}
        frameRange={[0, 100]}
        sourceRuns={[{ id: 'run-1', label: '완료 run 1' }]}
        onNavigateFrame={vi.fn()}
      >
        <ReviewStartProbe />
      </ReviewProvider>,
    )

    fireEvent.click(screen.getByRole('button', { name: 'probe start' }))

    await waitFor(() => expect(screen.getByLabelText('probe session')).toHaveTextContent(createdSession.id))
    expect(screen.getByLabelText('probe error')).toHaveTextContent('candidate source unavailable')
    expect(screen.getByLabelText('probe candidate guide')).toHaveTextContent('true')
    expect(window.localStorage.getItem(reviewSessionStorageKey('dataset-1'))).toBe(createdSession.id)

    fireEvent.click(screen.getByRole('button', { name: 'probe retry candidates' }))
    await waitFor(() => expect(generate).toHaveBeenCalledTimes(2))
    expect(create).toHaveBeenCalledTimes(1)
  })

  it('ignores a delayed work-package creation after the dataset changes', async () => {
    mockReviewApi({ sessions: [], tasks: [] })
    const pendingCreate = deferred<{ session: ReviewSession }>()
    const createdSession = { ...SESSION, id: 'rvw_stale_dataset', last_task_id: null }
    const create = vi.spyOn(api, 'createReviewSession').mockReturnValue(pendingCreate.promise)
    const generate = vi.spyOn(api, 'generateReviewTasks')
    const view = render(
      <ReviewProvider
        enabled
        datasetId="dataset-1"
        activeFrame={frame('frame-1')}
        frameRange={[0, 100]}
        onNavigateFrame={vi.fn()}
      >
        <ReviewStartProbe />
      </ReviewProvider>,
    )

    fireEvent.click(screen.getByRole('button', { name: 'probe start' }))
    await waitFor(() => expect(create).toHaveBeenCalledTimes(1))
    const signal = create.mock.calls[0][2]

    view.rerender(
      <ReviewProvider
        enabled
        datasetId="dataset-2"
        activeFrame={frame('frame-1')}
        frameRange={[0, 100]}
        onNavigateFrame={vi.fn()}
      >
        <ReviewStartProbe />
      </ReviewProvider>,
    )
    await waitFor(() => expect(signal?.aborted).toBe(true))
    await act(async () => pendingCreate.resolve({ session: createdSession }))

    expect(generate).not.toHaveBeenCalled()
    expect(screen.getByLabelText('probe session')).toBeEmptyDOMElement()
    expect(screen.getByLabelText('probe error')).toBeEmptyDOMElement()
    expect(screen.getByLabelText('probe creating')).toHaveTextContent('false')
    expect(screen.getByLabelText('probe generating')).toHaveTextContent('false')
    expect(window.localStorage.getItem(reviewSessionStorageKey('dataset-1'))).toBeNull()
  })

  it('restores a paused session without claiming todo tasks and still permits read-only navigation', async () => {
    mockReviewApi()
    window.localStorage.setItem(reviewSessionStorageKey('dataset-1'), PAUSED_SESSION.id)
    const { onNavigateFrame } = renderReview()

    await waitFor(() =>
      expect(screen.getByRole('combobox', { name: '검수 작업 선택' })).toHaveValue(
        PAUSED_SESSION.id,
      ),
    )
    await waitFor(() => expect(onNavigateFrame).toHaveBeenCalledWith(frame('frame-2'), 20))
    expect(screen.getByRole('button', { name: /수동 확인 검수 중/ })).toHaveAttribute(
      'aria-current',
      'true',
    )
    expect(screen.getByLabelText('검수 진행률 33%')).toBeInTheDocument()
    expect(screen.getByText('미처리 검수 항목 2개')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '검수 작업 완료' })).toHaveAttribute(
      'title',
      '미처리 검수 항목 2개',
    )
    expect(api.patchReviewTask).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: /수동 확인 대기/ }))

    await waitFor(() => expect(onNavigateFrame).toHaveBeenLastCalledWith(frame('frame-1'), 10))
    expect(api.patchReviewSession).toHaveBeenCalledWith(PAUSED_SESSION.id, {
      last_task_id: 'rvt_1',
    })
    expect(api.patchReviewTask).not.toHaveBeenCalled()
  })

  it('does not auto-claim the first todo task in a draft session', async () => {
    const draftSession = {
      ...SESSION,
      id: 'rvw_draft',
      status: 'draft' as const,
      last_task_id: 'rvt_draft',
    }
    const draftTask = task('rvt_draft', 'todo', 'frame-1')
    mockReviewApi({ sessions: [draftSession], tasks: [draftTask] })
    window.localStorage.setItem(reviewSessionStorageKey('dataset-1'), draftSession.id)
    renderReview()

    await waitFor(() =>
      expect(screen.getByRole('combobox', { name: '검수 작업 선택' })).toHaveValue(
        draftSession.id,
      ),
    )
    expect(api.patchReviewTask).not.toHaveBeenCalled()
    expect(screen.queryByRole('button', { name: /후보 추가/ })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '완료' })).toBeDisabled()
  })

  it('uses J/K for next and previous tasks while ignoring shortcuts from text inputs', async () => {
    mockReviewApi()
    window.localStorage.setItem(reviewSessionStorageKey('dataset-1'), PAUSED_SESSION.id)
    const { onNavigateFrame } = renderReview()
    await waitFor(() => expect(onNavigateFrame).toHaveBeenCalledWith(frame('frame-2'), 20))
    onNavigateFrame.mockClear()

    fireEvent.keyDown(screen.getByRole('textbox', { name: '속성 입력' }), {
      key: 'j',
      code: 'KeyJ',
    })
    expect(onNavigateFrame).not.toHaveBeenCalled()

    fireEvent.keyDown(window, { key: 'j', code: 'KeyJ' })
    await waitFor(() => expect(onNavigateFrame).toHaveBeenLastCalledWith(frame('frame-3'), 30))

    fireEvent.keyDown(window, { key: 'k', code: 'KeyK' })
    await waitFor(() => expect(onNavigateFrame).toHaveBeenLastCalledWith(frame('frame-2'), 20))
  })

  it('serializes rapid task selections and never lets stale session data replace the latest resume position', async () => {
    mockReviewApi({ sessions: [PAUSED_SESSION], tasks: TASKS })
    window.localStorage.setItem(reviewSessionStorageKey('dataset-1'), PAUSED_SESSION.id)
    const firstWrite = deferred<{ session: ReviewSession }>()
    const secondWrite = deferred<{ session: ReviewSession }>()
    const patchSession = vi.spyOn(api, 'patchReviewSession').mockImplementation(
      async (sessionId, patch) => {
        if (patch.last_task_id === 'rvt_1') return firstWrite.promise
        if (patch.last_task_id === 'rvt_3') return secondWrite.promise
        return {
          session: {
            ...PAUSED_SESSION,
            id: sessionId,
            ...patch,
          },
        }
      },
    )
    renderReview()

    await waitFor(() =>
      expect(screen.getByRole('button', { name: /frame-2/ })).toHaveAttribute('aria-current', 'true'),
    )
    fireEvent.click(screen.getByRole('button', { name: /frame-1/ }))
    await waitFor(() => expect(patchSession).toHaveBeenCalledWith(PAUSED_SESSION.id, {
      last_task_id: 'rvt_1',
    }))

    fireEvent.click(screen.getByRole('button', { name: /frame-3/ }))
    expect(screen.getByRole('button', { name: /frame-3/ })).toHaveAttribute('aria-current', 'true')
    // The B write waits for A, guaranteeing server arrival order as well as
    // client response order.
    expect(patchSession).toHaveBeenCalledTimes(1)

    firstWrite.resolve({
      // Simulate an A response captured before a concurrent status update. A
      // whole-object merge would incorrectly replace the paused UI state.
      session: { ...PAUSED_SESSION, status: 'completed', last_task_id: 'rvt_1' },
    })
    await waitFor(() => expect(patchSession).toHaveBeenCalledTimes(2))
    expect(patchSession).toHaveBeenLastCalledWith(PAUSED_SESSION.id, {
      last_task_id: 'rvt_3',
    })
    expect(screen.getByRole('button', { name: '재개' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /frame-3/ })).toHaveAttribute('aria-current', 'true')

    secondWrite.resolve({ session: { ...PAUSED_SESSION, last_task_id: 'rvt_3' } })
    await act(async () => {
      await secondWrite.promise
    })
    expect(patchSession).toHaveBeenCalledTimes(2)
    expect(screen.getByRole('button', { name: '재개' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /frame-3/ })).toHaveAttribute('aria-current', 'true')
  })

  it('does not move review tasks with J while the help modal is open', async () => {
    mockReviewApi()
    window.localStorage.setItem(reviewSessionStorageKey('dataset-1'), PAUSED_SESSION.id)
    const { onNavigateFrame } = renderReview()
    await waitFor(() => expect(onNavigateFrame).toHaveBeenCalled())
    onNavigateFrame.mockClear()
    render(<section role="dialog" aria-modal="true" aria-label="도움말" />)

    fireEvent.keyDown(window, { key: 'j', code: 'KeyJ' })

    expect(onNavigateFrame).not.toHaveBeenCalled()
  })

  it.each([
    ['완료', 'confirmed'],
    ['건너뛰기', 'skipped'],
    ['현장조사', 'field_survey'],
  ] as const)('resolves the current task through %s and updates progress', async (buttonName, resolution) => {
    const activeTask = task('rvt_action', 'in_progress', 'frame-4')
    const activeSession = { ...SESSION, last_task_id: activeTask.id }
    mockReviewApi({ sessions: [activeSession], tasks: [activeTask] })
    const resolveReviewTask = vi.spyOn(api, 'resolveReviewTask').mockResolvedValue({
      task: {
        ...activeTask,
        status: resolution,
        resolution,
      },
    })
    renderReview()
    await waitFor(() => expect(screen.getByRole('button', { name: buttonName })).toBeEnabled())

    fireEvent.click(screen.getByRole('button', { name: buttonName }))

    await waitFor(() =>
      expect(resolveReviewTask).toHaveBeenCalledWith(activeTask.id, { resolution }),
    )
    await waitFor(() => expect(screen.getByLabelText('검수 진행률 100%')).toBeInTheDocument())
    if (resolution === 'skipped') {
      expect(screen.queryByRole('button', { name: buttonName })).not.toBeInTheDocument()
      expect(screen.getByRole('button', { name: '다시 확인' })).toBeEnabled()
    } else {
      expect(screen.getByRole('button', { name: buttonName })).toBeDisabled()
    }
  })

  it('reopens a completed item and immediately claims it so actions are usable', async () => {
    const completedTask = task('rvt_reopen', 'confirmed', 'frame-1')
    const activeSession = { ...SESSION, last_task_id: completedTask.id }
    mockReviewApi({ sessions: [activeSession], tasks: [completedTask] })
    const reopen = vi.spyOn(api, 'reopenReviewTask').mockResolvedValue({
      task: { ...completedTask, status: 'todo', resolution: null },
    })
    renderReview()

    fireEvent.click(await screen.findByRole('button', { name: '다시 확인' }))

    await waitFor(() => expect(reopen).toHaveBeenCalledWith(completedTask.id, expect.any(AbortSignal)))
    await waitFor(() => expect(api.patchReviewTask).toHaveBeenCalledWith(completedTask.id, {
      status: 'in_progress',
      claimed_by: 'operator-local',
    }, expect.any(AbortSignal)))
    await waitFor(() => expect(screen.getByRole('button', { name: '완료' })).toBeEnabled())
    expect(screen.getByLabelText('검수 진행률 0%')).toBeInTheDocument()
  })

  it('does not apply a delayed reopen response after another session is selected', async () => {
    const sessionA = { ...SESSION, id: 'rvw_reopen_a', last_task_id: 'task-reopen-a' }
    const sessionB = { ...SESSION, id: 'rvw_reopen_b', last_task_id: 'task-reopen-b' }
    const taskA = { ...task('task-reopen-a', 'confirmed', 'frame-1'), session_id: sessionA.id }
    const taskB = { ...task('task-reopen-b', 'confirmed', 'frame-2'), session_id: sessionB.id }
    mockReviewApi({ sessions: [sessionA, sessionB], tasks: [] })
    vi.mocked(api.reviewTasks).mockImplementation(async (sessionId) => {
      const items = sessionId === sessionA.id ? [taskA] : [taskB]
      return {
        items,
        total: 1,
        offset: 0,
        limit: 200,
        next_offset: null,
        status_counts: { confirmed: 1 },
      }
    })
    const pendingReopen = deferred<{ task: ReviewTask }>()
    const reopen = vi.spyOn(api, 'reopenReviewTask').mockReturnValue(pendingReopen.promise)
    window.localStorage.setItem(reviewSessionStorageKey('dataset-1'), sessionA.id)
    render(
      <ReviewProvider enabled datasetId="dataset-1" onNavigateFrame={vi.fn()}>
        <ReviewAsyncProbe switchToSessionId={sessionB.id} />
      </ReviewProvider>,
    )

    await waitFor(() => expect(screen.getByLabelText('probe current session')).toHaveTextContent(sessionA.id))
    fireEvent.click(screen.getByRole('button', { name: 'probe reopen' }))
    await waitFor(() => expect(reopen).toHaveBeenCalledWith(taskA.id, expect.any(AbortSignal)))
    const signal = reopen.mock.calls[0][1]
    fireEvent.click(screen.getByRole('button', { name: 'probe switch session' }))
    await waitFor(() => expect(screen.getByLabelText('probe current session')).toHaveTextContent(sessionB.id))
    await waitFor(() => expect(signal?.aborted).toBe(true))

    await act(async () => pendingReopen.resolve({
      task: { ...taskA, status: 'todo', resolution: null },
    }))

    expect(screen.getByLabelText('probe current task')).toHaveTextContent(taskB.id)
    expect(screen.getByLabelText('probe current status')).toHaveTextContent('confirmed')
    expect(screen.getByLabelText('probe async error')).toBeEmptyDOMElement()
    expect(api.patchReviewTask).not.toHaveBeenCalledWith(
      taskA.id,
      expect.objectContaining({ status: 'in_progress' }),
      expect.anything(),
    )
  })

  it('does not publish an old QA completion refresh into the newly selected session', async () => {
    const sessionA = { ...SESSION, id: 'rvw_qa_a', last_task_id: 'task-qa-a' }
    const sessionB = { ...SESSION, id: 'rvw_qa_b', last_task_id: 'task-qa-b' }
    const taskA = { ...task('task-qa-a', 'in_progress', 'frame-1'), session_id: sessionA.id }
    const taskB = { ...task('task-qa-b', 'in_progress', 'frame-2'), session_id: sessionB.id }
    mockReviewApi({ sessions: [sessionA, sessionB], tasks: [] })
    vi.mocked(api.reviewTasks).mockImplementation(async (sessionId) => {
      const items = sessionId === sessionA.id ? [taskA] : [taskB]
      return {
        items,
        total: 1,
        offset: 0,
        limit: 200,
        next_offset: null,
        status_counts: { in_progress: 1 },
      }
    })
    const pendingCompletion = deferred<Awaited<ReturnType<typeof api.reviewCompletionStatus>>>()
    const completion = vi.mocked(api.reviewCompletionStatus).mockReturnValue(pendingCompletion.promise)
    window.localStorage.setItem(reviewSessionStorageKey('dataset-1'), sessionA.id)
    render(
      <ReviewProvider enabled datasetId="dataset-1" onNavigateFrame={vi.fn()}>
        <ReviewAsyncProbe switchToSessionId={sessionB.id} />
      </ReviewProvider>,
    )

    await waitFor(() => expect(screen.getByLabelText('probe current session')).toHaveTextContent(sessionA.id))
    fireEvent.click(screen.getByRole('button', { name: 'probe record qa' }))
    await waitFor(() => expect(completion).toHaveBeenCalledWith(sessionA.id, expect.any(AbortSignal)))
    const signal = completion.mock.calls[0][1]
    fireEvent.click(screen.getByRole('button', { name: 'probe switch session' }))
    await waitFor(() => expect(screen.getByLabelText('probe current session')).toHaveTextContent(sessionB.id))
    await waitFor(() => expect(signal?.aborted).toBe(true))

    await act(async () => pendingCompletion.resolve({
      session_status: 'active',
      requirements_met: true,
      can_complete: true,
      blockers: {},
      checked_at: '2026-08-24T00:01:00Z',
    }))

    expect(screen.getByLabelText('probe current qa time')).toBeEmptyDOMElement()
    expect(screen.getByLabelText('probe completion')).toHaveTextContent('false')
    expect(screen.getByLabelText('probe checking completion')).toHaveTextContent('false')
    expect(screen.getByLabelText('probe async error')).toBeEmptyDOMElement()
  })

  it('does not roll a new session task back when an old automatic claim fails', async () => {
    const sessionA = { ...SESSION, id: 'rvw_claim_a', last_task_id: 'shared-task' }
    const sessionB = { ...SESSION, id: 'rvw_claim_b', last_task_id: 'shared-task' }
    const taskA = { ...task('shared-task', 'todo', 'frame-1'), session_id: sessionA.id }
    const taskB = { ...task('shared-task', 'in_progress', 'frame-2'), session_id: sessionB.id }
    mockReviewApi({ sessions: [sessionA, sessionB], tasks: [] })
    vi.mocked(api.reviewTasks).mockImplementation(async (sessionId) => {
      const items = sessionId === sessionA.id ? [taskA] : [taskB]
      return {
        items,
        total: 1,
        offset: 0,
        limit: 200,
        next_offset: null,
        status_counts: sessionId === sessionA.id ? { todo: 1 } : { in_progress: 1 },
      }
    })
    const pendingClaim = deferred<{ task: ReviewTask }>()
    vi.mocked(api.patchReviewTask).mockReturnValue(pendingClaim.promise)
    window.localStorage.setItem(reviewSessionStorageKey('dataset-1'), sessionA.id)
    render(
      <ReviewProvider enabled datasetId="dataset-1" onNavigateFrame={vi.fn()}>
        <ReviewAsyncProbe switchToSessionId={sessionB.id} />
      </ReviewProvider>,
    )

    await waitFor(() => expect(api.patchReviewTask).toHaveBeenCalledWith(taskA.id, {
      status: 'in_progress',
      claimed_by: 'operator-local',
    }))
    fireEvent.click(screen.getByRole('button', { name: 'probe switch session' }))
    await waitFor(() => expect(screen.getByLabelText('probe current session')).toHaveTextContent(sessionB.id))

    await act(async () => pendingClaim.reject(new Error('stale claim conflict')))

    expect(screen.getByLabelText('probe current task')).toHaveTextContent(taskB.id)
    expect(screen.getByLabelText('probe current status')).toHaveTextContent('in_progress')
    expect(screen.getByLabelText('probe updating task')).toBeEmptyDOMElement()
  })

  it('does not roll filtered task counts back when an old automatic claim fails', async () => {
    const activeSession = { ...SESSION, id: 'rvw_claim_filter', last_task_id: 'shared-filter-task' }
    const unfilteredTask = {
      ...task('shared-filter-task', 'todo', 'frame-1'),
      session_id: activeSession.id,
    }
    const filteredTask = {
      ...task('shared-filter-task', 'in_progress', 'frame-2'),
      session_id: activeSession.id,
      task_type: 'POLE_BASE_REVIEW' as const,
    }
    mockReviewApi({ sessions: [activeSession], tasks: [] })
    vi.mocked(api.reviewTasks).mockImplementation(async (_sessionId, _offset, _limit, _signal, filters) => {
      const filtered = filters?.task_type === 'POLE_BASE_REVIEW'
      return {
        items: [filtered ? filteredTask : unfilteredTask],
        total: 1,
        offset: 0,
        limit: 200,
        next_offset: null,
        status_counts: filtered ? { in_progress: 1 } : { todo: 1 },
      }
    })
    const pendingClaim = deferred<{ task: ReviewTask }>()
    vi.mocked(api.patchReviewTask).mockReturnValue(pendingClaim.promise)
    render(
      <ReviewProvider enabled datasetId="dataset-1" onNavigateFrame={vi.fn()}>
        <ReviewAsyncProbe switchToSessionId={activeSession.id} />
      </ReviewProvider>,
    )

    await waitFor(() => expect(api.patchReviewTask).toHaveBeenCalled())
    fireEvent.click(screen.getByRole('button', { name: 'probe pole filter' }))
    await waitFor(() => expect(screen.getByLabelText('probe current status')).toHaveTextContent('in_progress'))

    await act(async () => pendingClaim.reject(new Error('stale filtered claim conflict')))

    expect(screen.getByLabelText('probe current status')).toHaveTextContent('in_progress')
    expect(screen.getByLabelText('probe updating task')).toBeEmptyDOMElement()
  })

  it('does not apply a delayed task resolution after another session is selected', async () => {
    const sessionA = { ...SESSION, id: 'rvw_resolve_a', last_task_id: 'task-resolve-a' }
    const sessionB = { ...SESSION, id: 'rvw_resolve_b', last_task_id: 'task-resolve-b' }
    const taskA = { ...task('task-resolve-a', 'in_progress', 'frame-1'), session_id: sessionA.id }
    const taskB = { ...task('task-resolve-b', 'in_progress', 'frame-2'), session_id: sessionB.id }
    mockReviewApi({ sessions: [sessionA, sessionB], tasks: [] })
    vi.mocked(api.reviewTasks).mockImplementation(async (sessionId) => {
      const items = sessionId === sessionA.id ? [taskA] : [taskB]
      return {
        items,
        total: 1,
        offset: 0,
        limit: 200,
        next_offset: null,
        status_counts: { in_progress: 1 },
      }
    })
    const pendingResolution = deferred<{ task: ReviewTask }>()
    vi.spyOn(api, 'resolveReviewTask').mockReturnValue(pendingResolution.promise)
    window.localStorage.setItem(reviewSessionStorageKey('dataset-1'), sessionA.id)
    render(
      <ReviewProvider enabled datasetId="dataset-1" onNavigateFrame={vi.fn()}>
        <ReviewAsyncProbe switchToSessionId={sessionB.id} />
      </ReviewProvider>,
    )

    await waitFor(() => expect(screen.getByLabelText('probe current session')).toHaveTextContent(sessionA.id))
    fireEvent.click(screen.getByRole('button', { name: 'probe resolve' }))
    await waitFor(() => expect(api.resolveReviewTask).toHaveBeenCalledWith(taskA.id, { resolution: 'confirmed' }))
    fireEvent.click(screen.getByRole('button', { name: 'probe switch session' }))
    await waitFor(() => expect(screen.getByLabelText('probe current session')).toHaveTextContent(sessionB.id))

    await act(async () => pendingResolution.resolve({
      task: { ...taskA, status: 'confirmed', resolution: 'confirmed' },
    }))

    expect(screen.getByLabelText('probe current task')).toHaveTextContent(taskB.id)
    expect(screen.getByLabelText('probe current status')).toHaveTextContent('in_progress')
    expect(screen.getByLabelText('probe async error')).toBeEmptyDOMElement()
  })

  it('does not restore an old task query when a resolution finishes after filters change', async () => {
    const activeSession = { ...SESSION, id: 'rvw_resolve_filter', last_task_id: 'task-unfiltered' }
    const unfilteredTask = {
      ...task('task-unfiltered', 'in_progress', 'frame-1'),
      session_id: activeSession.id,
    }
    const filteredTask = {
      ...task('task-filtered', 'in_progress', 'frame-2'),
      session_id: activeSession.id,
      task_type: 'POLE_BASE_REVIEW' as const,
    }
    mockReviewApi({ sessions: [activeSession], tasks: [] })
    vi.mocked(api.reviewTasks).mockImplementation(async (_sessionId, _offset, _limit, _signal, filters) => {
      const items = filters?.task_type === 'POLE_BASE_REVIEW' ? [filteredTask] : [unfilteredTask]
      return {
        items,
        total: 1,
        offset: 0,
        limit: 200,
        next_offset: null,
        status_counts: { in_progress: 1 },
      }
    })
    const pendingResolution = deferred<{ task: ReviewTask }>()
    vi.spyOn(api, 'resolveReviewTask').mockReturnValue(pendingResolution.promise)
    render(
      <ReviewProvider enabled datasetId="dataset-1" onNavigateFrame={vi.fn()}>
        <ReviewAsyncProbe switchToSessionId={activeSession.id} />
      </ReviewProvider>,
    )

    await waitFor(() => expect(screen.getByLabelText('probe current task')).toHaveTextContent(unfilteredTask.id))
    fireEvent.click(screen.getByRole('button', { name: 'probe resolve' }))
    await waitFor(() => expect(api.resolveReviewTask).toHaveBeenCalled())
    fireEvent.click(screen.getByRole('button', { name: 'probe pole filter' }))
    await waitFor(() => expect(screen.getByLabelText('probe current task')).toHaveTextContent(filteredTask.id))

    await act(async () => pendingResolution.resolve({
      task: { ...unfilteredTask, status: 'confirmed', resolution: 'confirmed' },
    }))

    expect(screen.getByLabelText('probe current task')).toHaveTextContent(filteredTask.id)
    expect(screen.getByLabelText('probe current status')).toHaveTextContent('in_progress')
  })

  it('keeps aggregate task counts consistent across automatic claim and resolution', async () => {
    const todoTask = task('rvt_claim_count', 'todo', 'frame-1')
    const activeSession = { ...SESSION, last_task_id: todoTask.id }
    mockReviewApi({ sessions: [activeSession], tasks: [todoTask] })
    vi.spyOn(api, 'resolveReviewTask').mockResolvedValue({
      task: { ...todoTask, status: 'confirmed', resolution: 'confirmed' },
    })
    renderReview()

    await waitFor(() => expect(screen.getByRole('button', { name: '완료' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: '완료' }))

    await waitFor(() => expect(screen.getByLabelText('검수 진행률 100%')).toBeInTheDocument())
    expect(screen.getByText('/ 1 처리')).toBeInTheDocument()
  })

  it('shows the real completion blockers instead of sending a doomed completion patch', async () => {
    const completedTask = task('rvt_gate', 'confirmed', 'frame-1')
    const activeSession = { ...SESSION, last_task_id: completedTask.id }
    mockReviewApi({ sessions: [activeSession], tasks: [completedTask] })
    vi.mocked(api.reviewCompletionStatus).mockResolvedValue({
      session_status: 'active',
      requirements_met: false,
      can_complete: false,
      blockers: {
        open_tasks: 0,
        open_error_qa_issues: 2,
        qa_not_run: 0,
        stale_qa_target_layers: 1,
        pending_task_resolutions: 0,
        task_resolution_errors: 0,
        task_resolution_scan_truncated: 0,
      },
      checked_at: '2026-08-24T00:00:00Z',
    })
    renderReview()

    expect(await screen.findByText(/미해결 QA 오류 2개/)).toBeInTheDocument()
    expect(screen.getByText(/QA 이후 변경된 레이어 1개/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '검수 작업 완료' })).toBeDisabled()
    expect(api.patchReviewSession).not.toHaveBeenCalledWith(activeSession.id, { status: 'completed' })
  })

  it('honors the server completion gate for an empty but QA-validated work package', async () => {
    const qaValidated = {
      ...SESSION,
      last_task_id: null,
      qa_ran_at: '2026-08-24T00:00:00Z',
      qa_layer_revisions: {},
    }
    mockReviewApi({ sessions: [qaValidated], tasks: [] })
    vi.mocked(api.reviewCompletionStatus).mockResolvedValue({
      session_status: 'active',
      requirements_met: true,
      can_complete: true,
      blockers: {
        open_tasks: 0,
        open_error_qa_issues: 0,
        qa_not_run: 0,
        stale_qa_target_layers: 0,
        pending_task_resolutions: 0,
        task_resolution_errors: 0,
        task_resolution_scan_truncated: 0,
      },
      checked_at: '2026-08-24T00:00:00Z',
    })
    renderReview()

    const complete = await screen.findByRole('button', { name: '검수 작업 완료' })
    expect(screen.getByText(/후보를 추가하거나, 확인할 후보가 없다면 QA 검사/)).toBeInTheDocument()
    await waitFor(() => expect(complete).toBeEnabled())
    fireEvent.click(complete)
    await waitFor(() => expect(api.patchReviewSession).toHaveBeenCalledWith(
      qaValidated.id,
      { status: 'completed' },
    ))
  })

  it('guards resolve actions until the current task is actually claimed', async () => {
    const todoTask = task('rvt_todo', 'todo', 'frame-1')
    const session = { ...SESSION, last_task_id: todoTask.id }
    mockReviewApi({ sessions: [session], tasks: [todoTask] })
    vi.mocked(api.patchReviewTask).mockRejectedValue(new Error('claim conflict'))
    const resolve = vi.spyOn(api, 'resolveReviewTask')
    renderReview()

    await waitFor(() => expect(screen.getByRole('button', { name: '완료' })).toBeDisabled())
    expect(screen.getByRole('button', { name: '건너뛰기' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '현장조사' })).toBeDisabled()
    expect(resolve).not.toHaveBeenCalled()
  })

  it('loads the next bounded task page and offers the corrected status filter', async () => {
    const firstTasks = Array.from({ length: 2 }, (_, index) => task(`first-${index}`, 'in_progress', `frame-${index + 1}`))
    const moreTask = task('more-1', 'corrected', 'frame-3')
    const session = { ...SESSION, last_task_id: firstTasks[0].id }
    mockReviewApi({ sessions: [session], tasks: firstTasks })
    vi.mocked(api.reviewTasks)
      .mockResolvedValueOnce({
        items: firstTasks,
        total: 3,
        offset: 0,
        limit: 200,
        next_offset: 2,
        next_cursor: 'cursor-page-2',
      })
      .mockResolvedValueOnce({
        items: [moreTask],
        total: 3,
        offset: 0,
        limit: 200,
        next_offset: null,
        next_cursor: null,
      })
    renderReview()

    expect(await screen.findByRole('option', { name: '수정 완료' })).toBeInTheDocument()
    fireEvent.click(await screen.findByRole('button', { name: '다음 200개 불러오기' }))
    await waitFor(() => expect(screen.getByRole('button', { name: /수동 확인 수정 완료/ })).toBeInTheDocument())
    expect(api.reviewTasks).toHaveBeenLastCalledWith(
      session.id,
      0,
      200,
      undefined,
      { status: undefined, task_type: undefined, cursor: 'cursor-page-2' },
    )
  })

  it('restores dataset-scoped queue filters and sends them with the bounded first page', async () => {
    mockReviewApi()
    window.localStorage.setItem(
      reviewQueueFilterStorageKey('dataset-1'),
      JSON.stringify({ status: 'corrected', taskType: 'POLE_BASE_REVIEW' }),
    )
    renderReview()

    const filters = await screen.findAllByRole('combobox')
    expect(filters).toHaveLength(3)
    expect(filters[1]).toHaveValue('corrected')
    expect(filters[2]).toHaveValue('POLE_BASE_REVIEW')
    await waitFor(() => expect(api.reviewTasks).toHaveBeenCalledWith(
      expect.any(String),
      0,
      200,
      expect.any(AbortSignal),
      { status: 'corrected', task_type: 'POLE_BASE_REVIEW' },
    ))
  })

  it('supports pause/resume, hides generation for terminal sessions, and keeps exports available', async () => {
    mockReviewApi()
    window.localStorage.setItem(reviewSessionStorageKey('dataset-1'), PAUSED_SESSION.id)
    renderReview()
    const resume = await screen.findByRole('button', { name: /재개/ })
    expect(screen.queryByRole('button', { name: /후보 추가/ })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '완료' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '건너뛰기' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '현장조사' })).toBeDisabled()
    expect(screen.getByRole('button', { name: /오검출/ })).toBeDisabled()
    expect(screen.getByRole('button', { name: /나중에 확인/ })).toBeDisabled()

    fireEvent.click(resume)
    await waitFor(() => expect(api.patchReviewSession).toHaveBeenCalledWith(PAUSED_SESSION.id, { status: 'active' }))
    await waitFor(() => expect(screen.getByRole('button', { name: /후보 추가/ })).toBeInTheDocument())
    fireEvent.click(screen.getByText('결과 받기'))
    expect(screen.getByRole('link', { name: '보고서 JSON' })).toHaveAttribute(
      'href',
      `/api/review-sessions/${PAUSED_SESSION.id}/report?format=json`,
    )

    cleanup()
    vi.restoreAllMocks()
    const completed = { ...SESSION, id: 'rvw_done', status: 'completed' as const, last_task_id: 'done' }
    mockReviewApi({ sessions: [completed], tasks: [task('done', 'confirmed', 'frame-1')] })
    renderReview()
    await screen.findByRole('button', { name: /새 검수 작업/ })
    expect(screen.queryByRole('button', { name: /후보 추가/ })).not.toBeInTheDocument()
    expect(screen.getByText(/완료된 검수 작업/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '완료 조건 다시 확인' })).not.toBeInTheDocument()
  })

  it('shows explainable priority evidence separately from the numeric priority', () => {
    expect(reviewTaskEvidenceSummary({
      ...task('reasoned', 'todo', 'frame-1'),
      priority_evidence: { reason: 'projection failed', source_weight: 1.25, adjustment: -4 },
    })).toBe('projection failed · source 가중치 1.25 · 보정 -4.0')
  })

  it('summarizes run, layer, track, and frame scope without an opaque work id', () => {
    expect(reviewSessionScopeSummary(
      SESSION,
      new Map([['run-1', '교통시설 8월 분석']]),
      new Map([['layer-1', '표지판 검수 레이어']]),
    )).toEqual({
      run: '교통시설 8월 분석',
      layer: '표지판 검수 레이어',
      location: 'Track01 · frame 0–100',
      option: '교통시설 8월 분석 · 표지판 검수 레이어 · Track01 · frame 0–100 · 진행 중',
    })
  })

  it('shows the full ordinal span represented by an interval task', () => {
    expect(reviewTaskFrameSummary({
      ...task('interval', 'todo', 'frame-025'),
      task_type: 'UNREVIEWED_INTERVAL',
      frame_start: 0,
      frame_end: 49,
    })).toBe('Track01 · frame 0–49 · 50개 프레임')
  })
})
