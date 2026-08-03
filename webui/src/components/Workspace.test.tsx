import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { ComponentProps } from 'react'
import type { DatasetSummary, Frame } from '../types'
import { Workspace } from './Workspace'

vi.mock('../views/MapView', () => ({
  MapView: () => <div data-testid="map-view" />,
}))

vi.mock('../views/PanoramaView', () => ({
  default: () => <div data-testid="panorama-view" />,
}))

vi.mock('../views/PointCloudView', () => ({
  default: () => <div data-testid="point-cloud-view" />,
}))

const DATASET: DatasetSummary = {
  id: 'dataset-1',
  name: 'Test delivery',
  status: 'ready',
  frame_count: 3,
  tracks: [
    {
      id: 'track-1',
      name: 'Track 01',
      frame_count: 3,
    },
  ],
}

const FRAMES: Frame[] = [0, 1, 2].map((index) => ({
  id: `frame-${index + 1}`,
  index,
  track_id: 'track-1',
  timestamp: `2026-08-03T09:30:0${index}.000Z`,
  coordinate: { lon: 126.978 + index * 0.001, lat: 37.5665 + index * 0.001 },
  has_panorama: true,
  has_points: true,
}))

function renderWorkspace(overrides: Partial<ComponentProps<typeof Workspace>> = {}) {
  const props: ComponentProps<typeof Workspace> = {
    dataset: DATASET,
    frames: FRAMES,
    frame: FRAMES[1],
    frameRange: null,
    route: [],
    routeLoading: false,
    demoMode: false,
    panoramaOpen: true,
    pointCloudOpen: true,
    hasMoreFrames: false,
    inspectorOpen: true,
    onTogglePanorama: vi.fn(),
    onTogglePointCloud: vi.fn(),
    onFrameChange: vi.fn(),
    onMoveFrame: vi.fn(),
    onToggleInspector: vi.fn(),
    onOpenSource: vi.fn(),
    onUseDemo: vi.fn(),
    ...overrides,
  }

  return { ...render(<Workspace {...props} />), props }
}

afterEach(cleanup)

describe('Workspace layered viewers', () => {
  it('renders panorama and 3D point data together over the map', async () => {
    renderWorkspace()

    expect(await screen.findByTestId('map-view')).toBeInTheDocument()
    expect(await screen.findByTestId('panorama-view')).toBeInTheDocument()
    expect(await screen.findByTestId('point-cloud-view')).toBeInTheDocument()
    expect(screen.getByRole('region', { name: '파노라마 오버레이' })).toBeInTheDocument()
    expect(screen.getByRole('region', { name: '3D 포인트 오버레이' })).toBeInTheDocument()
  })
})

describe('Workspace frame shortcuts', () => {
  it('moves with ArrowLeft, A, ArrowRight, and D', async () => {
    const onMoveFrame = vi.fn()
    renderWorkspace({ onMoveFrame })
    await screen.findByTestId('map-view')

    fireEvent.keyDown(window, { key: 'ArrowLeft', code: 'ArrowLeft' })
    fireEvent.keyDown(window, { key: 'a', code: 'KeyA' })
    fireEvent.keyDown(window, { key: 'ArrowRight', code: 'ArrowRight' })
    fireEvent.keyDown(window, { key: 'd', code: 'KeyD' })

    expect(onMoveFrame.mock.calls).toEqual([[-1], [-1], [1], [1]])
  })
})
