import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useState, type ComponentProps } from 'react'
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
    panoramaOpen: false,
    pointCloudOpen: false,
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

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

function fakePopup() {
  const events = new EventTarget()
  const popupDocument = document.implementation.createHTMLDocument('')
  const popup = {
    document: popupDocument,
    closed: false,
    KeyboardEvent: window.KeyboardEvent,
    addEventListener: events.addEventListener.bind(events),
    removeEventListener: events.removeEventListener.bind(events),
    dispatchEvent: events.dispatchEvent.bind(events),
    focus: vi.fn(),
    close: vi.fn(),
  }
  return popup as unknown as Window
}

function ControlledWorkspace({ onMoveFrame = vi.fn() }: { onMoveFrame?: (direction: -1 | 1) => void }) {
  const [panoramaOpen, setPanoramaOpen] = useState(false)
  const [pointCloudOpen, setPointCloudOpen] = useState(false)
  return (
    <Workspace
      dataset={DATASET}
      frames={FRAMES}
      frame={FRAMES[1]}
      frameRange={null}
      route={[]}
      routeLoading={false}
      demoMode={false}
      panoramaOpen={panoramaOpen}
      pointCloudOpen={pointCloudOpen}
      hasMoreFrames={false}
      inspectorOpen
      onTogglePanorama={() => setPanoramaOpen((value) => !value)}
      onTogglePointCloud={() => setPointCloudOpen((value) => !value)}
      onFrameChange={vi.fn()}
      onMoveFrame={onMoveFrame}
      onToggleInspector={vi.fn()}
      onOpenSource={vi.fn()}
      onUseDemo={vi.fn()}
    />
  )
}

describe('Workspace popup viewers', () => {
  it('opens panorama and 3D points directly in independent windows', async () => {
    const panoramaPopup = fakePopup()
    const pointPopup = fakePopup()
    const open = vi
      .spyOn(window, 'open')
      .mockReturnValueOnce(panoramaPopup)
      .mockReturnValueOnce(pointPopup)
    const onMoveFrame = vi.fn()
    render(<ControlledWorkspace onMoveFrame={onMoveFrame} />)

    expect(await screen.findByTestId('map-view')).toBeInTheDocument()
    expect(screen.queryByTestId('panorama-view')).not.toBeInTheDocument()
    expect(screen.queryByTestId('point-cloud-view')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '파노라마' }))
    fireEvent.click(screen.getByRole('button', { name: '3D 포인트' }))

    expect(open).toHaveBeenCalledTimes(2)
    await waitFor(() => {
      expect(panoramaPopup.document.querySelector('[data-testid="panorama-view"]')).not.toBeNull()
      expect(pointPopup.document.querySelector('[data-testid="point-cloud-view"]')).not.toBeNull()
    })
    expect(document.querySelector('[aria-label="파노라마 팝업"]')).toBeNull()
    expect(document.querySelector('[aria-label="3D 포인트 팝업"]')).toBeNull()

    panoramaPopup.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'ArrowRight', code: 'ArrowRight', cancelable: true }),
    )
    expect(onMoveFrame).toHaveBeenCalledWith(1)
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

  it('does not consume navigation keys from editable controls', async () => {
    const onMoveFrame = vi.fn()
    renderWorkspace({ onMoveFrame })
    await screen.findByTestId('map-view')
    const input = document.createElement('input')
    document.body.appendChild(input)

    fireEvent.keyDown(input, { key: 'ArrowLeft', code: 'ArrowLeft' })
    fireEvent.keyDown(input, { key: 'd', code: 'KeyD' })

    expect(onMoveFrame).not.toHaveBeenCalled()
    input.remove()
  })
})
