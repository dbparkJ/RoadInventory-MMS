import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { OverlayFeature, OverlayFeatureCollection, OverlayLayer } from '../types'
import { OverlayPanel } from './OverlayPanel'

const useOverlayWorkspace = vi.hoisted(() => vi.fn())

vi.mock('./OverlayContext', () => ({ useOverlayWorkspace }))

const LAYER: OverlayLayer = {
  id: 'layer-1',
  dataset_id: 'dataset-1',
  name: '검출 지주',
  geometry_type: 'Point',
  feature_count: 2,
  revision: 3,
}

function collection(features: OverlayFeature[], total = features.length): OverlayFeatureCollection {
  return {
    type: 'FeatureCollection',
    features,
    fields: [{ name: 'status', type: 'C' }, { name: 'score', type: 'N' }],
    total,
    offset: 0,
    limit: 3_000,
    revision: LAYER.revision,
    next_offset: features.length < total ? features.length : null,
  }
}

function pointFeature(): OverlayFeature {
  return {
    type: 'Feature',
    id: 'point-1',
    geometry: { type: 'Point', coordinates: [1, 2] },
    properties: { status: '검수 전', score: 0.75 },
  }
}

function lineFeature(): OverlayFeature {
  return {
    type: 'Feature',
    id: 'line-1',
    geometry: { type: 'LineString', coordinates: [[1, 2], [3, 4]] },
    properties: { status: '검수 전', score: 0.5 },
  }
}

function overlayWorkspace(selectedFeature: OverlayFeature | null, total?: number) {
  const dataset = collection(selectedFeature ? [selectedFeature] : [], total)
  return {
    datasetId: 'dataset-1',
    layers: [LAYER],
    features: {
      [LAYER.id]: {
        wgs84: null,
        dataset,
        loading: false,
        loadingDataset: false,
      },
    },
    visibleLayerIds: new Set([LAYER.id]),
    selected: selectedFeature ? { layerId: LAYER.id, featureId: selectedFeature.id } : null,
    selectedLayer: selectedFeature ? LAYER : null,
    selectedFeature: null,
    selectedDatasetFeature: selectedFeature,
    mapFeatures: [],
    datasetFeatures: [],
    loading: false,
    uploading: false,
    pickMode: false,
    setPickMode: vi.fn(),
    refresh: vi.fn().mockResolvedValue(undefined),
    ensureDatasetFeatures: vi.fn().mockResolvedValue(undefined),
    loadMoreDatasetFeatures: vi.fn().mockResolvedValue(undefined),
    upload: vi.fn().mockResolvedValue(undefined),
    removeLayer: vi.fn().mockResolvedValue(undefined),
    toggleLayer: vi.fn(),
    selectFeature: vi.fn(),
    updateSelected: vi.fn().mockResolvedValue(undefined),
    applyPickedCoordinate: vi.fn().mockResolvedValue(undefined),
    deleteSelected: vi.fn().mockResolvedValue(undefined),
    layerColor: vi.fn(() => '#2bcfa8'),
  }
}

beforeEach(() => {
  useOverlayWorkspace.mockReset()
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('OverlayPanel feature editing', () => {
  it('saves a 2D Point as [x, y] without a null Z coordinate', async () => {
    const overlay = overlayWorkspace(pointFeature())
    useOverlayWorkspace.mockReturnValue(overlay)
    render(<OverlayPanel onClose={vi.fn()} />)

    fireEvent.change(screen.getByLabelText('X'), { target: { value: '11.5' } })
    fireEvent.change(screen.getByLabelText('Y'), { target: { value: '22.75' } })
    expect(screen.getByLabelText('Z')).toHaveValue('')
    fireEvent.click(screen.getByRole('button', { name: /변경 저장/ }))

    await waitFor(() => expect(overlay.updateSelected).toHaveBeenCalledTimes(1))
    expect(overlay.updateSelected).toHaveBeenCalledWith({
      geometry: { type: 'Point', coordinates: [11.5, 22.75] },
      coordinate_space: 'dataset',
      properties: { status: '검수 전', score: 0.75 },
    })
    const coordinates = overlay.updateSelected.mock.calls[0][0].geometry.coordinates
    expect(coordinates).toHaveLength(2)
    expect(coordinates).not.toContain(null)
  })

  it('saves attributes only for a non-Point feature', async () => {
    const overlay = overlayWorkspace(lineFeature())
    useOverlayWorkspace.mockReturnValue(overlay)
    render(<OverlayPanel onClose={vi.fn()} />)

    expect(screen.getByLabelText('X')).toBeDisabled()
    fireEvent.change(screen.getByLabelText('status'), { target: { value: '확인 완료' } })
    fireEvent.click(screen.getByRole('button', { name: /변경 저장/ }))

    await waitFor(() => expect(overlay.updateSelected).toHaveBeenCalledTimes(1))
    expect(overlay.updateSelected).toHaveBeenCalledWith({
      properties: { status: '확인 완료', score: 0.5 },
    })
    expect(overlay.updateSelected.mock.calls[0][0]).not.toHaveProperty('geometry')
    expect(overlay.updateSelected.mock.calls[0][0]).not.toHaveProperty('coordinate_space')
  })

  it('lazily ensures the active layer and connects the load-more action', async () => {
    const overlay = overlayWorkspace(pointFeature(), 6_001)
    useOverlayWorkspace.mockReturnValue(overlay)
    render(<OverlayPanel onClose={vi.fn()} />)

    await waitFor(() => expect(overlay.ensureDatasetFeatures).toHaveBeenCalledWith(LAYER.id))
    fireEvent.click(screen.getByRole('button', { name: /다음 피처 불러오기/ }))
    await waitFor(() => expect(overlay.loadMoreDatasetFeatures).toHaveBeenCalledWith(LAYER.id))
  })

  it('shows a local actionable error when feature deletion fails', async () => {
    const overlay = overlayWorkspace(pointFeature())
    overlay.deleteSelected.mockRejectedValue(new Error('서버에서 피처 삭제를 거부했습니다.'))
    useOverlayWorkspace.mockReturnValue(overlay)
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(<OverlayPanel onClose={vi.fn()} />)

    fireEvent.click(screen.getByRole('button', { name: '삭제' }))

    expect(await screen.findByText('서버에서 피처 삭제를 거부했습니다.')).toBeInTheDocument()
    expect(overlay.deleteSelected).toHaveBeenCalledOnce()
  })
})
