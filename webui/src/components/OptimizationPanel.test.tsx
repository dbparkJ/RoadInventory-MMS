import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError } from '../lib/api'
import type { DatasetSummary, Frame, RunRequest } from '../types'
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

const MODEL_RESPONSE = {
  items: [
    { id: 'traffic_sign.pt', name: 'traffic_sign.pt', label: 'traffic_sign' },
    { id: 'traffic_light.pt', name: 'traffic_light.pt', label: 'traffic_light' },
  ],
  default_model_ids: ['traffic_sign.pt', 'traffic_light.pt'],
}

beforeEach(() => {
  vi.spyOn(api, 'detectionModels').mockResolvedValue(MODEL_RESPONSE)
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

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

    await waitFor(() => expect(screen.getByRole('button', { name: '작업 시작' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: '작업 시작' }))

    await waitFor(() => expect(onStart).toHaveBeenCalledTimes(1))
    expect(onStart).toHaveBeenCalledWith(
      expect.objectContaining({
        dataset_id: 'dataset-1',
        track_ids: ['track-1'],
        frame_range: [12, 48],
        mode: 'automatic',
        run_name: 'Test delivery 검출레이어',
        layer_name: 'Test delivery 검출레이어',
        model_names: ['traffic_sign.pt', 'traffic_light.pt'],
      }),
    )
    expect(screen.getAllByText('ordinal 12–48')).toHaveLength(2)
  })

  it('requires at least one selected model and submits the chosen subset and layer name', async () => {
    const onStart = vi.fn(async () => undefined)
    render(
      <OptimizationPanel
        dataset={READY_DATASET}
        selectedTrack="track-1"
        selectedFrame={FRAME}
        frameRange={null}
        busy={false}
        onStart={onStart}
        onOptimize={vi.fn(async () => undefined)}
        onSetFrameRangeStart={vi.fn()}
        onSetFrameRangeEnd={vi.fn()}
        onFrameRangeChange={vi.fn()}
        onClearFrameRange={vi.fn()}
      />,
    )

    const signModel = await screen.findByRole('checkbox', { name: /traffic_sign/ })
    const lightModel = screen.getByRole('checkbox', { name: /traffic_light/ })
    fireEvent.click(lightModel)
    fireEvent.change(screen.getByRole('textbox', { name: '검출 레이어 이름' }), {
      target: { value: '2026 교통표지 검출' },
    })
    fireEvent.click(screen.getByRole('button', { name: '작업 시작' }))

    await waitFor(() => expect(onStart).toHaveBeenCalledTimes(1))
    expect(onStart).toHaveBeenCalledWith(expect.objectContaining({
      run_name: '2026 교통표지 검출',
      layer_name: '2026 교통표지 검출',
      model_names: ['traffic_sign.pt'],
    }))

    fireEvent.click(signModel)
    expect(screen.getByText('실행할 모델을 한 개 이상 선택해 주세요.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '작업 시작' })).toBeDisabled()
  })

  it('does not submit automatic detection when the server reports no available models', async () => {
    vi.mocked(api.detectionModels).mockResolvedValueOnce({
      items: [],
      default_model_ids: [],
    })

    render(
      <OptimizationPanel
        dataset={READY_DATASET}
        selectedTrack="track-1"
        selectedFrame={FRAME}
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

    expect(await screen.findByText('사용 가능한 모델이 없습니다.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '작업 시작' })).toBeDisabled()
  })

  it('requires and submits the same layer and model selection in manual mode', async () => {
    const onStart = vi.fn(async (_request: RunRequest) => undefined)
    render(
      <OptimizationPanel
        dataset={READY_DATASET}
        selectedTrack="track-1"
        selectedFrame={FRAME}
        frameRange={null}
        busy={false}
        onStart={onStart}
        onOptimize={vi.fn(async () => undefined)}
        onSetFrameRangeStart={vi.fn()}
        onSetFrameRangeEnd={vi.fn()}
        onFrameRangeChange={vi.fn()}
        onClearFrameRange={vi.fn()}
      />,
    )

    const lightModel = await screen.findByRole('checkbox', { name: /traffic_light/ })
    fireEvent.click(lightModel)
    fireEvent.click(screen.getAllByRole('radio')[1])
    fireEvent.change(screen.getByRole('textbox', { name: '검출 레이어 이름' }), {
      target: { value: '' },
    })
    expect(screen.getByRole('button', { name: '작업 시작' })).toBeDisabled()
    fireEvent.change(screen.getByRole('textbox', { name: '검출 레이어 이름' }), {
      target: { value: '수동 파라미터 검출' },
    })
    fireEvent.click(screen.getByRole('button', { name: '작업 시작' }))

    await waitFor(() => expect(onStart).toHaveBeenCalledTimes(1))
    expect(onStart).toHaveBeenCalledWith(expect.objectContaining({
      mode: 'manual',
      run_name: '수동 파라미터 검출',
      layer_name: '수동 파라미터 검출',
      model_names: ['traffic_sign.pt'],
    }))
  })

  it('uses legacy all-model behavior only when the catalog endpoint is absent', async () => {
    vi.mocked(api.detectionModels).mockRejectedValueOnce(new ApiError('Not Found', 404))
    const onStart = vi.fn(async (_request: RunRequest) => undefined)
    render(
      <OptimizationPanel
        dataset={READY_DATASET}
        selectedTrack="track-1"
        selectedFrame={FRAME}
        frameRange={null}
        busy={false}
        onStart={onStart}
        onOptimize={vi.fn(async () => undefined)}
        onSetFrameRangeStart={vi.fn()}
        onSetFrameRangeEnd={vi.fn()}
        onFrameRangeChange={vi.fn()}
        onClearFrameRange={vi.fn()}
      />,
    )

    expect(await screen.findByText(/구버전 서버에서는 모델 목록을 제공하지 않아/))
      .toBeInTheDocument()
    const start = screen.getByRole('button', { name: '작업 시작' })
    expect(start).toBeEnabled()
    fireEvent.click(start)
    await waitFor(() => expect(onStart).toHaveBeenCalledTimes(1))
    expect(onStart.mock.calls[0]?.[0]).not.toHaveProperty('model_names')
    expect(onStart.mock.calls[0]?.[0]).toHaveProperty(
      'layer_name',
      'Test delivery 검출레이어',
    )
  })

  it.each([
    ['invalid server configuration', new ApiError('No configured models', 422)],
    ['network failure', new TypeError('Failed to fetch')],
  ])('blocks submission after %s and retries the catalog request', async (_label, failure) => {
    vi.mocked(api.detectionModels)
      .mockReset()
      .mockRejectedValueOnce(failure)
      .mockResolvedValueOnce(MODEL_RESPONSE)
    render(
      <OptimizationPanel
        dataset={READY_DATASET}
        selectedTrack="track-1"
        selectedFrame={FRAME}
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

    expect(await screen.findByRole('alert')).toHaveTextContent(
      '모델 목록을 불러오지 못했습니다.',
    )
    expect(screen.getByRole('button', { name: '작업 시작' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: '다시 시도' }))

    expect(await screen.findByRole('checkbox', { name: /traffic_sign/ })).toBeChecked()
    expect(api.detectionModels).toHaveBeenCalledTimes(2)
    expect(screen.getByRole('button', { name: '작업 시작' })).toBeEnabled()
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
