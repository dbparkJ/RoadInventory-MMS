import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../lib/api'
import type { QaIssue, ReviewSession } from '../types'
import { QaIssuePanel } from './QaIssuePanel'
import { ReviewProvider } from './ReviewContext'

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

function issue(id: string): QaIssue {
  return {
    id,
    session_id: SESSION.id,
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

function mockReviewApi() {
  vi.spyOn(api, 'reviewSessions').mockResolvedValue({
    items: [SESSION], total: 1, offset: 0, limit: 100, next_offset: null,
  })
  vi.spyOn(api, 'reviewSession').mockResolvedValue({ session: SESSION })
  vi.spyOn(api, 'reviewTasks').mockResolvedValue({
    items: [], total: 0, offset: 0, limit: 200, next_offset: null, status_counts: {},
  })
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
    expect(await screen.findByText(first.message)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '다음 200개 불러오기' }))
    expect(await screen.findByText(second.message)).toBeInTheDocument()
    expect(qaIssues).toHaveBeenNthCalledWith(2, SESSION.id, {
      offset: 200,
      limit: 200,
      status: 'open',
      severity: undefined,
    })
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
})
