import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../lib/api'
import type {
  OverlayCoordinateSpace,
  OverlayFeature,
  OverlayFeatureCollection,
  OverlayLayer,
} from '../types'
import { OverlayProvider, useOverlayWorkspace } from './OverlayContext'

const LAYER: OverlayLayer = {
  id: 'layer-1',
  dataset_id: 'dataset-1',
  name: '검출 지주',
  geometry_type: 'Point',
  feature_count: 7,
  revision: 4,
}

function feature(id: string, coordinate: number, label = id): OverlayFeature {
  return {
    type: 'Feature',
    id,
    geometry: { type: 'Point', coordinates: [coordinate, coordinate + 0.5] },
    properties: { label },
  }
}

function page(
  items: OverlayFeature[],
  options: { offset?: number; total?: number; nextOffset?: number | null } = {},
): OverlayFeatureCollection {
  return {
    type: 'FeatureCollection',
    features: items,
    fields: [{ name: 'label', type: 'C' }],
    total: options.total ?? items.length,
    offset: options.offset ?? 0,
    limit: 3_000,
    revision: LAYER.revision,
    next_offset: options.nextOffset ?? null,
  }
}

const WGS_FIRST = page(
  [feature('wgs-1', 126), feature('wgs-2', 127), feature('wgs-3', 128)],
  { total: 7, nextOffset: 5 },
)
const DATASET_FIRST = page(
  [feature('dataset-1', 1), feature('dataset-2', 2)],
  { total: 7, nextOffset: 2 },
)
const WGS_MORE = page([feature('wgs-6', 131), feature('wgs-7', 132)], {
  offset: 5,
  total: 7,
  nextOffset: null,
})
const DATASET_MORE = page(
  [
    feature('dataset-3', 3),
    feature('dataset-4', 4),
    feature('dataset-5', 5),
    feature('dataset-6', 6),
    feature('dataset-7', 7),
  ],
  { offset: 2, total: 7, nextOffset: null },
)

function WorkspaceProbe() {
  const overlay = useOverlayWorkspace()
  const layerFeatures = overlay.features[LAYER.id]
  return (
    <div>
      <output data-testid="wgs-ids">
        {layerFeatures?.wgs84?.features.map((item) => item.id).join(',') ?? 'not-loaded'}
      </output>
      <output data-testid="dataset-ids">
        {layerFeatures?.dataset?.features.map((item) => item.id).join(',') ?? 'not-loaded'}
      </output>
      <output data-testid="selected-label">
        {String(overlay.selectedDatasetFeature?.properties.label ?? 'not-selected')}
      </output>
      <output data-testid="pick-mode">{overlay.pickMode ? 'on' : 'off'}</output>
      <button type="button" onClick={() => void overlay.ensureDatasetFeatures(LAYER.id)}>
        ensure dataset
      </button>
      <button type="button" onClick={() => void overlay.loadMoreDatasetFeatures(LAYER.id)}>
        load more
      </button>
      <button
        type="button"
        onClick={() => overlay.selectFeature({ layerId: LAYER.id, featureId: 'outside-42' })}
      >
        select outside
      </button>
      <input aria-label="속성 입력" />
    </div>
  )
}

function renderWorkspace() {
  return render(
    <OverlayProvider datasetId="dataset-1" demoMode={false}>
      <WorkspaceProbe />
    </OverlayProvider>,
  )
}

function mockFeaturePages() {
  vi.spyOn(api, 'overlays').mockResolvedValue({ items: [LAYER] })
  return vi
    .spyOn(api, 'overlayFeatures')
    .mockImplementation(
      async (
        _datasetId: string,
        _layerId: string,
        coordinateSpace: OverlayCoordinateSpace,
        offset = 0,
      ) => {
        if (coordinateSpace === 'wgs84' && offset === 0) return WGS_FIRST
        if (coordinateSpace === 'dataset' && offset === 0) return DATASET_FIRST
        if (coordinateSpace === 'wgs84' && offset === 5) return WGS_MORE
        if (coordinateSpace === 'dataset' && offset === 2) return DATASET_MORE
        throw new Error(`Unexpected feature page: ${coordinateSpace}:${offset}`)
      },
    )
}

function mockOutsideFeature() {
  return vi.spyOn(api, 'overlayFeature').mockResolvedValue({
    feature: feature('outside-42', 42, '첫 페이지 밖 피처'),
    revision: LAYER.revision,
    coordinate_space: 'dataset',
    crs: 'EPSG:5186',
    fields: [{ name: 'label', type: 'C' }],
  })
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('OverlayProvider feature loading', () => {
  it('loads only WGS84 during refresh and lazily loads dataset coordinates on ensure', async () => {
    const overlayFeatures = mockFeaturePages()
    renderWorkspace()

    await waitFor(() => expect(screen.getByTestId('wgs-ids')).toHaveTextContent('wgs-1,wgs-2,wgs-3'))
    expect(screen.getByTestId('dataset-ids')).toHaveTextContent('not-loaded')
    expect(overlayFeatures).toHaveBeenCalledTimes(1)
    expect(overlayFeatures.mock.calls[0][2]).toBe('wgs84')

    fireEvent.click(screen.getByRole('button', { name: 'ensure dataset' }))

    await waitFor(() =>
      expect(screen.getByTestId('dataset-ids')).toHaveTextContent('dataset-1,dataset-2'),
    )
    expect(overlayFeatures.mock.calls.map((call) => call[2])).toEqual(['wgs84', 'dataset'])
  })

  it('appends dataset and WGS84 pages using each coordinate space next_offset', async () => {
    const overlayFeatures = mockFeaturePages()
    renderWorkspace()
    await waitFor(() => expect(screen.getByTestId('wgs-ids')).toHaveTextContent('wgs-1'))

    fireEvent.click(screen.getByRole('button', { name: 'ensure dataset' }))
    await waitFor(() => expect(screen.getByTestId('dataset-ids')).toHaveTextContent('dataset-2'))
    fireEvent.click(screen.getByRole('button', { name: 'load more' }))

    await waitFor(() => {
      expect(screen.getByTestId('dataset-ids')).toHaveTextContent(
        'dataset-1,dataset-2,dataset-3,dataset-4,dataset-5,dataset-6,dataset-7',
      )
      expect(screen.getByTestId('wgs-ids')).toHaveTextContent(
        'wgs-1,wgs-2,wgs-3,wgs-6,wgs-7',
      )
    })
    expect(
      overlayFeatures.mock.calls.some(
        (call) => call[2] === 'dataset' && call[3] === DATASET_FIRST.next_offset,
      ),
    ).toBe(true)
    expect(
      overlayFeatures.mock.calls.some(
        (call) => call[2] === 'wgs84' && call[3] === WGS_FIRST.next_offset,
      ),
    ).toBe(true)
  })

  it('fetches a single dataset-coordinate detail for selection outside the loaded page', async () => {
    mockFeaturePages()
    const overlayFeature = mockOutsideFeature()
    renderWorkspace()
    await waitFor(() => expect(screen.getByTestId('wgs-ids')).toHaveTextContent('wgs-1'))
    fireEvent.click(screen.getByRole('button', { name: 'ensure dataset' }))
    await waitFor(() => expect(screen.getByTestId('dataset-ids')).toHaveTextContent('dataset-1'))

    fireEvent.click(screen.getByRole('button', { name: 'select outside' }))

    await waitFor(() =>
      expect(screen.getByTestId('selected-label')).toHaveTextContent('첫 페이지 밖 피처'),
    )
    expect(overlayFeature).toHaveBeenCalledOnce()
    expect(overlayFeature).toHaveBeenCalledWith(
      'dataset-1',
      LAYER.id,
      'outside-42',
      'dataset',
      expect.any(AbortSignal),
    )
  })
})

describe('OverlayProvider pick-mode shortcuts', () => {
  it('toggles with P and Escape globally while ignoring editable inputs', async () => {
    mockFeaturePages()
    mockOutsideFeature()
    renderWorkspace()
    await waitFor(() => expect(screen.getByTestId('wgs-ids')).toHaveTextContent('wgs-1'))
    fireEvent.click(screen.getByRole('button', { name: 'select outside' }))
    await waitFor(() => expect(screen.getByTestId('selected-label')).toHaveTextContent('첫 페이지 밖 피처'))
    const input = screen.getByRole('textbox', { name: '속성 입력' })

    fireEvent.keyDown(input, { key: 'p', code: 'KeyP' })
    expect(screen.getByTestId('pick-mode')).toHaveTextContent('off')

    fireEvent.keyDown(window, { key: 'p', code: 'KeyP' })
    expect(screen.getByTestId('pick-mode')).toHaveTextContent('on')

    fireEvent.keyDown(input, { key: 'Escape', code: 'Escape' })
    expect(screen.getByTestId('pick-mode')).toHaveTextContent('on')

    fireEvent.keyDown(window, { key: 'Escape', code: 'Escape' })
    expect(screen.getByTestId('pick-mode')).toHaveTextContent('off')
  })
})
