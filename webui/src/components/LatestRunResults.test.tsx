import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, api } from '../lib/api'
import type { DatasetSummary, RunRecord, RunResults } from '../types'
import { LatestRunResults, latestCompletedRunForDataset } from './LatestRunResults'

const DATASET: DatasetSummary = {
  id: 'dataset/a',
  name: '강남 검출 구간',
  status: 'ready',
  frame_count: 10,
  tracks: [],
}

const RUN: RunRecord = {
  id: 'run-server-latest',
  dataset_id: DATASET.id,
  dataset_name: DATASET.name,
  status: 'completed',
  progress: 100,
  created_at: '2026-08-13T01:00:00Z',
  finished_at: '2026-08-13T01:05:00Z',
}

const RESULTS: RunResults = {
  files: [],
  shapefiles: [],
  file_count: 0,
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('LatestRunResults', () => {
  it('loads the durable server result on click instead of trusting the recent run list', async () => {
    let resolveLatest: ((value: { run: RunRecord | null }) => void) | undefined
    const latest = vi.spyOn(api, 'latestCompletedRun').mockImplementation(
      () => new Promise((resolve) => { resolveLatest = resolve }),
    )
    vi.spyOn(api, 'runResults').mockResolvedValue(RESULTS)

    render(
      <LatestRunResults
        dataset={DATASET}
        runs={[]}
        demoMode={false}
        onOpenQueue={vi.fn()}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: '검출결과' }))

    expect(screen.getByText('최신 완료 실행을 확인하고 있습니다.')).toBeInTheDocument()
    await act(async () => resolveLatest?.({ run: RUN }))
    await waitFor(() => {
      expect(screen.getByRole('dialog').querySelector('header small')).toHaveTextContent(RUN.id)
    })
    expect(latest).toHaveBeenCalledWith(DATASET.id, expect.any(AbortSignal))
  })

  it('distinguishes an empty durable result from lookup failure and supports retry', async () => {
    const latest = vi
      .spyOn(api, 'latestCompletedRun')
      .mockRejectedValueOnce(new Error('서버 최신 실행 조회 실패'))
      .mockResolvedValueOnce({ run: null })

    render(
      <LatestRunResults
        dataset={DATASET}
        runs={[]}
        demoMode={false}
        onOpenQueue={vi.fn()}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: '검출결과' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('서버 최신 실행 조회 실패')
    fireEvent.click(screen.getByRole('button', { name: '다시 시도' }))

    expect(await screen.findByText('완료된 자동 검출결과가 없습니다')).toBeInTheDocument()
    expect(latest).toHaveBeenCalledTimes(2)
  })

  it('uses the selected dataset local fallback only in demo mode', async () => {
    const latest = vi.spyOn(api, 'latestCompletedRun')
    vi.spyOn(api, 'runResults').mockResolvedValue(RESULTS)
    const newer = { ...RUN, id: 'run-demo-newer', finished_at: '2026-08-13T02:00:00Z' }
    const unrelated = { ...newer, id: 'run-other', dataset_id: 'dataset-other' }

    expect(latestCompletedRunForDataset([RUN, unrelated, newer], DATASET.id)).toBe(newer)
    render(
      <LatestRunResults
        dataset={DATASET}
        runs={[RUN, unrelated, newer]}
        demoMode
        onOpenQueue={vi.fn()}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: '검출결과' }))

    await waitFor(() => {
      expect(screen.getByRole('dialog').querySelector('header small')).toHaveTextContent(newer.id)
    })
    expect(latest).not.toHaveBeenCalled()
  })

  it('orders legacy fallback runs by completion time with stable fallbacks', () => {
    const finishedLast = {
      ...RUN,
      id: 'run-created-first-finished-last',
      created_at: '2026-08-13T00:00:00Z',
      finished_at: '2026-08-13T03:00:00Z',
    }
    const createdLast = {
      ...RUN,
      id: 'run-created-last-finished-first',
      created_at: '2026-08-13T01:00:00Z',
      finished_at: '2026-08-13T02:00:00Z',
    }
    const legacyWithoutFinishedAt = {
      ...RUN,
      id: 'run-legacy-updated',
      created_at: '2026-08-12T00:00:00Z',
      updated_at: '2026-08-13T04:00:00Z',
      finished_at: undefined,
    }

    expect(
      latestCompletedRunForDataset(
        [createdLast, finishedLast, legacyWithoutFinishedAt],
        DATASET.id,
      ),
    ).toBe(legacyWithoutFinishedAt)
  })

  it('falls back to the legacy run collection when an old server returns 404', async () => {
    const latest = vi
      .spyOn(api, 'latestCompletedRun')
      .mockRejectedValue(new ApiError('Not Found', 404))
    const legacyRuns = vi.spyOn(api, 'runs').mockResolvedValue({ items: [RUN] })
    vi.spyOn(api, 'runResults').mockResolvedValue(RESULTS)

    render(
      <LatestRunResults
        dataset={DATASET}
        runs={[]}
        demoMode={false}
        onOpenQueue={vi.fn()}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: '검출결과' }))

    await waitFor(() => {
      expect(screen.getByRole('dialog').querySelector('header small')).toHaveTextContent(RUN.id)
    })
    expect(latest).toHaveBeenCalledOnce()
    expect(legacyRuns).toHaveBeenCalledWith(expect.any(AbortSignal), 200)
    expect(screen.queryByText('Not Found')).not.toBeInTheDocument()
  })
})
