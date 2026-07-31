import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { DatasetSummary } from '../types'
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

describe('OptimizationPanel', () => {
  it('includes the selected global ordinal range in the submitted run request', async () => {
    const onStart = vi.fn(async () => undefined)

    render(
      <OptimizationPanel
        dataset={READY_DATASET}
        selectedTrack="track-1"
        frameRange={[12, 48]}
        busy={false}
        onStart={onStart}
        onOptimize={vi.fn(async () => undefined)}
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
    expect(screen.getByText('ordinal 12–48')).toBeInTheDocument()
  })
})
