import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { ComponentProps } from 'react'
import type { DatasetSummary, Frame } from '../types'
import { formatCount } from '../lib/format'
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
    onDatasetChange: vi.fn(),
    onTrackChange: vi.fn(),
    onFrameChange: vi.fn(),
    onLoadMoreFrames: vi.fn(),
    onOpenSource: vi.fn(),
    ...overrides,
  }

  return { ...render(<DatasetPanel {...props} />), props }
}

afterEach(cleanup)

describe('DatasetPanel data explorer', () => {
  it('does not render execution range controls or the retired frame list', () => {
    renderPanel()

    expect(screen.queryByRole('spinbutton', { name: '실행 시작 프레임 번호' })).not.toBeInTheDocument()
    expect(screen.queryByText('실행 프레임 범위')).not.toBeInTheDocument()
    expect(screen.queryByText('frame-11')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /프레임 컴포넌트/ })).not.toBeInTheDocument()
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
    const restoreButton = screen.getByRole('button', { name: '작업 데이터 패널 복원' })
    expect(restoreButton).toBeInTheDocument()
    fireEvent.click(restoreButton.querySelector('svg') as SVGElement)
    expect(onToggleCollapsed).toHaveBeenCalledTimes(2)
    expect(screen.getByText('DATA')).toBeInTheDocument()
    fireEvent.click(screen.getByText('DATA'))
    expect(onToggleCollapsed).toHaveBeenCalledTimes(3)
    expect(screen.queryByRole('combobox', { name: '데이터셋 선택' })).not.toBeInTheDocument()
  })

  it('opens the custom dataset list when either row icon is clicked', () => {
    const { container } = renderPanel()
    const trigger = screen.getByRole('combobox', { name: '데이터셋 선택' })
    const icons = container.querySelectorAll('.dataset-select svg')

    expect(icons).toHaveLength(2)
    fireEvent.click(icons[0] as SVGElement)
    expect(trigger).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByRole('listbox', { name: '작업 데이터 목록' })).toBeInTheDocument()

    fireEvent.click(trigger)
    expect(trigger).toHaveAttribute('aria-expanded', 'false')

    fireEvent.click(icons[1] as SVGElement)
    expect(trigger).toHaveAttribute('aria-expanded', 'true')
  })

  it('shows useful metadata and changes datasets from the custom list', () => {
    const onDatasetChange = vi.fn()
    const secondDataset: DatasetSummary = {
      ...DATASET,
      id: 'dataset-2',
      name: 'Second delivery',
      status: 'indexing',
      frame_count: 240,
      point_count: 125_000,
    }
    renderPanel({ datasets: [DATASET, secondDataset], onDatasetChange })

    fireEvent.click(screen.getByRole('combobox', { name: '데이터셋 선택' }))
    const option = screen.getByRole('option', { name: /Second delivery/ })
    expect(option).toHaveTextContent(`240 프레임 · ${formatCount(125_000)} pts`)
    expect(option).toHaveTextContent('인덱싱 중')
    fireEvent.click(option)

    expect(onDatasetChange).toHaveBeenCalledWith('dataset-2')
    expect(screen.getByRole('combobox', { name: '데이터셋 선택' })).toHaveAttribute(
      'aria-expanded',
      'false',
    )
  })

  it('naturally sorts track rows by their displayed names', () => {
    renderPanel({
      selectedDataset: {
        ...DATASET,
        tracks: [
          { id: 'sec-10', name: 'SEC_10', frame_count: 1 },
          { id: 'sec-2', name: 'SEC_02', frame_count: 1 },
          { id: 'sec-5', name: 'SEC_05', frame_count: 1 },
          { id: 'sec-1', name: 'SEC_01', frame_count: 1 },
        ],
      },
    })

    expect(
      Array.from(document.querySelectorAll('.track-row strong')).map((node) => node.textContent),
    ).toEqual(['전체 구간', 'SEC_01', 'SEC_02', 'SEC_05', 'SEC_10'])
  })
})
