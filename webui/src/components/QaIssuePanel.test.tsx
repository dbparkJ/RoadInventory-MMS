import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../lib/api'
import type { QaIssue, ReviewSession } from '../types'
import { QaIssuePanel } from './QaIssuePanel'
import { ReviewProvider, useReviewWorkspace } from './ReviewContext'

const SESSION: ReviewSession = {
  id: 'session-qa',
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
  last_task_id: null,
  qa_layer_revisions: null,
  qa_ran_at: null,
}

const SESSION_B: ReviewSession = {
  ...SESSION,
  id: 'session-qa-b',
}

function issue(id: string, sessionId = SESSION.id): QaIssue {
  return {
    id,
    session_id: sessionId,
    layer_id: 'layer-1',
    feature_id: null,
    rule_id: 'REQUIRED_PROPERTY',
    severity: 'warning',
    message: `QA issue ${id}`,
    related_feature_ids: [],
    status: 'open',
    override_reason: null,
    frame_id: null,
    location_hint: null,
  }
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

function QaSessionSwitchProbe() {
  const review = useReviewWorkspace()
  return (
    <div>
      <button type="button" onClick={() => review.selectSession(SESSION_B.id)}>probe switch qa session</button>
      <output aria-label="probe qa session">{review.session?.id ?? ''}</output>
      <output aria-label="probe qa ran at">{review.session?.qa_ran_at ?? ''}</output>
    </div>
  )
}

function mockReviewApi() {
  vi.spyOn(api, 'reviewSessions').mockResolvedValue({
    items: [SESSION], total: 1, offset: 0, limit: 100, next_offset: null,
  })
  vi.spyOn(api, 'reviewSession').mockResolvedValue({ session: SESSION })
  vi.spyOn(api, 'reviewTasks').mockResolvedValue({
    items: [], total: 0, offset: 0, limit: 200, next_offset: null, status_counts: {},
  })
  vi.spyOn(api, 'reviewCompletionStatus').mockResolvedValue({
    session_status: 'active',
    requirements_met: false,
    can_complete: false,
    blockers: { qa_not_run: 1 },
    checked_at: '2026-08-24T00:00:00Z',
  })
}

function mockTwoQaSessions() {
  mockReviewApi()
  vi.mocked(api.reviewSessions).mockResolvedValue({
    items: [SESSION, SESSION_B], total: 2, offset: 0, limit: 100, next_offset: null,
  })
  vi.mocked(api.reviewSession).mockImplementation(async (sessionId) => ({
    session: sessionId === SESSION_B.id ? SESSION_B : SESSION,
  }))
}

afterEach(() => {
  cleanup()
  window.localStorage.clear()
  vi.restoreAllMocks()
})

describe('QaIssuePanel', () => {
  it('loads QA issues in bounded 200-item pages', async () => {
    mockReviewApi()
    const first = issue('issue-1')
    const second = issue('issue-201')
    const qaIssues = vi.spyOn(api, 'qaIssues')
      .mockResolvedValueOnce({
        items: [first], total: 201, offset: 0, limit: 200, next_offset: 200,
      })
      .mockResolvedValueOnce({
        items: [second], total: 201, offset: 200, limit: 200, next_offset: null,
      })

    render(
      <ReviewProvider enabled datasetId="dataset-1" onNavigateFrame={vi.fn()}>
        <QaIssuePanel />
      </ReviewProvider>,
    )

    fireEvent.click(await screen.findByRole('button', { name: 'QA 오류 탐색기 열기' }))
    expect(await screen.findByText(first.message, undefined, { timeout: 5_000 })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '다음 200개 불러오기' }))
    expect(await screen.findByText(second.message)).toBeInTheDocument()
    expect(qaIssues).toHaveBeenNthCalledWith(2, SESSION.id, {
      offset: 200,
      limit: 200,
      status: 'open',
      severity: undefined,
    }, expect.any(AbortSignal))
    await waitFor(() => expect(screen.queryByRole('button', { name: '다음 200개 불러오기' })).not.toBeInTheDocument())
  })

  it('requires data correction and QA rerun for error issues', async () => {
    mockReviewApi()
    const blockingError = {
      ...issue('issue-error'),
      severity: 'error' as const,
      message: 'Blocking geometry error',
    }
    vi.spyOn(api, 'qaIssues').mockResolvedValue({
      items: [blockingError], total: 1, offset: 0, limit: 200, next_offset: null,
    })
    const patchQaIssue = vi.spyOn(api, 'patchQaIssue')

    render(
      <ReviewProvider enabled datasetId="dataset-1" onNavigateFrame={vi.fn()}>
        <QaIssuePanel />
      </ReviewProvider>,
    )

    fireEvent.click(await screen.findByRole('button', { name: 'QA 오류 탐색기 열기' }))
    fireEvent.click(await screen.findByText(blockingError.message))
    expect(screen.getByText('오류는 데이터를 수정한 뒤 QA 검사를 다시 실행하면 자동으로 해소됩니다.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '해결 처리' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '사유로 무시' })).not.toBeInTheDocument()
    expect(patchQaIssue).not.toHaveBeenCalled()
  })

  it('refreshes the shared completion gate after QA runs', async () => {
    mockReviewApi()
    vi.spyOn(api, 'qaIssues').mockResolvedValue({
      items: [], total: 0, offset: 0, limit: 200, next_offset: null,
    })
    vi.spyOn(api, 'runQa').mockResolvedValue({
      items: [],
      total: 0,
      counts: { info: 0, warning: 0, error: 0 },
      ran_at: '2026-08-24T00:01:00Z',
      layer_revisions: { 'layer-1': 3 },
    })
    vi.mocked(api.reviewCompletionStatus)
      .mockResolvedValueOnce({
        session_status: 'active',
        requirements_met: false,
        can_complete: false,
        blockers: { qa_not_run: 1 },
        checked_at: '2026-08-24T00:00:00Z',
      })
      .mockResolvedValue({
        session_status: 'active',
        requirements_met: true,
        can_complete: true,
        blockers: { qa_not_run: 0 },
        checked_at: '2026-08-24T00:01:00Z',
      })

    render(
      <ReviewProvider enabled datasetId="dataset-1" onNavigateFrame={vi.fn()}>
        <QaIssuePanel />
      </ReviewProvider>,
    )

    fireEvent.click(await screen.findByRole('button', { name: 'QA 오류 탐색기 열기' }))
    fireEvent.click(screen.getByRole('button', { name: 'QA 검사 실행' }))

    await waitFor(() => expect(api.runQa).toHaveBeenCalledWith(SESSION.id, expect.any(AbortSignal)))
    await waitFor(() => expect(api.reviewCompletionStatus).toHaveBeenCalledTimes(2))
  })

  it('keeps the newly selected session issues when the old list resolves late', async () => {
    mockTwoQaSessions()
    const oldIssue = issue('issue-old-session')
    const newIssue = issue('issue-new-session', SESSION_B.id)
    const pendingOldList = deferred<Awaited<ReturnType<typeof api.qaIssues>>>()
    const qaIssues = vi.spyOn(api, 'qaIssues').mockImplementation(async (sessionId) => {
      if (sessionId === SESSION.id) return pendingOldList.promise
      return { items: [newIssue], total: 1, offset: 0, limit: 200, next_offset: null }
    })
    render(
      <ReviewProvider enabled datasetId="dataset-1" onNavigateFrame={vi.fn()}>
        <QaSessionSwitchProbe />
        <QaIssuePanel />
      </ReviewProvider>,
    )

    await waitFor(() => expect(screen.getByLabelText('probe qa session')).toHaveTextContent(SESSION.id))
    fireEvent.click(screen.getByRole('button', { name: 'QA 오류 탐색기 열기' }))
    await waitFor(() => expect(qaIssues).toHaveBeenCalledWith(
      SESSION.id,
      expect.any(Object),
      expect.any(AbortSignal),
    ))
    const oldSignal = qaIssues.mock.calls.find(([sessionId]) => sessionId === SESSION.id)?.[2]
    fireEvent.click(screen.getByRole('button', { name: 'probe switch qa session' }))

    expect(await screen.findByText(newIssue.message)).toBeInTheDocument()
    await waitFor(() => expect(oldSignal?.aborted).toBe(true))
    await act(async () => pendingOldList.resolve({
      items: [oldIssue], total: 1, offset: 0, limit: 200, next_offset: null,
    }))

    expect(screen.getByText(newIssue.message)).toBeInTheDocument()
    expect(screen.queryByText(oldIssue.message)).not.toBeInTheDocument()
  })

  it('does not record or reload an old QA run after the session changes', async () => {
    mockTwoQaSessions()
    const oldIssue = issue('issue-before-run')
    const newIssue = issue('issue-after-switch', SESSION_B.id)
    vi.spyOn(api, 'qaIssues').mockImplementation(async (sessionId) => ({
      items: [sessionId === SESSION.id ? oldIssue : newIssue],
      total: 1,
      offset: 0,
      limit: 200,
      next_offset: null,
    }))
    const pendingRun = deferred<Awaited<ReturnType<typeof api.runQa>>>()
    const runQa = vi.spyOn(api, 'runQa').mockReturnValue(pendingRun.promise)
    render(
      <ReviewProvider enabled datasetId="dataset-1" onNavigateFrame={vi.fn()}>
        <QaSessionSwitchProbe />
        <QaIssuePanel />
      </ReviewProvider>,
    )

    fireEvent.click(await screen.findByRole('button', { name: 'QA 오류 탐색기 열기' }))
    await screen.findByText(oldIssue.message, undefined, { timeout: 5_000 })
    fireEvent.click(screen.getByRole('button', { name: 'QA 검사 실행' }))
    await waitFor(() => expect(runQa).toHaveBeenCalledWith(SESSION.id, expect.any(AbortSignal)))
    const runSignal = runQa.mock.calls[0][1]
    fireEvent.click(screen.getByRole('button', { name: 'probe switch qa session' }))

    expect(await screen.findByText(newIssue.message)).toBeInTheDocument()
    await waitFor(() => expect(runSignal?.aborted).toBe(true))
    await act(async () => pendingRun.resolve({
      items: [],
      total: 0,
      counts: { info: 0, warning: 0, error: 0 },
      ran_at: '2026-08-24T00:02:00Z',
      layer_revisions: { 'layer-1': 4 },
    }))

    expect(screen.getByLabelText('probe qa session')).toHaveTextContent(SESSION_B.id)
    expect(screen.getByLabelText('probe qa ran at')).toBeEmptyDOMElement()
    expect(screen.getByText(newIssue.message)).toBeInTheDocument()
    expect(screen.queryByText(oldIssue.message)).not.toBeInTheDocument()
  }, 10_000)

  it('does not let an old issue patch take ownership of the new session list', async () => {
    mockTwoQaSessions()
    const oldIssue = issue('issue-patch-old')
    const newIssue = issue('issue-patch-new', SESSION_B.id)
    vi.spyOn(api, 'qaIssues').mockImplementation(async (sessionId) => ({
      items: [sessionId === SESSION.id ? oldIssue : newIssue],
      total: 1,
      offset: 0,
      limit: 200,
      next_offset: null,
    }))
    const pendingPatch = deferred<Awaited<ReturnType<typeof api.patchQaIssue>>>()
    const patchQaIssue = vi.spyOn(api, 'patchQaIssue').mockReturnValue(pendingPatch.promise)
    render(
      <ReviewProvider enabled datasetId="dataset-1" onNavigateFrame={vi.fn()}>
        <QaSessionSwitchProbe />
        <QaIssuePanel />
      </ReviewProvider>,
    )

    await waitFor(() => expect(screen.getByLabelText('probe qa session')).toHaveTextContent(SESSION.id))
    fireEvent.click(screen.getByRole('button', { name: 'QA 오류 탐색기 열기' }))
    fireEvent.click(await screen.findByText(oldIssue.message))
    fireEvent.click(screen.getByRole('button', { name: '해결 처리' }))
    await waitFor(() => expect(patchQaIssue).toHaveBeenCalledWith(
      oldIssue.id,
      { status: 'resolved' },
      expect.any(AbortSignal),
    ))
    const patchSignal = patchQaIssue.mock.calls[0][2]
    fireEvent.click(screen.getByRole('button', { name: 'probe switch qa session' }))

    expect(await screen.findByText(newIssue.message)).toBeInTheDocument()
    await waitFor(() => expect(patchSignal?.aborted).toBe(true))
    await act(async () => pendingPatch.resolve({ issue: { ...oldIssue, status: 'resolved' } }))

    expect(screen.getByText(newIssue.message)).toBeInTheDocument()
    expect(screen.queryByText(oldIssue.message)).not.toBeInTheDocument()
  })

  it('refreshes with the latest filters after an issue patch completes', async () => {
    mockReviewApi()
    const warningIssue = issue('issue-warning-filter')
    const errorIssue = {
      ...issue('issue-error-filter'),
      severity: 'error' as const,
    }
    const qaIssues = vi.spyOn(api, 'qaIssues').mockImplementation(async (_sessionId, options) => ({
      items: [options?.severity === 'error' ? errorIssue : warningIssue],
      total: 1,
      offset: 0,
      limit: 200,
      next_offset: null,
    }))
    const pendingPatch = deferred<Awaited<ReturnType<typeof api.patchQaIssue>>>()
    vi.spyOn(api, 'patchQaIssue').mockReturnValue(pendingPatch.promise)
    render(
      <ReviewProvider enabled datasetId="dataset-1" onNavigateFrame={vi.fn()}>
        <QaIssuePanel />
      </ReviewProvider>,
    )

    fireEvent.click(await screen.findByRole('button', { name: 'QA 오류 탐색기 열기' }))
    fireEvent.click(await screen.findByText(warningIssue.message, undefined, { timeout: 5_000 }))
    fireEvent.click(screen.getByRole('button', { name: '해결 처리' }))
    fireEvent.change(screen.getByRole('combobox', { name: 'QA 심각도 필터' }), {
      target: { value: 'error' },
    })
    expect(await screen.findByText(errorIssue.message)).toBeInTheDocument()

    await act(async () => pendingPatch.resolve({ issue: { ...warningIssue, status: 'resolved' } }))
    await waitFor(() => expect(qaIssues).toHaveBeenLastCalledWith(
      SESSION.id,
      expect.objectContaining({ severity: 'error' }),
      expect.any(AbortSignal),
    ))
    expect(screen.getByText(errorIssue.message)).toBeInTheDocument()
    expect(screen.queryByText(warningIssue.message)).not.toBeInTheDocument()
  })

  it('does not toggle the QA explorer with Q while the help modal is open', async () => {
    mockReviewApi()
    vi.spyOn(api, 'qaIssues').mockResolvedValue({
      items: [], total: 0, offset: 0, limit: 200, next_offset: null,
    })
    render(
      <ReviewProvider enabled datasetId="dataset-1" onNavigateFrame={vi.fn()}>
        <QaIssuePanel />
      </ReviewProvider>,
    )
    await screen.findByRole('button', { name: 'QA 오류 탐색기 열기' })
    render(<section role="dialog" aria-modal="true" aria-label="도움말" />)

    fireEvent.keyDown(window, { key: 'q', code: 'KeyQ' })

    expect(screen.getByRole('button', { name: 'QA 오류 탐색기 열기' })).toBeInTheDocument()
    expect(screen.queryByLabelText('QA 오류 탐색기', { selector: 'aside' })).not.toBeInTheDocument()
  })
})
