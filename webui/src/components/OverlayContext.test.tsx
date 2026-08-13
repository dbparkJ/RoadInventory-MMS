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
  metadata_revision: 1,
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
      <output data-testid="selected-wgs-label">
        {String(overlay.selectedFeature?.properties.label ?? 'not-selected')}
      </output>
      <output data-testid="map-selected-ids">
        {overlay.mapFeatures
          .filter((item) => Number(item.properties.__overlay_selected) === 1)
          .map((item) => item.id)
          .join(',') || 'none'}
      </output>
      <output data-testid="pick-mode">{overlay.pickMode ? 'on' : 'off'}</output>
      <output data-testid="pick-target">{overlay.pickTarget?.kind ?? 'none'}</output>
      <output data-testid="active-layer">{overlay.activeLayerId || 'none'}</output>
      <output data-testid="layer-name">{overlay.layers[0]?.name ?? 'none'}</output>
      <output data-testid="layer-color">{overlay.layerColor(LAYER.id)}</output>
      <output data-testid="selected-id">{String(overlay.selected?.featureId ?? 'none')}</output>
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
      <button type="button" onClick={() => overlay.selectFeature(null)}>
        clear selection
      </button>
      <button
        type="button"
        onClick={() =>
          overlay.selectFeature(
            { layerId: LAYER.id, featureId: 'outside-42' },
            { navigate: false },
          )
        }
      >
        select details only
      </button>
      <button type="button" onClick={() => overlay.beginCreatePoint(LAYER.id)}>
        begin create
      </button>
      <button
        type="button"
        onClick={() => void overlay.applyPickedCoordinate([127.1, 37.2], 'wgs84')}
      >
        apply coordinate
      </button>
      <button type="button" onClick={() => void overlay.copySelectedLocation()}>
        copy location
      </button>
      <button type="button" onClick={() => void overlay.deleteSelected()}>
        delete selected
      </button>
      <button type="button" onClick={() => void overlay.deleteField(LAYER.id, 'label')}>
        delete field
      </button>
      <button
        type="button"
        onClick={() => void overlay.updateLayerMetadata(LAYER.id, { name: '새 이름', color: '#112233' })}
      >
        update layer metadata
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
  return vi.spyOn(api, 'overlayFeature').mockImplementation(
    async (_datasetId, _layerId, _featureId, coordinateSpace) => ({
      feature: feature(
        'outside-42',
        coordinateSpace === 'wgs84' ? 127.42 : 42,
        '첫 페이지 밖 피처',
      ),
      revision: LAYER.revision,
      coordinate_space: coordinateSpace,
      crs: coordinateSpace === 'wgs84' ? 'EPSG:4326' : 'EPSG:5186',
      fields: [{ name: 'label', type: 'C' }],
    }),
  )
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('OverlayProvider feature loading', () => {
  it('can select a feature for attribute details without triggering frame navigation', async () => {
    mockFeaturePages()
    mockOutsideFeature()
    const navigationListener = vi.fn()
    window.addEventListener('mms-overlay-selected', navigationListener)
    renderWorkspace()
    await waitFor(() => expect(screen.getByTestId('wgs-ids')).toHaveTextContent('wgs-1'))

    fireEvent.click(screen.getByRole('button', { name: 'select details only' }))

    await waitFor(() => expect(screen.getByTestId('selected-id')).toHaveTextContent('outside-42'))
    expect(navigationListener).not.toHaveBeenCalled()
    window.removeEventListener('mms-overlay-selected', navigationListener)
  })

  it('updates the shared persisted layer name and color with metadata revision', async () => {
    mockFeaturePages()
    const updatedLayer: OverlayLayer = {
      ...LAYER,
      name: '새 이름',
      color: '#112233',
      metadata_revision: 2,
    }
    const patchOverlay = vi.spyOn(api, 'patchOverlay').mockResolvedValue({ layer: updatedLayer })
    renderWorkspace()
    await waitFor(() => expect(screen.getByTestId('active-layer')).toHaveTextContent(LAYER.id))

    fireEvent.click(screen.getByRole('button', { name: 'update layer metadata' }))

    await waitFor(() => expect(screen.getByTestId('layer-name')).toHaveTextContent('새 이름'))
    expect(screen.getByTestId('layer-color')).toHaveTextContent('#112233')
    expect(patchOverlay).toHaveBeenCalledWith('dataset-1', LAYER.id, {
      name: '새 이름',
      color: '#112233',
      expected_metadata_revision: 1,
    })
  })

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

  it('fetches both coordinate details and merges a page-external selection into the map', async () => {
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
    await waitFor(() => {
      expect(screen.getByTestId('selected-wgs-label')).toHaveTextContent('첫 페이지 밖 피처')
      expect(screen.getByTestId('map-selected-ids')).toHaveTextContent('outside-42')
    })
    expect(overlayFeature).toHaveBeenCalledTimes(2)
    expect(overlayFeature.mock.calls.map((call) => call.slice(0, 4))).toEqual(
      expect.arrayContaining([
        ['dataset-1', LAYER.id, 'outside-42', 'dataset'],
        ['dataset-1', LAYER.id, 'outside-42', 'wgs84'],
      ]),
    )
    expect(overlayFeature.mock.calls.every((call) => call[4] instanceof AbortSignal)).toBe(true)
  })

  it('aborts stale coordinate-detail requests and clears the merged map selection', async () => {
    mockFeaturePages()
    const signals: AbortSignal[] = []
    vi.spyOn(api, 'overlayFeature').mockImplementation(
      async (_datasetId, _layerId, _featureId, coordinateSpace, signal) => {
        if (signal) signals.push(signal)
        return await new Promise((_resolve, reject) => {
          signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')))
        })
      },
    )
    renderWorkspace()
    await waitFor(() => expect(screen.getByTestId('wgs-ids')).toHaveTextContent('wgs-1'))

    fireEvent.click(screen.getByRole('button', { name: 'select outside' }))
    await waitFor(() => expect(signals).toHaveLength(2))
    fireEvent.click(screen.getByRole('button', { name: 'clear selection' }))

    expect(signals.every((signal) => signal.aborted)).toBe(true)
    expect(screen.getByTestId('selected-id')).toHaveTextContent('none')
    expect(screen.getByTestId('selected-wgs-label')).toHaveTextContent('not-selected')
    expect(screen.getByTestId('map-selected-ids')).toHaveTextContent('none')
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

  it('toggles new Point picking with N, ignores inputs and does not retrigger on key repeat', async () => {
    mockFeaturePages()
    renderWorkspace()
    await waitFor(() => expect(screen.getByTestId('active-layer')).toHaveTextContent(LAYER.id))
    const input = screen.getByRole('textbox', { name: '속성 입력' })

    fireEvent.keyDown(input, { key: 'n', code: 'KeyN' })
    expect(screen.getByTestId('pick-target')).toHaveTextContent('none')

    fireEvent.keyDown(window, { key: 'n', code: 'KeyN' })
    expect(screen.getByTestId('pick-target')).toHaveTextContent('create')

    fireEvent.keyDown(window, { key: 'n', code: 'KeyN', repeat: true })
    expect(screen.getByTestId('pick-target')).toHaveTextContent('create')

    fireEvent.keyDown(window, { key: 'n', code: 'KeyN' })
    expect(screen.getByTestId('pick-target')).toHaveTextContent('none')

    fireEvent.keyDown(window, { key: 'n', code: 'KeyN', ctrlKey: true })
    expect(screen.getByTestId('pick-target')).toHaveTextContent('none')
  })

  it('keeps N inactive and explains when the active SHP layer is not Point geometry', async () => {
    const lineLayer = { ...LAYER, geometry_type: 'Polyline' }
    vi.spyOn(api, 'overlays').mockResolvedValue({ items: [lineLayer] })
    vi.spyOn(api, 'overlayFeatures').mockResolvedValue(WGS_FIRST)
    const notify = vi.fn()
    render(
      <OverlayProvider datasetId="dataset-1" demoMode={false} notify={notify}>
        <WorkspaceProbe />
      </OverlayProvider>,
    )
    await waitFor(() => expect(screen.getByTestId('active-layer')).toHaveTextContent(LAYER.id))

    fireEvent.keyDown(window, { key: 'n', code: 'KeyN' })

    expect(screen.getByTestId('pick-target')).toHaveTextContent('none')
    expect(notify).toHaveBeenCalledWith(expect.objectContaining({
      tone: 'info',
      title: expect.stringContaining('Point 레이어'),
    }))
  })

  it('creates a Point at the picked WGS84 coordinate and selects the server-assigned ID', async () => {
    mockFeaturePages()
    const created = feature('f_000000008', 127.1, '')
    const createOverlayFeature = vi.spyOn(api, 'createOverlayFeature').mockResolvedValue({
      feature: created,
      revision: 5,
      coordinate_space: 'wgs84',
      crs: 'EPSG:4326',
      fields: [{ name: 'label', type: 'C' }],
    })
    vi.spyOn(api, 'overlayFeature').mockResolvedValue({
      feature: feature('f_000000008', 8, ''),
      revision: 5,
      coordinate_space: 'dataset',
      crs: 'EPSG:5186',
      fields: [{ name: 'label', type: 'C' }],
    })
    renderWorkspace()
    await waitFor(() => expect(screen.getByTestId('active-layer')).toHaveTextContent(LAYER.id))

    fireEvent.click(screen.getByRole('button', { name: 'begin create' }))
    expect(screen.getByTestId('pick-target')).toHaveTextContent('create')
    fireEvent.click(screen.getByRole('button', { name: 'apply coordinate' }))

    await waitFor(() => expect(screen.getByTestId('selected-id')).toHaveTextContent('f_000000008'))
    expect(createOverlayFeature).toHaveBeenCalledWith('dataset-1', LAYER.id, {
      geometry: { type: 'Point', coordinates: [127.1, 37.2] },
      coordinate_space: 'wgs84',
      expected_revision: LAYER.revision,
    })
    expect(screen.getByTestId('pick-target')).toHaveTextContent('none')

    // The stable global listener must observe the post-request state. This
    // guards against a completed point-pick leaving N permanently bound to the
    // previous create target.
    fireEvent.keyDown(window, { key: 'n', code: 'KeyN' })
    expect(screen.getByTestId('pick-target')).toHaveTextContent('create')
  })

  it('keeps global edit and frame shortcuts live after deleting the selected feature', async () => {
    mockFeaturePages()
    mockOutsideFeature()
    const deleteOverlayFeature = vi.spyOn(api, 'deleteOverlayFeature').mockResolvedValue({
      id: 'outside-42',
      deleted: true,
      revision: 5,
      source_preserved: true,
    })
    renderWorkspace()
    await waitFor(() => expect(screen.getByTestId('wgs-ids')).toHaveTextContent('wgs-1'))
    fireEvent.click(screen.getByRole('button', { name: 'select outside' }))
    await waitFor(() => expect(screen.getByTestId('selected-id')).toHaveTextContent('outside-42'))

    fireEvent.click(screen.getByRole('button', { name: 'delete selected' }))
    await waitFor(() => expect(deleteOverlayFeature).toHaveBeenCalledOnce())
    await waitFor(() => expect(screen.getByTestId('selected-id')).toHaveTextContent('none'))

    fireEvent.keyDown(window, { key: 'n', code: 'KeyN' })
    expect(screen.getByTestId('pick-target')).toHaveTextContent('create')

    const frameNavigation = vi.fn((event: KeyboardEvent) => event.preventDefault())
    window.addEventListener('keydown', frameNavigation)
    try {
      const event = new KeyboardEvent('keydown', {
        key: 'd',
        code: 'KeyD',
        bubbles: true,
        cancelable: true,
      })
      expect(window.dispatchEvent(event)).toBe(false)
      expect(frameNavigation).toHaveBeenCalledOnce()
    } finally {
      window.removeEventListener('keydown', frameNavigation)
    }
  })

  it('copies only the selected feature geometry through the server-side copy action', async () => {
    mockFeaturePages()
    mockOutsideFeature()
    const copied = feature('f_000000008', 42, '')
    const createOverlayFeature = vi.spyOn(api, 'createOverlayFeature').mockResolvedValue({
      feature: copied,
      revision: 5,
      coordinate_space: 'dataset',
      crs: 'EPSG:5186',
      fields: [{ name: 'label', type: 'C' }],
    })
    renderWorkspace()
    await waitFor(() => expect(screen.getByTestId('wgs-ids')).toHaveTextContent('wgs-1'))
    fireEvent.click(screen.getByRole('button', { name: 'select outside' }))
    await waitFor(() => expect(screen.getByTestId('selected-id')).toHaveTextContent('outside-42'))

    fireEvent.click(screen.getByRole('button', { name: 'copy location' }))

    await waitFor(() => expect(createOverlayFeature).toHaveBeenCalledOnce())
    expect(createOverlayFeature).toHaveBeenCalledWith('dataset-1', LAYER.id, {
      copy_geometry_from: 'outside-42',
      coordinate_space: 'dataset',
      expected_revision: LAYER.revision,
    })
    await waitFor(() => expect(screen.getByTestId('selected-id')).toHaveTextContent('f_000000008'))
  })
})

describe('OverlayProvider schema editing', () => {
  it('deletes a field with the current revision and refreshes the layer cache', async () => {
    const overlayFeatures = mockFeaturePages()
    const deleteOverlayField = vi.spyOn(api, 'deleteOverlayField').mockResolvedValue({
      deleted_field: 'label',
      revision: 5,
      fields: [{ name: 'status', type: 'C' }],
      layer: { ...LAYER, revision: 5, fields: [{ name: 'status', type: 'C' }] },
      source_preserved: true,
    })
    renderWorkspace()
    await waitFor(() => expect(screen.getByTestId('active-layer')).toHaveTextContent(LAYER.id))

    fireEvent.click(screen.getByRole('button', { name: 'delete field' }))

    await waitFor(() => expect(deleteOverlayField).toHaveBeenCalledOnce())
    expect(deleteOverlayField).toHaveBeenCalledWith('dataset-1', LAYER.id, 'label', LAYER.revision)
    await waitFor(() => expect(overlayFeatures.mock.calls.length).toBeGreaterThan(1))
  })
})
