import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../lib/api'
import type {
  Frame,
  ReviewSession,
  ReviewTask,
  ReviewTaskResolution,
  ReviewTaskStatus,
} from '../types'
import {
  ReviewProvider,
  reviewQueueFilterStorageKey,
  reviewSessionStorageKey,
} from './ReviewContext'
import { ReviewQueue } from './ReviewQueue'
import { ReviewSessionBar } from './ReviewSessionBar'
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
      expect(screen.getByRole('option', { name: /rvw_1/ })).toBeInTheDocument(),
    )
    expect(screen.queryByRole('link', { name: 'Active-learning ZIP' })).not.toBeInTheDocument()
  })

  it('does not render or call review APIs when the capability is disabled', async () => {
    const reviewSessions = vi.spyOn(api, 'reviewSessions')
    renderReview(vi.fn(), false)

    expect(screen.queryByRole('region', { name: '검수 세션' })).not.toBeInTheDocument()
    expect(screen.queryByRole('complementary', { name: '검수 작업 큐' })).not.toBeInTheDocument()
    await Promise.resolve()
    expect(reviewSessions).not.toHaveBeenCalled()
  })

  it('restores a paused session without claiming todo tasks and still permits read-only navigation', async () => {
    mockReviewApi()
    window.localStorage.setItem(reviewSessionStorageKey('dataset-1'), PAUSED_SESSION.id)
    const { onNavigateFrame } = renderReview()

    await waitFor(() =>
      expect(screen.getByRole('combobox', { name: '검수 세션 선택' })).toHaveValue(
        PAUSED_SESSION.id,
      ),
    )
    await waitFor(() => expect(onNavigateFrame).toHaveBeenCalledWith(frame('frame-2'), 20))
    expect(screen.getByRole('button', { name: /수동 확인 검수 중/ })).toHaveAttribute(
      'aria-current',
      'true',
    )
    expect(screen.getByLabelText('검수 진행률 33%')).toBeInTheDocument()
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
      expect(screen.getByRole('combobox', { name: '검수 세션 선택' })).toHaveValue(
        draftSession.id,
      ),
    )
    expect(api.patchReviewTask).not.toHaveBeenCalled()
    expect(screen.queryByRole('button', { name: /후보 생성/ })).not.toBeInTheDocument()
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
    expect(screen.queryByRole('button', { name: /후보 생성/ })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '완료' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '건너뛰기' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '현장조사' })).toBeDisabled()
    expect(screen.getByRole('button', { name: /오검출/ })).toBeDisabled()
    expect(screen.getByRole('button', { name: /나중에 확인/ })).toBeDisabled()

    fireEvent.click(resume)
    await waitFor(() => expect(api.patchReviewSession).toHaveBeenCalledWith(PAUSED_SESSION.id, { status: 'active' }))
    await waitFor(() => expect(screen.getByRole('button', { name: /후보 생성/ })).toBeInTheDocument())
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
    await screen.findByRole('button', { name: /새 세션/ })
    expect(screen.queryByRole('button', { name: /후보 생성/ })).not.toBeInTheDocument()
  })

  it('shows explainable priority evidence separately from the numeric priority', () => {
    expect(reviewTaskEvidenceSummary({
      ...task('reasoned', 'todo', 'frame-1'),
      priority_evidence: { reason: 'projection failed', source_weight: 1.25, adjustment: -4 },
    })).toBe('projection failed · source 가중치 1.25 · 보정 -4.0')
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
