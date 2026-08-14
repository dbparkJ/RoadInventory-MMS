import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { DatasetSummary, Frame } from '../types'
import { OptimizationPanel } from './OptimizationPanel'

const READY_DATASET: DatasetSummary = {
  id: 'dataset-1',
  name: 'Test delivery',
  status: 'ready',
  frame_count: 240,
  tracks: [
    {
      id: 'track-1',
      name: 'Track 01',
      frame_count: 240,
    },
  ],
}

const FRAME: Frame = {
  id: 'frame-21',
  index: 20,
  track_id: 'track-1',
  timestamp: '2026-08-03T09:30:00.000Z',
  coordinate: { lon: 127, lat: 37 },
  has_panorama: true,
  has_points: true,
}

afterEach(cleanup)

describe('OptimizationPanel', () => {
  it('includes the selected global ordinal range in the submitted run request', async () => {
    const onStart = vi.fn(async () => undefined)

    render(
      <OptimizationPanel
        dataset={READY_DATASET}
        selectedTrack="track-1"
        selectedFrame={FRAME}
        frameRange={[12, 48]}
        busy={false}
        onStart={onStart}
        onOptimize={vi.fn(async () => undefined)}
        onSetFrameRangeStart={vi.fn()}
        onSetFrameRangeEnd={vi.fn()}
        onFrameRangeChange={vi.fn()}
        onClearFrameRange={vi.fn()}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: '작업 시작' }))

    await waitFor(() => expect(onStart).toHaveBeenCalledTimes(1))
    expect(onStart).toHaveBeenCalledWith(
      expect.objectContaining({
        dataset_id: 'dataset-1',
        track_ids: ['track-1'],
        frame_range: [12, 48],
        mode: 'automatic',
      }),
    )
    expect(screen.getAllByText('ordinal 12–48')).toHaveLength(2)
  })

  it('owns the execution range controls inside automatic detection setup', () => {
    const onFrameRangeChange = vi.fn()
    const onSetFrameRangeStart = vi.fn()
    render(
      <OptimizationPanel
        dataset={READY_DATASET}
        selectedTrack="track-1"
        selectedFrame={FRAME}
        frameRange={null}
        busy={false}
        onStart={vi.fn(async () => undefined)}
        onOptimize={vi.fn(async () => undefined)}
        onSetFrameRangeStart={onSetFrameRangeStart}
        onSetFrameRangeEnd={vi.fn()}
        onFrameRangeChange={onFrameRangeChange}
        onClearFrameRange={vi.fn()}
      />,
    )

    fireEvent.change(screen.getByRole('spinbutton', { name: '실행 시작 프레임 번호' }), {
      target: { value: '12' },
    })
    fireEvent.change(screen.getByRole('spinbutton', { name: '실행 끝 프레임 번호' }), {
      target: { value: '5' },
    })
    fireEvent.click(screen.getByRole('button', { name: '적용' }))
    expect(onFrameRangeChange).toHaveBeenCalledWith([4, 11])

    fireEvent.click(screen.getByRole('button', { name: '시작 지정' }))
    expect(onSetFrameRangeStart).toHaveBeenCalledWith(20)
  })

  it('keeps global dataset ordinals available for a short later track', () => {
    const laterFrame = { ...FRAME, index: 199, track_id: 'track-2' }
    const multiTrackDataset: DatasetSummary = {
      ...READY_DATASET,
      frame_count: 240,
      tracks: [
        { id: 'track-1', name: 'Track 01', frame_count: 180 },
        { id: 'track-2', name: 'Track 02', frame_count: 60 },
      ],
    }
    render(
      <OptimizationPanel
        dataset={multiTrackDataset}
        selectedTrack="track-2"
        selectedFrame={laterFrame}
        frameRange={null}
        busy={false}
        onStart={vi.fn(async () => undefined)}
        onOptimize={vi.fn(async () => undefined)}
        onSetFrameRangeStart={vi.fn()}
        onSetFrameRangeEnd={vi.fn()}
        onFrameRangeChange={vi.fn()}
        onClearFrameRange={vi.fn()}
      />,
    )

    expect(screen.getByRole('spinbutton', { name: '실행 시작 프레임 번호' })).toHaveAttribute(
      'max',
      '240',
    )
  })
})
