import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError } from '../lib/api'
import type {
  OverlayCoordinateSpace,
  OverlayFeature,
  OverlayFeatureCollection,
  OverlayLayer,
  PoleBaseInferResponse,
} from '../types'
import {
  buildPoleBasePropertyPatch,
  OverlayProvider,
  poleBaseReasonMessage,
  useOverlayWorkspace,
} from './OverlayContext'

const LAYER: OverlayLayer = {
  id: 'layer-1',
  dataset_id: 'dataset-1',
  name: '검출 지주',
  geometry_type: 'Point',
  feature_count: 7,
  revision: 4,
  metadata_revision: 1,
}

const POLE_LAYER: OverlayLayer = {
  ...LAYER,
  fields: [
    { name: 'base_x', type: 'N', decimal: 3 },
    { name: 'BAS Y', type: 'C' },
    { name: 'ELEV', type: 'F', decimal: 3 },
    { name: 'BASE_MTH', type: 'C' },
    { name: 'BASE_Q', type: 'N', decimal: 0 },
    { name: 'QA_STATUS', type: 'C' },
    { name: 'SRC_FRAME', type: 'C' },
    { name: 'X', type: 'N', decimal: 3 },
  ],
}

const AUTO_POLE_BASE_RESULT: PoleBaseInferResponse = {
  status: 'auto',
  algorithm: 'manual_seed_axis_ground_intersection',
  algorithm_version: '1',
  coordinate_space: 'dataset',
  seed_position: [10, 20, 35],
  snapped_seed_position: [10.01, 20.01, 34.99],
  base_position: [10.1, 20.2, 30.3],
  quality: {
    score: 0.91,
    candidate_count: 1,
    ambiguous: false,
    bottom_gap_m: 0.2,
    components: {
      seed: 0.9,
      axis: 0.92,
      span: 0.95,
      continuity: 0.9,
      ground: 0.88,
      bottom_gap: 1,
    },
  },
  reason_codes: [],
  warnings: [],
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
      <output data-testid="pole-proposal-status">{overlay.poleBaseProposal.status}</output>
      <output data-testid="pole-result-status">
        {overlay.poleBaseProposal.status === 'ready'
          ? overlay.poleBaseProposal.result.status
          : 'none'}
      </output>
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
      <button type="button" onClick={() => overlay.beginCreatePoleBase(LAYER.id, false)}>
        begin pole once
      </button>
      <button type="button" onClick={() => overlay.beginCreatePoleBase(LAYER.id, true)}>
        begin pole continuous
      </button>
      <button type="button" onClick={() => overlay.beginRecomputeSelectedPoleBase()}>
        begin pole recompute
      </button>
      <button
        type="button"
        onClick={() => void overlay.applyPoleSeed('frame-1', [10, 20, 35])}
      >
        apply pole seed
      </button>
      <button
        type="button"
        onClick={() => void overlay.applyPoleSeed('frame-2', [11, 21, 36])}
      >
        apply next-frame pole seed
      </button>
      <button type="button" onClick={() => void overlay.confirmPoleBaseProposal()}>
        confirm pole
      </button>
      <button type="button" onClick={() => overlay.retryPoleBasePick()}>
        retry pole
      </button>
      <button type="button" onClick={() => overlay.handlePoleBaseFrameChange('frame-2')}>
        change pole frame
      </button>
      <button type="button" onClick={() => overlay.cancelPoleBaseProposal()}>
        cancel pole
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
        aria-label="focused viewer shortcut surface"
        onKeyDown={(event) => event.stopPropagation()}
      >
        focused viewer shortcut surface
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

function renderWorkspace(activeFrameId?: string | null) {
  return render(
    <OverlayProvider
      datasetId="dataset-1"
      activeFrameId={activeFrameId}
      demoMode={false}
    >
      <WorkspaceProbe />
    </OverlayProvider>,
  )
}

function mockFeaturePages(layer: OverlayLayer = LAYER) {
  vi.spyOn(api, 'overlays').mockResolvedValue({ items: [layer] })
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
    // previous create target. The viewer also stops bubbling, as map SDKs do
    // after the picked canvas has retained keyboard focus.
    const viewer = screen.getByRole('button', { name: 'focused viewer shortcut surface' })
    fireEvent.keyDown(viewer, { key: 'n', code: 'KeyN' })
    expect(screen.getByTestId('pick-target')).toHaveTextContent('create')

    fireEvent.keyDown(viewer, { key: 'Escape', code: 'Escape' })
    expect(screen.getByTestId('pick-target')).toHaveTextContent('none')
    fireEvent.keyDown(viewer, { key: 'p', code: 'KeyP' })
    expect(screen.getByTestId('pick-target')).toHaveTextContent('move')
    fireEvent.keyDown(viewer, { key: 'Escape', code: 'Escape' })
    expect(screen.getByTestId('pick-target')).toHaveTextContent('none')
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

    const viewer = screen.getByRole('button', { name: 'focused viewer shortcut surface' })
    fireEvent.keyDown(viewer, { key: 'n', code: 'KeyN' })
    expect(screen.getByTestId('pick-target')).toHaveTextContent('create')

    fireEvent.keyDown(viewer, { key: 'Escape', code: 'Escape' })
    expect(screen.getByTestId('pick-target')).toHaveTextContent('none')

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

describe('manual pole-base proposals', () => {
  it('builds a patch only for exact normalized aliases and respects DBF value types', () => {
    const patch = buildPoleBasePropertyPatch(
      POLE_LAYER.fields ?? [],
      { label: 'keep', base_x: 0 },
      AUTO_POLE_BASE_RESULT,
      'frame-1',
    )

    expect(patch).toEqual({
      base_x: 10.1,
      'BAS Y': '20.2',
      ELEV: 30.3,
      BASE_MTH: 'MAN_SEED',
      BASE_Q: 91,
      QA_STATUS: 'AUTO',
      SRC_FRAME: 'frame-1',
    })
    expect(patch).not.toHaveProperty('X')
    expect(
      buildPoleBasePropertyPatch(
        [
          { name: 'BAS_Q', type: 'F', decimal: 0 },
          { name: 'BASE_X_EXTRA', type: 'N', decimal: 3 },
        ],
        {},
        AUTO_POLE_BASE_RESULT,
        'frame-1',
      ),
    ).toEqual({ BAS_Q: 91 })
    expect(
      buildPoleBasePropertyPatch(
        [{ name: 'label', type: 'C' }],
        { label: 'keep' },
        AUTO_POLE_BASE_RESULT,
        'frame-1',
      ),
    ).toEqual({})
    expect(poleBaseReasonMessage('NO_GROUND_SUPPORT')).toContain('지면')
  })

  it('keeps create inference read-only until confirmation and atomically creates geometry and aliases', async () => {
    mockFeaturePages(POLE_LAYER)
    let resolveInference!: (result: PoleBaseInferResponse) => void
    const inferPoleBase = vi.spyOn(api, 'inferPoleBase').mockReturnValue(
      new Promise<PoleBaseInferResponse>((resolve) => {
        resolveInference = resolve
      }),
    )
    const created: OverlayFeature = {
      ...feature('pole-new', 10.1, ''),
      geometry: { type: 'Point', coordinates: AUTO_POLE_BASE_RESULT.base_position },
      properties: { BASE_MTH: 'MAN_SEED' },
    }
    const createOverlayFeature = vi.spyOn(api, 'createOverlayFeature').mockResolvedValue({
      feature: created,
      revision: 5,
      coordinate_space: 'dataset',
      crs: 'EPSG:5186',
      fields: POLE_LAYER.fields ?? [],
    })
    vi.spyOn(api, 'overlayFeature').mockResolvedValue({
      feature: created,
      revision: 5,
      coordinate_space: 'wgs84',
      crs: 'EPSG:4326',
      fields: POLE_LAYER.fields ?? [],
    })
    const navigationListener = vi.fn()
    window.addEventListener('mms-overlay-selected', navigationListener)
    renderWorkspace()
    await waitFor(() => expect(screen.getByTestId('active-layer')).toHaveTextContent(LAYER.id))

    fireEvent.click(screen.getByRole('button', { name: 'begin pole once' }))
    expect(screen.getByTestId('pick-target')).toHaveTextContent('pole-base-create')
    fireEvent.click(screen.getByRole('button', { name: 'apply pole seed' }))

    await waitFor(() => expect(screen.getByTestId('pole-proposal-status')).toHaveTextContent('loading'))
    expect(createOverlayFeature).not.toHaveBeenCalled()
    expect(inferPoleBase).toHaveBeenCalledWith(
      'dataset-1',
      'frame-1',
      {
        coordinate_space: 'dataset',
        seed_position: [10, 20, 35],
        profile: 'balanced',
        debug: false,
      },
      expect.any(AbortSignal),
    )

    resolveInference(AUTO_POLE_BASE_RESULT)
    await waitFor(() => expect(screen.getByTestId('pole-proposal-status')).toHaveTextContent('ready'))
    expect(createOverlayFeature).not.toHaveBeenCalled()

    fireEvent.keyDown(window, { key: 'Enter', code: 'Enter' })
    await waitFor(() => expect(createOverlayFeature).toHaveBeenCalledOnce())
    expect(createOverlayFeature).toHaveBeenCalledWith('dataset-1', LAYER.id, {
      geometry: { type: 'Point', coordinates: [10.1, 20.2, 30.3] },
      coordinate_space: 'dataset',
      properties: {
        base_x: 10.1,
        'BAS Y': '20.2',
        ELEV: 30.3,
        BASE_MTH: 'MAN_SEED',
        BASE_Q: 91,
        QA_STATUS: 'AUTO',
        SRC_FRAME: 'frame-1',
      },
      expected_revision: LAYER.revision,
    })
    await waitFor(() => expect(screen.getByTestId('pole-proposal-status')).toHaveTextContent('idle'))
    expect(navigationListener).not.toHaveBeenCalled()
    window.removeEventListener('mms-overlay-selected', navigationListener)
  })

  it('patches a selected feature with geometry, aliases, and the expected revision', async () => {
    mockFeaturePages(POLE_LAYER)
    mockOutsideFeature()
    const reviewResult: PoleBaseInferResponse = {
      ...AUTO_POLE_BASE_RESULT,
      status: 'review',
      reason_codes: ['AMBIGUOUS_AXES'],
    }
    vi.spyOn(api, 'inferPoleBase').mockResolvedValue(reviewResult)
    const updated: OverlayFeature = {
      ...feature('outside-42', 10.1, '첫 페이지 밖 피처'),
      geometry: { type: 'Point', coordinates: reviewResult.base_position },
    }
    const patchOverlayFeature = vi.spyOn(api, 'patchOverlayFeature').mockResolvedValue({
      feature: updated,
      revision: 5,
      coordinate_space: 'dataset',
    })
    renderWorkspace()
    await waitFor(() => expect(screen.getByTestId('wgs-ids')).toHaveTextContent('wgs-1'))
    fireEvent.click(screen.getByRole('button', { name: 'select outside' }))
    await waitFor(() => expect(screen.getByTestId('selected-id')).toHaveTextContent('outside-42'))

    fireEvent.click(screen.getByRole('button', { name: 'begin pole recompute' }))
    fireEvent.click(screen.getByRole('button', { name: 'apply pole seed' }))
    await waitFor(() => expect(screen.getByTestId('pole-result-status')).toHaveTextContent('review'))
    fireEvent.click(screen.getByRole('button', { name: 'confirm pole' }))

    await waitFor(() => expect(patchOverlayFeature).toHaveBeenCalledOnce())
    expect(patchOverlayFeature).toHaveBeenCalledWith('dataset-1', LAYER.id, 'outside-42', {
      geometry: { type: 'Point', coordinates: [10.1, 20.2, 30.3] },
      coordinate_space: 'dataset',
      properties: expect.objectContaining({
        QA_STATUS: 'REVIEW',
        BASE_MTH: 'MAN_SEED',
        BASE_Q: 91,
        SRC_FRAME: 'frame-1',
      }),
      expected_revision: LAYER.revision,
    })
    await waitFor(() => expect(screen.getByTestId('pole-proposal-status')).toHaveTextContent('idle'))
  })

  it('does not save a failed inference result', async () => {
    mockFeaturePages(POLE_LAYER)
    vi.spyOn(api, 'inferPoleBase').mockResolvedValue({
      ...AUTO_POLE_BASE_RESULT,
      status: 'failed',
      base_position: null,
      reason_codes: ['NO_GROUND_SUPPORT'],
    })
    const createOverlayFeature = vi.spyOn(api, 'createOverlayFeature')
    renderWorkspace()
    await waitFor(() => expect(screen.getByTestId('active-layer')).toHaveTextContent(LAYER.id))

    fireEvent.keyDown(window, { key: 'b', code: 'KeyB' })
    fireEvent.click(screen.getByRole('button', { name: 'apply pole seed' }))
    await waitFor(() => expect(screen.getByTestId('pole-result-status')).toHaveTextContent('failed'))
    fireEvent.keyDown(window, { key: 'Enter', code: 'Enter' })

    expect(createOverlayFeature).not.toHaveBeenCalled()
    expect(screen.getByTestId('pole-proposal-status')).toHaveTextContent('ready')
  })

  it('supports B, R, and two-level Escape globally while ignoring editable inputs', async () => {
    mockFeaturePages(POLE_LAYER)
    vi.spyOn(api, 'inferPoleBase').mockResolvedValue(AUTO_POLE_BASE_RESULT)
    renderWorkspace()
    await waitFor(() => expect(screen.getByTestId('active-layer')).toHaveTextContent(LAYER.id))
    const input = screen.getByRole('textbox', { name: '속성 입력' })

    fireEvent.keyDown(input, { key: 'b', code: 'KeyB' })
    expect(screen.getByTestId('pole-proposal-status')).toHaveTextContent('idle')

    fireEvent.keyDown(window, { key: 'b', code: 'KeyB' })
    expect(screen.getByTestId('pole-proposal-status')).toHaveTextContent('picking')
    fireEvent.click(screen.getByRole('button', { name: 'apply pole seed' }))
    await waitFor(() => expect(screen.getByTestId('pole-proposal-status')).toHaveTextContent('ready'))

    fireEvent.keyDown(window, { key: 'r', code: 'KeyR' })
    expect(screen.getByTestId('pole-proposal-status')).toHaveTextContent('picking')
    fireEvent.click(screen.getByRole('button', { name: 'apply pole seed' }))
    await waitFor(() => expect(screen.getByTestId('pole-proposal-status')).toHaveTextContent('ready'))

    fireEvent.keyDown(window, { key: 'Escape', code: 'Escape' })
    expect(screen.getByTestId('pole-proposal-status')).toHaveTextContent('picking')
    fireEvent.keyDown(window, { key: 'Escape', code: 'Escape' })
    expect(screen.getByTestId('pole-proposal-status')).toHaveTextContent('idle')
    expect(screen.getByTestId('pick-target')).toHaveTextContent('none')
  })

  it('aborts and ignores an old inference when the frame or dataset changes', async () => {
    mockFeaturePages(POLE_LAYER)
    const pendingResolvers: Array<(result: PoleBaseInferResponse) => void> = []
    const signals: AbortSignal[] = []
    vi.spyOn(api, 'inferPoleBase').mockImplementation(
      async (_datasetId, _frameId, _payload, signal) => {
        if (signal) signals.push(signal)
        return await new Promise<PoleBaseInferResponse>((resolve) => pendingResolvers.push(resolve))
      },
    )
    const rendered = renderWorkspace('frame-1')
    await waitFor(() => expect(screen.getByTestId('active-layer')).toHaveTextContent(LAYER.id))

    fireEvent.click(screen.getByRole('button', { name: 'begin pole continuous' }))
    fireEvent.click(screen.getByRole('button', { name: 'apply pole seed' }))
    await waitFor(() => expect(signals).toHaveLength(1))
    rendered.rerender(
      <OverlayProvider datasetId="dataset-1" activeFrameId="frame-2" demoMode={false}>
        <WorkspaceProbe />
      </OverlayProvider>,
    )
    expect(signals[0].aborted).toBe(true)
    expect(screen.getByTestId('pole-proposal-status')).toHaveTextContent('picking')
    pendingResolvers[0](AUTO_POLE_BASE_RESULT)
    await Promise.resolve()
    expect(screen.getByTestId('pole-proposal-status')).toHaveTextContent('picking')

    fireEvent.click(screen.getByRole('button', { name: 'apply next-frame pole seed' }))
    await waitFor(() => expect(signals).toHaveLength(2))
    rendered.rerender(
      <OverlayProvider datasetId="dataset-2" activeFrameId="frame-2" demoMode={false}>
        <WorkspaceProbe />
      </OverlayProvider>,
    )
    await waitFor(() => expect(signals[1].aborted).toBe(true))
    expect(screen.getByTestId('pole-proposal-status')).toHaveTextContent('idle')
    pendingResolvers[1](AUTO_POLE_BASE_RESULT)
    await Promise.resolve()
    expect(screen.getByTestId('pole-proposal-status')).toHaveTextContent('idle')
  })

  it('returns to picking after a successful continuous create', async () => {
    mockFeaturePages(POLE_LAYER)
    vi.spyOn(api, 'inferPoleBase').mockResolvedValue(AUTO_POLE_BASE_RESULT)
    const created = feature('pole-continuous', 10.1, '')
    vi.spyOn(api, 'createOverlayFeature').mockResolvedValue({
      feature: created,
      revision: 5,
      coordinate_space: 'dataset',
      crs: 'EPSG:5186',
      fields: POLE_LAYER.fields ?? [],
    })
    vi.spyOn(api, 'overlayFeature').mockResolvedValue({
      feature: created,
      revision: 5,
      coordinate_space: 'wgs84',
      crs: 'EPSG:4326',
      fields: POLE_LAYER.fields ?? [],
    })
    renderWorkspace()
    await waitFor(() => expect(screen.getByTestId('active-layer')).toHaveTextContent(LAYER.id))

    fireEvent.click(screen.getByRole('button', { name: 'begin pole continuous' }))
    fireEvent.click(screen.getByRole('button', { name: 'apply pole seed' }))
    await waitFor(() => expect(screen.getByTestId('pole-proposal-status')).toHaveTextContent('ready'))
    fireEvent.click(screen.getByRole('button', { name: 'confirm pole' }))

    await waitFor(() => expect(screen.getByTestId('pole-proposal-status')).toHaveTextContent('picking'))
    expect(screen.getByTestId('pick-target')).toHaveTextContent('pole-base-create')
  })

  it('refreshes a 409 revision conflict and keeps the ready proposal retryable', async () => {
    let revision = 4
    const revisedLayer = () => ({ ...POLE_LAYER, revision })
    vi.spyOn(api, 'overlays').mockImplementation(async () => ({ items: [revisedLayer()] }))
    vi.spyOn(api, 'overlayFeatures').mockImplementation(async () => ({
      ...WGS_FIRST,
      revision,
    }))
    vi.spyOn(api, 'inferPoleBase').mockResolvedValue(AUTO_POLE_BASE_RESULT)
    const created = feature('pole-after-conflict', 10.1, '')
    const createOverlayFeature = vi
      .spyOn(api, 'createOverlayFeature')
      .mockImplementationOnce(async () => {
        revision = 5
        throw new ApiError('revision conflict', 409, 'REVISION_CONFLICT')
      })
      .mockResolvedValue({
        feature: created,
        revision: 6,
        coordinate_space: 'dataset',
        crs: 'EPSG:5186',
        fields: POLE_LAYER.fields ?? [],
      })
    vi.spyOn(api, 'overlayFeature').mockResolvedValue({
      feature: created,
      revision: 6,
      coordinate_space: 'wgs84',
      crs: 'EPSG:4326',
      fields: POLE_LAYER.fields ?? [],
    })
    renderWorkspace()
    await waitFor(() => expect(screen.getByTestId('active-layer')).toHaveTextContent(LAYER.id))

    fireEvent.click(screen.getByRole('button', { name: 'begin pole once' }))
    fireEvent.click(screen.getByRole('button', { name: 'apply pole seed' }))
    await waitFor(() => expect(screen.getByTestId('pole-proposal-status')).toHaveTextContent('ready'))
    fireEvent.click(screen.getByRole('button', { name: 'confirm pole' }))

    await waitFor(() => expect(createOverlayFeature).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(screen.getByTestId('pole-proposal-status')).toHaveTextContent('ready'))
    fireEvent.click(screen.getByRole('button', { name: 'confirm pole' }))
    await waitFor(() => expect(createOverlayFeature).toHaveBeenCalledTimes(2))
    expect(createOverlayFeature.mock.calls[1][2]).toMatchObject({ expected_revision: 5 })
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
