import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { OverlayFeature, OverlayFeatureCollection, OverlayLayer } from '../types'
import {
  OverlayAttributePanel,
  OverlayPanel,
  overlaySupportId,
  sharesOverlayLocationOrSupport,
  sharesOverlaySupport,
} from './OverlayPanel'

const useOverlayWorkspace = vi.hoisted(() => vi.fn())

vi.mock('./OverlayContext', () => ({ useOverlayWorkspace }))

const LAYER: OverlayLayer = {
  id: 'layer-1',
  dataset_id: 'dataset-1',
  name: '검출 지주',
  geometry_type: 'Point',
  feature_count: 2,
  revision: 3,
  color: '#2bcfa8',
  metadata_revision: 2,
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
    poleBaseInferenceEnabled: true,
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
    activeLayerId: LAYER.id,
    setActiveLayerId: vi.fn(),
    selected: selectedFeature ? { layerId: LAYER.id, featureId: selectedFeature.id } : null,
    selectedLayer: selectedFeature ? LAYER : null,
    selectedFeature: null,
    selectedDatasetFeature: selectedFeature,
    mapFeatures: [],
    datasetFeatures: [],
    loading: false,
    uploading: false,
    creatingFeature: false,
    pickMode: false,
    pickTarget: null,
    poleBaseProposal: { status: 'idle' as const },
    setPickMode: vi.fn(),
    beginCreatePoint: vi.fn(),
    beginCreatePoleBase: vi.fn(),
    beginRecomputeSelectedPoleBase: vi.fn(),
    applyPoleSeed: vi.fn().mockResolvedValue(undefined),
    confirmPoleBaseProposal: vi.fn().mockResolvedValue(undefined),
    retryPoleBasePick: vi.fn(),
    cancelPoleBaseProposal: vi.fn(),
    refresh: vi.fn().mockResolvedValue(undefined),
    ensureDatasetFeatures: vi.fn().mockResolvedValue(undefined),
    loadMoreDatasetFeatures: vi.fn().mockResolvedValue(undefined),
    upload: vi.fn().mockResolvedValue(undefined),
    updateLayerMetadata: vi.fn().mockResolvedValue(undefined),
    removeLayer: vi.fn().mockResolvedValue(undefined),
    toggleLayer: vi.fn(),
    selectFeature: vi.fn(),
    updateSelected: vi.fn().mockResolvedValue(undefined),
    applyPickedCoordinate: vi.fn().mockResolvedValue(undefined),
    copySelectedLocation: vi.fn().mockResolvedValue(undefined),
    deleteSelected: vi.fn().mockResolvedValue(undefined),
    deleteField: vi.fn().mockResolvedValue(undefined),
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
  it('matches explicit support_id relationships and exact copied Point locations', () => {
    expect(overlaySupportId({ SUPPORT_ID: ' P-001 ' })).toBe('P-001')
    expect(sharesOverlaySupport({ support_id: 'P-001' }, { SUPPORT_ID: 'P-001' })).toBe(true)
    expect(sharesOverlaySupport({ support_id: 'P-001' }, { support_id: 'p-001' })).toBe(false)
    expect(sharesOverlaySupport({ support_id: '' }, { support_id: '' })).toBe(false)
    expect(sharesOverlayLocationOrSupport(
      pointFeature(),
      { ...pointFeature(), id: 'copy', properties: {} },
    )).toBe(true)
    expect(sharesOverlayLocationOrSupport(
      pointFeature(),
      { ...pointFeature(), id: 'other', geometry: { type: 'Point', coordinates: [1.01, 2] } },
    )).toBe(false)
    expect(sharesOverlaySupport({ POLE_TYPE: '2주식' }, { POLE_TYPE: '2주식' })).toBe(false)
  })

  it('highlights rows sharing the selected support_id or Point XY only while selected', () => {
    const selected = {
      ...pointFeature(),
      properties: { ...pointFeature().properties, support_id: 'P-shared' },
    }
    const sameSupport: OverlayFeature = {
      ...pointFeature(),
      id: 'point-2',
      geometry: { type: 'Point', coordinates: [9, 9] },
      properties: { ...pointFeature().properties, SUPPORT_ID: 'P-shared' },
    }
    const sameLocationWithBlankProperties: OverlayFeature = {
      ...pointFeature(),
      id: 'point-copy',
      properties: {},
    }
    const differentValueCase: OverlayFeature = {
      ...pointFeature(),
      id: 'point-3',
      geometry: { type: 'Point', coordinates: [3, 4] },
      properties: { ...pointFeature().properties, support_id: 'p-shared' },
    }
    const overlay = overlayWorkspace(selected)
    overlay.features[LAYER.id].dataset = {
      ...collection([selected, sameSupport, sameLocationWithBlankProperties, differentValueCase]),
      fields: [{ name: 'status', type: 'C' }, { name: 'support_id', type: 'C' }],
    }
    useOverlayWorkspace.mockReturnValue(overlay)

    const { container, rerender } = render(<OverlayAttributePanel onClose={vi.fn()} />)
    expect(container.querySelectorAll('tr[data-related-support="true"]')).toHaveLength(3)
    expect(container.querySelector('tr.selected')).toHaveAttribute('data-related-support', 'true')

    const withoutSelection = { ...overlay, selected: null, selectedDatasetFeature: null }
    useOverlayWorkspace.mockReturnValue(withoutSelection)
    rerender(<OverlayAttributePanel onClose={vi.fn()} />)
    expect(container.querySelectorAll('tr[data-related-support="true"]')).toHaveLength(0)
  })

  it('edits the selected layer display name and shared color', async () => {
    const overlay = overlayWorkspace(pointFeature())
    useOverlayWorkspace.mockReturnValue(overlay)
    render(<OverlayPanel onClose={vi.fn()} />)

    expect(screen.queryByRole('table')).not.toBeInTheDocument()

    fireEvent.change(screen.getByRole('textbox', { name: '선택 레이어 이름' }), {
      target: { value: '  현장 지주  ' },
    })
    fireEvent.change(screen.getByLabelText('선택 레이어 색상'), {
      target: { value: '#123456' },
    })
    fireEvent.click(screen.getByRole('button', { name: /이름·색상 저장/ }))

    await waitFor(() =>
      expect(overlay.updateLayerMetadata).toHaveBeenCalledWith(LAYER.id, {
        name: '현장 지주',
        color: '#123456',
      }),
    )
  })

  it('consumes an imported layer focus once without overriding later layer changes', async () => {
    const overlay = overlayWorkspace(pointFeature())
    useOverlayWorkspace.mockReturnValue(overlay)
    const view = render(<OverlayPanel focusLayerId={LAYER.id} onClose={vi.fn()} />)

    await waitFor(() => expect(overlay.setActiveLayerId).toHaveBeenCalledWith(LAYER.id))
    expect(overlay.setActiveLayerId).toHaveBeenCalledTimes(1)

    useOverlayWorkspace.mockReturnValue({ ...overlay, layers: [{ ...LAYER, revision: 4 }] })
    view.rerender(<OverlayPanel focusLayerId={LAYER.id} onClose={vi.fn()} />)
    expect(overlay.setActiveLayerId).toHaveBeenCalledTimes(1)
  })

  it('saves a 2D Point as [x, y] without a null Z coordinate', async () => {
    const overlay = overlayWorkspace(pointFeature())
    useOverlayWorkspace.mockReturnValue(overlay)
    render(<OverlayAttributePanel onClose={vi.fn()} />)

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
    render(<OverlayAttributePanel onClose={vi.fn()} />)

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
    render(<OverlayAttributePanel onClose={vi.fn()} />)

    await waitFor(() => expect(overlay.ensureDatasetFeatures).toHaveBeenCalledWith(LAYER.id))
    fireEvent.click(screen.getByRole('button', { name: /다음 피처 불러오기/ }))
    await waitFor(() => expect(overlay.loadMoreDatasetFeatures).toHaveBeenCalledWith(LAYER.id))
  })

  it('shows a local actionable error when feature deletion fails', async () => {
    const overlay = overlayWorkspace(pointFeature())
    overlay.deleteSelected.mockRejectedValue(new Error('서버에서 피처 삭제를 거부했습니다.'))
    useOverlayWorkspace.mockReturnValue(overlay)
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(<OverlayAttributePanel onClose={vi.fn()} />)

    fireEvent.click(screen.getByRole('button', { name: '삭제' }))

    expect(await screen.findByText('서버에서 피처 삭제를 거부했습니다.')).toBeInTheDocument()
    expect(overlay.deleteSelected).toHaveBeenCalledOnce()
  })

  it('renders every loaded row and SHP field without truncating the attribute table', () => {
    const overlay = overlayWorkspace(pointFeature())
    const fields = Array.from({ length: 8 }, (_, index) => ({ name: `field_${index + 1}`, type: 'C' }))
    const features = Array.from({ length: 501 }, (_, index): OverlayFeature => ({
      type: 'Feature',
      id: `point-${index + 1}`,
      geometry: { type: 'Point', coordinates: [index, index + 1] },
      properties: Object.fromEntries(fields.map((field) => [field.name, `${field.name}-${index}`])),
    }))
    overlay.features[LAYER.id].dataset = {
      ...collection(features),
      fields,
    }
    overlay.selected = null
    overlay.selectedLayer = null
    overlay.selectedDatasetFeature = null
    useOverlayWorkspace.mockReturnValue(overlay)

    render(<OverlayAttributePanel onClose={vi.fn()} />)

    expect(screen.getByRole('columnheader', { name: 'field_8' })).toBeInTheDocument()
    expect(screen.getByText('point-501')).toBeInTheDocument()
    expect(screen.queryByText(/첫 500행/)).not.toBeInTheDocument()
  })

  it('starts map-click creation and duplicates only the selected geometry through context actions', async () => {
    const overlay = overlayWorkspace(pointFeature())
    const onClose = vi.fn()
    useOverlayWorkspace.mockReturnValue(overlay)
    render(<OverlayAttributePanel onClose={onClose} />)

    fireEvent.click(screen.getByRole('button', { name: /신규 포인트/ }))
    expect(overlay.beginCreatePoint).toHaveBeenCalledWith(LAYER.id)
    expect(onClose).toHaveBeenCalledOnce()

    fireEvent.click(screen.getByRole('button', { name: /위치만 복사/ }))
    await waitFor(() => expect(overlay.copySelectedLocation).toHaveBeenCalledOnce())
  })

  it('starts missing-pole creation with a default-on continuous toggle', () => {
    const overlay = overlayWorkspace(pointFeature())
    const onClose = vi.fn()
    useOverlayWorkspace.mockReturnValue(overlay)
    render(<OverlayAttributePanel onClose={onClose} />)
    const continuous = screen.getByRole('checkbox', { name: '연속 추가' })
    expect(continuous).toBeChecked()

    fireEvent.click(screen.getByRole('button', { name: /미검출 지주 추가/ }))
    expect(overlay.beginCreatePoleBase).toHaveBeenLastCalledWith(LAYER.id, true)

    fireEvent.click(continuous)
    fireEvent.click(screen.getByRole('button', { name: /미검출 지주 추가/ }))
    expect(overlay.beginCreatePoleBase).toHaveBeenLastCalledWith(LAYER.id, false)
    expect(onClose).toHaveBeenCalledTimes(2)
  })

  it('recomputes only a selected Point through the pole-base workflow', () => {
    const overlay = overlayWorkspace(pointFeature())
    const onClose = vi.fn()
    useOverlayWorkspace.mockReturnValue(overlay)
    render(<OverlayAttributePanel onClose={onClose} />)

    fireEvent.click(screen.getByRole('button', { name: /지주 하단 재산출/ }))

    expect(overlay.beginRecomputeSelectedPoleBase).toHaveBeenCalledOnce()
    expect(onClose).toHaveBeenCalledOnce()
  })

  it('disables pole-base tools for a non-Point layer or feature', () => {
    const overlay = overlayWorkspace(lineFeature())
    overlay.layers = [{ ...LAYER, geometry_type: 'LineString' }]
    overlay.selectedLayer = overlay.layers[0]
    useOverlayWorkspace.mockReturnValue(overlay)
    render(<OverlayAttributePanel onClose={vi.fn()} />)

    expect(screen.getByRole('button', { name: /미검출 지주 추가/ })).toBeDisabled()
    expect(screen.getByRole('checkbox', { name: '연속 추가' })).toBeDisabled()
    expect(screen.getByRole('button', { name: /지주 하단 재산출/ })).toBeDisabled()
  })

  it('disables pole-base tools when the server capability is unavailable', () => {
    const overlay = overlayWorkspace(pointFeature())
    overlay.poleBaseInferenceEnabled = false
    useOverlayWorkspace.mockReturnValue(overlay)
    render(<OverlayAttributePanel onClose={vi.fn()} />)

    expect(screen.getByRole('button', { name: /미검출 지주 추가/ })).toBeDisabled()
    expect(screen.getByRole('checkbox', { name: '연속 추가' })).toBeDisabled()
    expect(screen.getByRole('button', { name: /지주 하단 재산출/ })).toBeDisabled()
  })

  it('deletes the selected attribute column after confirmation in its owner window', async () => {
    const overlay = overlayWorkspace(pointFeature())
    useOverlayWorkspace.mockReturnValue(overlay)
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(<OverlayAttributePanel onClose={vi.fn()} />)

    fireEvent.click(screen.getByRole('button', { name: 'status' }))
    const deleteColumn = screen.getByRole('button', { name: /선택 열 삭제/ })
    expect(deleteColumn).toBeEnabled()
    fireEvent.click(deleteColumn)

    expect(confirm).toHaveBeenCalledWith(expect.stringContaining("'status' 속성 열"))
    await waitFor(() => expect(overlay.deleteField).toHaveBeenCalledWith(LAYER.id, 'status'))
  })

  it('does not offer deletion for the automatic ID field', () => {
    const overlay = overlayWorkspace(pointFeature())
    overlay.features[LAYER.id].dataset = {
      ...collection([pointFeature()]),
      fields: [{ name: 'ID', type: 'N' }, { name: 'status', type: 'C' }],
    }
    useOverlayWorkspace.mockReturnValue(overlay)
    render(<OverlayAttributePanel onClose={vi.fn()} />)

    fireEvent.click(screen.getByRole('button', { name: 'ID' }))
    expect(screen.getByRole('button', { name: /선택 열 삭제/ })).toBeDisabled()
  })
})
