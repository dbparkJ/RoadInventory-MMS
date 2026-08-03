import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { ComponentProps } from 'react'
import type { DatasetSummary, Frame } from '../types'
import { DatasetPanel } from './DatasetPanel'

const DATASET: DatasetSummary = {
  id: 'dataset-1',
  name: 'Test delivery',
  status: 'ready',
  frame_count: 100,
  tracks: [
    {
      id: 'track-1',
      name: 'Track 01',
      frame_count: 100,
    },
  ],
}

const FRAMES: Frame[] = [
  {
    id: 'frame-11',
    index: 10,
    track_id: 'track-1',
    timestamp: '2026-08-03T09:30:00.000Z',
    coordinate: { lon: 126.978, lat: 37.5665 },
    has_panorama: true,
    has_points: true,
  },
  {
    id: 'frame-21',
    index: 20,
    track_id: 'track-1',
    timestamp: '2026-08-03T09:30:01.000Z',
    coordinate: { lon: 126.979, lat: 37.567 },
    has_panorama: true,
    has_points: true,
  },
  {
    id: 'frame-31',
    index: 30,
    track_id: 'track-1',
    timestamp: '2026-08-03T09:30:02.000Z',
    coordinate: { lon: 126.98, lat: 37.568 },
    has_panorama: true,
    has_points: true,
  },
]

function renderPanel(overrides: Partial<ComponentProps<typeof DatasetPanel>> = {}) {
  const props: ComponentProps<typeof DatasetPanel> = {
    datasets: [DATASET],
    selectedDataset: DATASET,
    selectedTrack: 'track-1',
    frames: FRAMES,
    selectedFrame: FRAMES[0],
    framesLoading: false,
    framesLoadingMore: false,
    frameTotal: FRAMES.length,
    hasMoreFrames: false,
    frameRange: null,
    onDatasetChange: vi.fn(),
    onTrackChange: vi.fn(),
    onFrameChange: vi.fn(),
    onSetFrameRangeStart: vi.fn(),
    onSetFrameRangeEnd: vi.fn(),
    onFrameRangeChange: vi.fn(),
    onClearFrameRange: vi.fn(),
    onLoadMoreFrames: vi.fn(),
    onOpenSource: vi.fn(),
    ...overrides,
  }

  return { ...render(<DatasetPanel {...props} />), props }
}

afterEach(cleanup)

describe('DatasetPanel frame range selection', () => {
  it('applies a one-based numeric range as an ordered zero-based ordinal range', () => {
    const onFrameRangeChange = vi.fn()
    renderPanel({ onFrameRangeChange })

    fireEvent.change(screen.getByRole('spinbutton', { name: '실행 시작 프레임 번호' }), {
      target: { value: '12' },
    })
    fireEvent.change(screen.getByRole('spinbutton', { name: '실행 끝 프레임 번호' }), {
      target: { value: '5' },
    })
    fireEvent.click(screen.getByRole('button', { name: '적용' }))

    expect(onFrameRangeChange).toHaveBeenCalledOnce()
    expect(onFrameRangeChange).toHaveBeenCalledWith([4, 11])
  })

  it('selects the range from the current frame through a Shift-clicked frame', () => {
    const onFrameChange = vi.fn()
    const onFrameRangeChange = vi.fn()
    renderPanel({ onFrameChange, onFrameRangeChange, selectedFrame: FRAMES[0] })

    const targetRow = screen.getByText('frame-31').closest('button')
    expect(targetRow).not.toBeNull()
    fireEvent.click(targetRow!, { shiftKey: true })

    expect(onFrameRangeChange).toHaveBeenCalledOnce()
    expect(onFrameRangeChange).toHaveBeenCalledWith([10, 30])
    expect(onFrameChange).toHaveBeenCalledWith(FRAMES[2])
  })

  it('minimizes and restores the frame component without losing the panel', () => {
    renderPanel()

    fireEvent.click(screen.getByRole('button', { name: '프레임 컴포넌트 최소화' }))
    expect(screen.queryByRole('spinbutton', { name: '실행 시작 프레임 번호' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '프레임 컴포넌트 복원' })).toHaveAttribute(
      'aria-expanded',
      'false',
    )

    fireEvent.click(screen.getByRole('button', { name: '프레임 컴포넌트 복원' }))
    expect(screen.getByRole('spinbutton', { name: '실행 시작 프레임 번호' })).toBeInTheDocument()
  })

  it('offers a compact data explorer rail controlled by the parent layout', () => {
    const onToggleCollapsed = vi.fn()
    const { rerender, props } = renderPanel({ onToggleCollapsed })

    fireEvent.click(screen.getByRole('button', { name: '작업 데이터 패널 최소화' }))
    expect(onToggleCollapsed).toHaveBeenCalledOnce()

    rerender(<DatasetPanel {...props} collapsed onToggleCollapsed={onToggleCollapsed} />)
    expect(screen.getByRole('complementary', { name: '데이터 탐색기' })).toHaveAttribute(
      'data-collapsed',
      'true',
    )
    expect(screen.getByRole('button', { name: '작업 데이터 패널 복원' })).toBeInTheDocument()
    expect(screen.queryByRole('combobox', { name: '데이터셋 선택' })).not.toBeInTheDocument()
  })
})
