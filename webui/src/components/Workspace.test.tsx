import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useState, type ComponentProps } from 'react'
import type { DatasetSummary, Frame } from '../types'
import { api } from '../lib/api'
import { OPEN_POINT_CLOUD_EVENT, Workspace } from './Workspace'

type MockMapMode = '2d' | 'satellite' | '3d'

vi.mock('../views/MapView', () => ({
  MapView: ({
    mapMode,
    activeTrackId,
    visibleTrackIds,
    surveySegments,
    surveyDraft,
    surveyDraftPreview,
    surveyDrawing,
    onMapModeChange,
    onSelectTrack,
    onAddSurveyPoint,
    onPreviewSurveyPoint,
  }: {
    mapMode: MockMapMode
    activeTrackId?: string
    visibleTrackIds?: ReadonlySet<string>
    surveySegments?: unknown[]
    surveyDraft?: [number, number][]
    surveyDraftPreview?: [number, number] | null
    surveyDrawing?: boolean
    onMapModeChange?: (mode: MockMapMode) => void
    onSelectTrack?: (trackId: string) => void
    onAddSurveyPoint?: (coordinate: [number, number]) => void
    onPreviewSurveyPoint?: (coordinate: [number, number] | null) => void
  }) => {
    const instance = useState(() => crypto.randomUUID())[0]
    return (
      <div
        data-testid="map-view"
        data-instance={instance}
        data-mode={mapMode}
        data-active-track={activeTrackId ?? ''}
        data-track-layer-visible={String(visibleTrackIds?.has('track-1') ?? true)}
        data-visible-tracks={visibleTrackIds ? [...visibleTrackIds].join(',') : ''}
        data-survey-count={surveySegments?.length ?? 0}
        data-survey-draft-count={surveyDraft?.length ?? 0}
        data-survey-preview={surveyDraftPreview ? surveyDraftPreview.join(',') : 'none'}
        data-survey-drawing={String(Boolean(surveyDrawing))}
      >
        <div role="group" aria-label="지도 모드 선택">
          <button type="button" onClick={() => onMapModeChange?.('2d')}>2D</button>
          <button type="button" onClick={() => onMapModeChange?.('satellite')}>위성지도</button>
          <button type="button" onClick={() => onMapModeChange?.('3d')}>3D</button>
        </div>
        <button type="button" onClick={() => onSelectTrack?.('track-1')}>
          Track 01 경로 선택
        </button>
        <button
          type="button"
          aria-label="현장조사 점 추가"
          onClick={() => onAddSurveyPoint?.([127 + (surveyDraft?.length ?? 0) * 0.001, 37])}
        >
          mock survey point
        </button>
        <button
          type="button"
          aria-label="현장조사 선 미리보기"
          onClick={() => onPreviewSurveyPoint?.([127.002, 37.002])}
        >
          mock survey preview
        </button>
        <button
          type="button"
          aria-label="현장조사 지도 나가기"
          onClick={() => onPreviewSurveyPoint?.(null)}
        >
          mock survey leave
        </button>
      </div>
    )
  },
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

const MULTI_TRACK_DATASET: DatasetSummary = {
  ...DATASET,
  id: 'dataset-many-tracks',
  tracks: [
    { id: 'sec-10', name: 'SEC_10', frame_count: 10 },
    { id: 'sec-2', name: 'SEC_02', frame_count: 2 },
    { id: 'sec-5', name: 'SEC_05', frame_count: 5 },
    { id: 'sec-1', name: 'SEC_01', frame_count: 1 },
  ],
}

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

beforeEach(() => {
  vi.spyOn(api, 'surveySegments').mockResolvedValue({ items: [] })
})

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

function MutationFocusSurface() {
  const [featurePresent, setFeaturePresent] = useState(false)
  return (
    <div
      data-testid="mutation-focus-surface"
      tabIndex={0}
      // Map SDKs handle keys on their focused canvas and may stop bubbling.
      onKeyDown={(event) => event.stopPropagation()}
    >
      <button type="button" onClick={() => setFeaturePresent((value) => !value)}>
        {featurePresent ? 'delete test feature' : 'create test feature'}
      </button>
    </div>
  )
}

describe('Workspace popup viewers', () => {
  it('draws, saves, toggles, and deletes a persisted field-survey line', async () => {
    const segment = {
      id: 'survey-1',
      dataset_id: DATASET.id,
      name: '현장조사 필요구간 1',
      color: '#f59e0b',
      geometry: { type: 'LineString' as const, coordinates: [[127, 37], [127.001, 37]] as [number, number][] },
      created_at: '2026-08-14T00:00:00Z',
      updated_at: '2026-08-14T00:00:00Z',
    }
    const create = vi.spyOn(api, 'createSurveySegment').mockResolvedValue({ segment })
    const remove = vi.spyOn(api, 'deleteSurveySegment').mockResolvedValue({
      id: segment.id,
      deleted: true,
    })
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderWorkspace()
    const map = await screen.findByTestId('map-view')

    fireEvent.click(screen.getByRole('button', { name: '구간 그리기' }))
    expect(map).toHaveAttribute('data-survey-drawing', 'true')
    expect(screen.getByText(/시작점과 끝점을 포함해 2개 이상 지점을 클릭/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '현장조사 점 추가' }))
    fireEvent.click(screen.getByRole('button', { name: '현장조사 선 미리보기' }))
    expect(map).toHaveAttribute('data-survey-preview', '127.002,37.002')
    fireEvent.click(screen.getByRole('button', { name: '현장조사 점 추가' }))
    expect(map).toHaveAttribute('data-survey-draft-count', '2')
    expect(map).toHaveAttribute('data-survey-preview', 'none')
    fireEvent.click(screen.getByRole('button', { name: '저장' }))

    await waitFor(() => expect(create).toHaveBeenCalledOnce())
    expect(map).toHaveAttribute('data-survey-count', '1')
    expect(map).toHaveAttribute('data-survey-preview', 'none')
    fireEvent.click(screen.getByTitle('현장조사 필요구간 1 숨기기'))
    expect(map).toHaveAttribute('data-survey-count', '0')
    fireEvent.click(screen.getByRole('button', { name: '현장조사 필요구간 1 삭제' }))
    await waitFor(() => expect(remove).toHaveBeenCalledWith(
      DATASET.id,
      segment.id,
      expect.any(AbortSignal),
    ))
    expect(screen.queryByText('현장조사 필요구간 1')).not.toBeInTheDocument()
  })

  it('clears the survey preview when the pointer leaves or drawing is cancelled', async () => {
    renderWorkspace()
    const map = await screen.findByTestId('map-view')

    fireEvent.click(screen.getByRole('button', { name: '구간 그리기' }))
    fireEvent.click(screen.getByRole('button', { name: '현장조사 점 추가' }))
    fireEvent.click(screen.getByRole('button', { name: '현장조사 선 미리보기' }))
    expect(map).toHaveAttribute('data-survey-preview', '127.002,37.002')

    fireEvent.click(screen.getByRole('button', { name: '현장조사 지도 나가기' }))
    expect(map).toHaveAttribute('data-survey-preview', 'none')
    fireEvent.click(screen.getByRole('button', { name: '현장조사 선 미리보기' }))
    fireEvent.click(screen.getByRole('button', { name: '그리기 취소' }))
    expect(map).toHaveAttribute('data-survey-preview', 'none')
    expect(map).toHaveAttribute('data-survey-drawing', 'false')
  })

  it('toggles the independent track layer and collapses the layer card to one line', async () => {
    renderWorkspace()
    const map = await screen.findByTestId('map-view')
    const panel = screen.getByRole('region', { name: '지도 레이어 표시 설정' })

    expect(map).toHaveAttribute('data-track-layer-visible', 'true')
    expect(panel.querySelector('#map-layer-quick-list')).not.toBeNull()
    expect(screen.getByRole('button', { name: '전체 트랙 모두 표시' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )

    fireEvent.click(screen.getByTitle('Track 01 트랙 이 트랙만 표시'))
    expect(map).toHaveAttribute('data-track-layer-visible', 'true')
    expect(screen.getByRole('button', { name: '전체 트랙 모두 표시' })).toHaveAttribute(
      'aria-pressed',
      'false',
    )

    fireEvent.click(screen.getByTitle('Track 01 트랙 숨기기'))
    expect(map).toHaveAttribute('data-track-layer-visible', 'false')
    expect(screen.getByTitle('Track 01 트랙 추가 표시')).toHaveAttribute('aria-pressed', 'false')

    fireEvent.click(screen.getByRole('button', { name: '지도 레이어 카드 최소화' }))
    expect(panel).toHaveClass('collapsed')
    expect(panel.querySelector('#map-layer-quick-list')).toBeNull()
    expect(screen.getByRole('button', { name: '지도 레이어 카드 펼치기' })).toHaveAttribute(
      'aria-expanded',
      'false',
    )
  })

  it('supports exclusive, additive, and all-track visibility and resets for another dataset', async () => {
    const view = renderWorkspace({ dataset: MULTI_TRACK_DATASET, selectedTrack: 'sec-2' })
    const map = await screen.findByTestId('map-view')
    const trackButtons = screen.getAllByTitle(/SEC_\d+ 작업 구간 선택/)

    expect(trackButtons.map((button) => button.querySelector('span')?.textContent)).toEqual([
      'SEC_01',
      'SEC_02',
      'SEC_05',
      'SEC_10',
    ])
    expect(map).toHaveAttribute('data-visible-tracks', 'sec-1,sec-2,sec-5,sec-10')

    fireEvent.click(screen.getByTitle('SEC_02 트랙 이 트랙만 표시'))
    expect(map).toHaveAttribute('data-visible-tracks', 'sec-2')
    expect(screen.getByTitle('SEC_02 트랙 숨기기')).toHaveAttribute(
      'aria-pressed',
      'true',
    )

    fireEvent.click(screen.getByTitle('SEC_05 트랙 추가 표시'))
    expect(map).toHaveAttribute('data-visible-tracks', 'sec-2,sec-5')

    fireEvent.click(screen.getByTitle('SEC_02 트랙 숨기기'))
    expect(map).toHaveAttribute('data-visible-tracks', 'sec-5')
    expect(screen.getByTitle('SEC_02 트랙 추가 표시')).toHaveAttribute(
      'aria-pressed',
      'false',
    )

    fireEvent.click(screen.getByRole('button', { name: '전체 트랙 모두 표시' }))
    expect(map).toHaveAttribute('data-visible-tracks', 'sec-1,sec-2,sec-5,sec-10')
    expect(screen.getByRole('button', { name: '전체 트랙 모두 표시' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )

    view.rerender(<Workspace {...view.props} dataset={DATASET} selectedTrack="track-1" />)
    expect(screen.getByTestId('map-view')).toHaveAttribute('data-visible-tracks', 'track-1')
  })

  it('forwards the selected work track to the map and a map route click back to the parent', async () => {
    const onTrackChange = vi.fn()
    renderWorkspace({ selectedTrack: 'track-1', onTrackChange })

    const map = await screen.findByTestId('map-view')
    expect(map).toHaveAttribute('data-active-track', 'track-1')

    fireEvent.click(screen.getByTitle('Track 01 작업 구간 선택'))
    expect(onTrackChange).toHaveBeenCalledWith('track-1')
    onTrackChange.mockClear()

    fireEvent.click(screen.getByRole('button', { name: 'Track 01 경로 선택' }))
    expect(onTrackChange).toHaveBeenCalledOnce()
    expect(onTrackChange).toHaveBeenCalledWith('track-1')
  })

  it('exposes the attribute table next to 3D points and delegates popup lifecycle', async () => {
    const onToggleAttributeTable = vi.fn()
    renderWorkspace({ onToggleAttributeTable })
    await screen.findByTestId('map-view')

    const button = screen.getByRole('button', { name: '속성표' })
    expect(button).toHaveAttribute('aria-pressed', 'false')
    fireEvent.click(button)
    expect(onToggleAttributeTable).toHaveBeenCalledOnce()
  })

  it('remounts the map when the workspace crosses a document boundary', async () => {
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
      detached: false,
      onTogglePanorama: vi.fn(),
      onTogglePointCloud: vi.fn(),
      onFrameChange: vi.fn(),
      onMoveFrame: vi.fn(),
      onOpenSource: vi.fn(),
      onUseDemo: vi.fn(),
    }
    const view = render(<Workspace {...props} />)
    const mainInstance = (await screen.findByTestId('map-view')).dataset.instance
    fireEvent.click(screen.getByRole('button', { name: '위성지도' }))
    expect(screen.getByTestId('map-view')).toHaveAttribute('data-mode', 'satellite')

    view.rerender(<Workspace {...props} detached />)
    const popupInstance = screen.getByTestId('map-view').dataset.instance
    expect(screen.getByTestId('map-view')).toHaveAttribute('data-mode', 'satellite')

    view.rerender(<Workspace {...props} detached={false} />)
    const returnedInstance = screen.getByTestId('map-view').dataset.instance
    expect(screen.getByTestId('map-view')).toHaveAttribute('data-mode', 'satellite')

    expect(popupInstance).not.toBe(mainInstance)
    expect(returnedInstance).not.toBe(popupInstance)

    fireEvent.click(screen.getByRole('button', { name: '3D' }))
    expect(screen.getByTestId('map-view')).toHaveAttribute('data-mode', '3d')
  })

  it('opens panorama and 3D points only in independent windows', async () => {
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
    expect(screen.queryByTestId('panorama-view')).not.toBeInTheDocument()
    expect(screen.queryByTestId('point-cloud-view')).not.toBeInTheDocument()

    panoramaPopup.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'ArrowRight', code: 'ArrowRight', cancelable: true }),
    )
    expect(onMoveFrame).toHaveBeenCalledWith(1)
  })

  it('opens or focuses the 3D popup when a point-cloud tool requests it', async () => {
    const pointPopup = fakePopup()
    const open = vi.spyOn(window, 'open').mockReturnValue(pointPopup)
    render(<ControlledWorkspace />)
    await screen.findByTestId('map-view')

    fireEvent(window, new CustomEvent(OPEN_POINT_CLOUD_EVENT))

    expect(open).toHaveBeenCalledOnce()
    await waitFor(() => {
      expect(pointPopup.document.querySelector('[data-testid="point-cloud-view"]')).not.toBeNull()
    })

    fireEvent(window, new CustomEvent(OPEN_POINT_CLOUD_EVENT))
    expect(open).toHaveBeenCalledOnce()
    expect(pointPopup.focus).toHaveBeenCalledTimes(2)
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

  it('does not move frames behind an open modal dialog', async () => {
    const onMoveFrame = vi.fn()
    renderWorkspace({ onMoveFrame })
    await screen.findByTestId('map-view')
    render(<section role="dialog" aria-modal="true" aria-label="도움말" />)

    fireEvent.keyDown(window, { key: 'ArrowLeft', code: 'ArrowLeft' })
    fireEvent.keyDown(window, { key: 'd', code: 'KeyD' })

    expect(onMoveFrame).not.toHaveBeenCalled()
  })

  it('keeps A/D and arrow navigation live on a focused view after feature create and delete', async () => {
    const onMoveFrame = vi.fn()
    renderWorkspace({
      onMoveFrame,
      externalAction: <MutationFocusSurface />,
    })
    await screen.findByTestId('map-view')
    const focusSurface = screen.getByTestId('mutation-focus-surface')

    fireEvent.click(screen.getByRole('button', { name: 'create test feature' }))
    fireEvent.keyDown(focusSurface, { key: 'ArrowLeft', code: 'ArrowLeft' })
    fireEvent.keyDown(focusSurface, { key: 'd', code: 'KeyD' })

    fireEvent.click(screen.getByRole('button', { name: 'delete test feature' }))
    fireEvent.keyDown(focusSurface, { key: 'a', code: 'KeyA' })
    fireEvent.keyDown(focusSurface, { key: 'ArrowRight', code: 'ArrowRight' })

    expect(onMoveFrame.mock.calls).toEqual([[-1], [1], [-1], [1]])
  })
})
