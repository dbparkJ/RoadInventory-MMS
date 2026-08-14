import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, api } from '../lib/api'
import type { DatasetSummary, RunRecord, RunResults } from '../types'
import { completedRunsForDataset, LatestRunResults } from './LatestRunResults'

const DATASET: DatasetSummary = {
  id: 'dataset/a',
  name: '강남 검출 구간',
  status: 'ready',
  frame_count: 10,
  tracks: [],
}

const RUN: RunRecord = {
  id: 'run-first',
  dataset_id: DATASET.id,
  dataset_name: DATASET.name,
  status: 'completed',
  progress: 100,
  created_at: '2026-08-13T01:00:00Z',
  finished_at: '2026-08-13T01:05:00Z',
  request: {
    dataset_id: DATASET.id,
    track_ids: ['track-a'],
    frame_range: [2, 7],
    mode: 'automatic',
    auto: { preset: 'balanced' },
  },
}

const SECOND_RUN: RunRecord = {
  ...RUN,
  id: 'run-second',
  created_at: '2026-08-13T02:00:00Z',
  finished_at: '2026-08-13T02:05:00Z',
}

const RESULTS: RunResults = { files: [], shapefiles: [], file_count: 0 }

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('LatestRunResults job history', () => {
  it('loads all durable completed jobs and opens the selected result detail', async () => {
    let resolveRuns: ((value: { items: RunRecord[] }) => void) | undefined
    const completed = vi.spyOn(api, 'completedRuns').mockImplementation(
      () => new Promise((resolve) => { resolveRuns = resolve }),
    )
    vi.spyOn(api, 'runResults').mockResolvedValue(RESULTS)

    render(
      <LatestRunResults dataset={DATASET} runs={[]} demoMode={false} onOpenQueue={vi.fn()} />,
    )
    fireEvent.click(screen.getByRole('button', { name: '검출결과' }))

    expect(screen.getByText('완료된 자동 검출 작업을 불러오고 있습니다.')).toBeInTheDocument()
    await act(async () => resolveRuns?.({ items: [SECOND_RUN, RUN] }))
    expect(await screen.findByText(/완료 작업 2건/)).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /상세 보기/ })).toHaveLength(2)

    fireEvent.click(screen.getAllByRole('button', { name: /상세 보기/ })[1])
    await waitFor(() => {
      expect(screen.getByRole('dialog', { name: '검출 결과' }).querySelector('header small'))
        .toHaveTextContent(RUN.id)
    })
    expect(completed).toHaveBeenCalledWith(DATASET.id, expect.any(AbortSignal), 200, 0, undefined)
  })

  it('distinguishes an empty job history from lookup failure and supports retry', async () => {
    vi.spyOn(api, 'completedRuns')
      .mockRejectedValueOnce(new Error('완료 작업 조회 실패'))
      .mockResolvedValueOnce({ items: [] })

    render(
      <LatestRunResults dataset={DATASET} runs={[]} demoMode={false} onOpenQueue={vi.fn()} />,
    )
    fireEvent.click(screen.getByRole('button', { name: '검출결과' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('완료 작업 조회 실패')
    fireEvent.click(screen.getByRole('button', { name: '다시 시도' }))
    expect(await screen.findByText('완료된 자동 검출 작업이 없습니다')).toBeInTheDocument()
  })

  it('sorts and shows every selected-dataset demo result', async () => {
    const unrelated = { ...SECOND_RUN, id: 'run-other', dataset_id: 'dataset-other' }
    vi.spyOn(api, 'runResults').mockResolvedValue(RESULTS)

    expect(completedRunsForDataset([RUN, unrelated, SECOND_RUN], DATASET.id))
      .toEqual([SECOND_RUN, RUN])
    render(
      <LatestRunResults
        dataset={DATASET}
        runs={[RUN, unrelated, SECOND_RUN]}
        demoMode
        onOpenQueue={vi.fn()}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: '검출결과' }))
    expect(await screen.findByText(/완료 작업 2건/)).toBeInTheDocument()
  })

  it('falls back to the legacy queue collection when an old server returns 404', async () => {
    vi.spyOn(api, 'completedRuns').mockRejectedValue(new ApiError('Not Found', 404))
    const legacyRuns = vi.spyOn(api, 'runs').mockResolvedValue({ items: [RUN] })

    render(
      <LatestRunResults dataset={DATASET} runs={[]} demoMode={false} onOpenQueue={vi.fn()} />,
    )
    fireEvent.click(screen.getByRole('button', { name: '검출결과' }))

    expect(await screen.findByText(/완료 작업 1건/)).toBeInTheDocument()
    expect(legacyRuns).toHaveBeenCalledWith(expect.any(AbortSignal), 200)
    expect(screen.queryByText('Not Found')).not.toBeInTheDocument()
  })

  it('loads every completed-run page before rendering the history', async () => {
    const firstPage = Array.from({ length: 200 }, (_, index) => ({
      ...RUN,
      id: `run-${String(index).padStart(3, '0')}`,
    }))
    const completed = vi.spyOn(api, 'completedRuns')
      .mockResolvedValueOnce({
        items: firstPage,
        total: 201,
        next_offset: 200,
        snapshot_at: '2026-08-14T01:02:03+00:00',
      })
      .mockResolvedValueOnce({
        items: [{ ...RUN, id: 'run-last' }],
        total: 201,
        next_offset: null,
        snapshot_at: '2026-08-14T01:02:03+00:00',
      })

    render(
      <LatestRunResults dataset={DATASET} runs={[]} demoMode={false} onOpenQueue={vi.fn()} />,
    )
    fireEvent.click(screen.getByRole('button', { name: '검출결과' }))

    expect(await screen.findByText(/완료 작업 201건/)).toBeInTheDocument()
    expect(completed).toHaveBeenNthCalledWith(
      1,
      DATASET.id,
      expect.any(AbortSignal),
      200,
      0,
      undefined,
    )
    expect(completed).toHaveBeenNthCalledWith(
      2,
      DATASET.id,
      expect.any(AbortSignal),
      200,
      200,
      '2026-08-14T01:02:03+00:00',
    )
  })

  it('moves focus into the history dialog, traps Tab, and restores the trigger on close', async () => {
    render(
      <LatestRunResults dataset={DATASET} runs={[RUN]} demoMode onOpenQueue={vi.fn()} />,
    )
    const trigger = screen.getByRole('button', { name: '검출결과' })
    trigger.focus()
    fireEvent.click(trigger)

    const dialog = await screen.findByRole('dialog', { name: '작업별 검출결과' })
    const closeButton = within(dialog).getByRole('button', { name: '결과 목록 닫기' })
    const resultCard = within(dialog).getByRole('button', { name: /상세 보기/ })
    await waitFor(() => expect(closeButton).toHaveFocus())

    fireEvent.keyDown(document, { key: 'Tab', shiftKey: true })
    expect(resultCard).toHaveFocus()
    fireEvent.keyDown(document, { key: 'Tab' })
    expect(closeButton).toHaveFocus()

    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(dialog).not.toBeInTheDocument())
    expect(trigger).toHaveFocus()
  })
})
