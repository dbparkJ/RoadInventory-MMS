import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../lib/api'
import type {
  EquirectangularBBoxGeometry,
  Frame,
  GeometryProposal,
  ManualObjectTemplate,
  ManualObservation,
  OverlayLayer,
} from '../types'
import type { OverlayContextValue } from './OverlayContext'

const contextMocks = vi.hoisted(() => ({
  useOverlayWorkspace: vi.fn(),
  useOptionalReviewWorkspace: vi.fn(),
  isReviewTaskComplete: vi.fn(() => false),
}))

vi.mock('./OverlayContext', () => ({
  useOverlayWorkspace: contextMocks.useOverlayWorkspace,
}))

vi.mock('./ReviewContext', () => ({
  isReviewTaskComplete: contextMocks.isReviewTaskComplete,
  useOptionalReviewWorkspace: contextMocks.useOptionalReviewWorkspace,
}))

import {
  ManualObjectProvider,
  manualEffectiveProperties,
  missingManualRequiredFields,
  seamSafeBboxFromUv,
  seamSafeBboxFromUvSamples,
  useManualObjectWorkspace,
} from './ManualObjectContext'

const TRAFFIC_LAYER: OverlayLayer = {
  id: 'traffic-layer',
  dataset_id: 'dataset-1',
  name: 'Traffic signs',
  geometry_type: 'Point',
  feature_count: 0,
  revision: 7,
  fields: [
    { name: 'CLASS_NM', type: 'C', required: true },
    { name: 'label', type: 'C' },
  ],
}

const POLE_LAYER: OverlayLayer = {
  id: 'pole-layer',
  dataset_id: 'dataset-1',
  name: 'Sign support poles',
  geometry_type: 'Point',
  feature_count: 0,
  revision: 3,
  fields: [
    { name: 'CLASS_NM', type: 'C', required: true },
    { name: 'NOTE', type: 'C', required: true },
    { name: 'BASE_X', type: 'N', decimal: 3 },
  ],
}

const TEMPLATES: ManualObjectTemplate[] = [
  {
    template_id: 'TRAFFIC_SIGN',
    class_name: 'TRAFFIC_SIGN',
    geometry_type: 'Point',
    tool_id: 'panorama_bbox_point_v1',
    duplicate_radius_m: 0.75,
    continuous: true,
    required_semantics: ['class'],
    relation_semantics: ['support_id'],
    default_values: { label: 'unclassified' },
  },
  {
    template_id: 'SIGN_SUPPORT_POLE',
    class_name: 'SIGN_SUPPORT_POLE',
    geometry_type: 'Point',
    tool_id: 'manual_pole_base_v1',
    duplicate_radius_m: 0.5,
    continuous: true,
    required_semantics: ['class'],
    relation_semantics: [],
    fixed_values: { class: 'SIGN_SUPPORT_POLE' },
    default_values: { NOTE: 'template default' },
  },
]

const FRAME_1: Frame = {
  id: 'frame-1',
  index: 1,
  track_id: 'track-1',
  timestamp: '2026-08-24T00:00:00Z',
  coordinate: null,
  has_panorama: true,
  has_points: true,
}

const FRAME_2: Frame = {
  ...FRAME_1,
  id: 'frame-2',
  index: 2,
  timestamp: '2026-08-24T00:00:01Z',
}

const BBOX: EquirectangularBBoxGeometry = {
  type: 'equirectangular_bbox',
  u_intervals: [[0.2, 0.4]],
  v_min: 0.3,
  v_max: 0.6,
  image_width: 4_096,
  image_height: 2_048,
}

const OBSERVATION: ManualObservation = {
  observation_id: 'observation-1',
  dataset_id: 'dataset-1',
  frame_id: FRAME_1.id,
  view_type: 'panorama',
  class_name: 'TRAFFIC_SIGN',
  geometry_2d: BBOX,
  created_by: 'operator-local',
}

const PROPOSAL: GeometryProposal = {
  proposal_id: 'proposal-1',
  tool_id: 'panorama_bbox_point_v1',
  status: 'auto',
  coordinate_space: 'dataset',
  geometry: { type: 'Point', coordinates: [10, 20, 30] },
  property_patch: {},
  quality: { score: 0.92 },
  reason_codes: [],
  evidence: { frame_id: FRAME_1.id, observation_id: OBSERVATION.observation_id },
}

function makeOverlay(): OverlayContextValue {
  return {
    datasetId: 'dataset-1',
    poleBaseInferenceEnabled: true,
    layers: [TRAFFIC_LAYER, POLE_LAYER],
    features: {},
    visibleLayerIds: new Set([TRAFFIC_LAYER.id, POLE_LAYER.id]),
    activeLayerId: TRAFFIC_LAYER.id,
    setActiveLayerId: vi.fn(),
    selected: null,
    selectedLayer: null,
    selectedFeature: null,
    selectedDatasetFeature: null,
    mapFeatures: [],
    datasetFeatures: [],
    loading: false,
    uploading: false,
    creatingFeature: false,
    pickMode: false,
    pickTarget: null,
    poleBaseProposal: { status: 'idle' },
    setPickMode: vi.fn(),
    beginCreatePoint: vi.fn(),
    beginStagedPointCreate: vi.fn(),
    beginStagedSelectedPointMove: vi.fn(),
    updateStagedPoleBaseTemplateOptions: vi.fn(),
    beginCreatePoleBase: vi.fn(),
    beginRecomputeSelectedPoleBase: vi.fn(),
    applyPoleSeed: vi.fn(async () => undefined),
    confirmPoleBaseProposal: vi.fn(async () => true),
    retryPoleBasePick: vi.fn(),
    cancelPoleBaseProposal: vi.fn(),
    handlePoleBaseFrameChange: vi.fn(),
    refresh: vi.fn(async () => undefined),
    ensureDatasetFeatures: vi.fn(async () => undefined),
    loadMoreDatasetFeatures: vi.fn(async () => undefined),
    upload: vi.fn(async () => undefined),
    updateLayerMetadata: vi.fn(async () => undefined),
    removeLayer: vi.fn(async () => undefined),
    toggleLayer: vi.fn(),
    selectFeature: vi.fn(),
    updateSelected: vi.fn(async () => undefined),
    applyPickedCoordinate: vi.fn(async () => undefined),
    applyPointCloudCoordinate: vi.fn(async () => undefined),
    copySelectedLocation: vi.fn(async () => undefined),
    deleteSelected: vi.fn(async () => undefined),
    deleteField: vi.fn(async () => undefined),
    layerColor: vi.fn(() => '#f97316'),
  }
}

function stateFrameId(state: ReturnType<typeof useManualObjectWorkspace>['proposalState']): string {
  if (state.status === 'ready' || state.status === 'committing') return state.data.frameId
  if (state.status === 'idle') return 'none'
  return state.frameId
}

function WorkspaceProbe() {
  const manual = useManualObjectWorkspace()
  return (
    <div>
      <output data-testid="template-id">{manual.templateId}</output>
      <output data-testid="target-layer-id">{manual.targetLayerId || 'none'}</output>
      <output data-testid="proposal-status">{manual.proposalState.status}</output>
      <output data-testid="proposal-frame">{stateFrameId(manual.proposalState)}</output>
      <output data-testid="proposal-position">
        {manual.proposalPosition?.join(',') ?? 'none'}
      </output>
      <button type="button" onClick={() => manual.setTemplateId('SIGN_SUPPORT_POLE')}>
        select pole
      </button>
      <button type="button" onClick={manual.beginTrafficSignBbox}>begin bbox</button>
      <button type="button" onClick={manual.startSelectedTemplate}>start selected template</button>
      <button type="button" onClick={() => void manual.submitBbox(BBOX)}>submit bbox</button>
      <button type="button" onClick={() => void manual.confirmProposal()}>confirm proposal</button>
    </div>
  )
}

function ManualHarness({ frame }: { frame: Frame }) {
  return (
    <ManualObjectProvider enabled datasetId="dataset-1" frame={frame}>
      <WorkspaceProbe />
    </ManualObjectProvider>
  )
}

function renderManual(frame: Frame = FRAME_1) {
  return render(<ManualHarness frame={frame} />)
}

function mockTemplates() {
  return vi.spyOn(api, 'manualObjectTemplates').mockResolvedValue({ items: TEMPLATES })
}

function mockReadyProposal() {
  vi.spyOn(api, 'createManualObservation').mockResolvedValue({
    observation: OBSERVATION,
    target_layer_id: TRAFFIC_LAYER.id,
  })
  vi.spyOn(api, 'createManualObjectProposal').mockResolvedValue({
    proposal: PROPOSAL,
    target_layer_id: TRAFFIC_LAYER.id,
    expires_in_seconds: 600,
  })
  vi.spyOn(api, 'duplicateManualObjectPreflight').mockResolvedValue({
    exact_duplicate: false,
    blocked: false,
    candidates: [],
    warning_count: 0,
    radius_m: 0.75,
  })
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((promiseResolve, promiseReject) => {
    resolve = promiseResolve
    reject = promiseReject
  })
  return { promise, resolve, reject }
}

beforeEach(() => {
  window.localStorage.clear()
  contextMocks.useOverlayWorkspace.mockReset()
  contextMocks.useOptionalReviewWorkspace.mockReset()
  contextMocks.useOverlayWorkspace.mockReturnValue(makeOverlay())
  contextMocks.useOptionalReviewWorkspace.mockReturnValue(null)
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  window.localStorage.clear()
})

describe('manual panorama bbox helpers', () => {
  it('keeps ordinary boxes in one interval and splits a seam-crossing box', () => {
    const ordinary = seamSafeBboxFromUv(
      { u: 0.2, v: 0.3 },
      { u: 0.4, v: 0.6 },
      4_096,
      2_048,
    )
    expect(ordinary).toMatchObject({
      type: 'equirectangular_bbox',
      v_min: 0.3,
      v_max: 0.6,
      image_width: 4_096,
      image_height: 2_048,
    })
    expect(ordinary?.u_intervals).toHaveLength(1)
    expect(ordinary?.u_intervals[0][0]).toBeCloseTo(0.2)
    expect(ordinary?.u_intervals[0][1]).toBeCloseTo(0.4)

    const seam = seamSafeBboxFromUv(
      { u: 0.95, v: 0.25 },
      { u: 0.05, v: 0.75 },
      4_096,
      2_048,
    )
    expect(seam?.u_intervals).toHaveLength(2)
    expect(seam?.u_intervals[0][0]).toBeCloseTo(0.95)
    expect(seam?.u_intervals[0][1]).toBe(1)
    expect(seam?.u_intervals[1][0]).toBe(0)
    expect(seam?.u_intervals[1][1]).toBeCloseTo(0.05)
  })

  it('uses the minimum circular arc across all samples and rejects sub-pixel boxes', () => {
    const sampled = seamSafeBboxFromUvSamples(
      [
        { u: 0.98, v: 0.2 },
        { u: 0.01, v: 0.5 },
        { u: 0.02, v: 0.8 },
      ],
      1_000,
      500,
    )
    expect(sampled?.u_intervals).toHaveLength(2)
    expect(sampled?.u_intervals[0]).toEqual([0.98, 1])
    expect(sampled?.u_intervals[1][0]).toBe(0)
    expect(sampled?.u_intervals[1][1]).toBeCloseTo(0.02)
    expect(sampled).toMatchObject({ v_min: 0.2, v_max: 0.8 })

    expect(
      seamSafeBboxFromUv(
        { u: 0.2, v: 0.2 },
        { u: 0.2001, v: 0.2001 },
        1_000,
        1_000,
      ),
    ).toBeNull()
    expect(seamSafeBboxFromUvSamples([{ u: 0.2, v: 0.2 }], 1_000, 1_000)).toBeNull()
  })
})

describe('manual template properties', () => {
  it('applies defaults, operator values, fixed values, and class semantics in precedence order', () => {
    const template: ManualObjectTemplate = {
      ...TEMPLATES[0],
      class_name: 'REGULATORY_SIGN',
      default_values: {
        from_default: 'yes',
        shared: 'default',
        locked: 'default',
        CLASS_NM: 'wrong default class',
      },
      fixed_values: { locked: 'fixed', shared_fixed: 'fixed' },
    }
    const fields = [
      { name: 'CLASS_NM', required: true },
      { name: 'shared', required: true },
      { name: 'locked', required: true },
      { name: 'zero', required: true },
      { name: 'missing', required: true },
      { name: 'internal_only', required: true, internal: true },
    ]

    const effective = manualEffectiveProperties(template, fields, {
      shared: 'operator',
      locked: 'operator',
      zero: 0,
      missing: '   ',
    })

    expect(effective).toMatchObject({
      from_default: 'yes',
      shared: 'operator',
      locked: 'fixed',
      shared_fixed: 'fixed',
      zero: 0,
      CLASS_NM: 'REGULATORY_SIGN',
    })
    expect(missingManualRequiredFields(fields, effective)).toEqual(['missing'])
  })
})

describe('ManualObjectProvider state reconciliation', () => {
  it('starts the pole adapter with only effective target fields and fixed class aliases', async () => {
    mockTemplates()
    const overlay = makeOverlay()
    contextMocks.useOverlayWorkspace.mockReturnValue(overlay)
    window.localStorage.setItem('mms.manual-object:dataset-1', JSON.stringify({
      templateId: 'SIGN_SUPPORT_POLE',
      targetLayers: { SIGN_SUPPORT_POLE: POLE_LAYER.id },
    }))
    renderManual()

    await waitFor(() => {
      expect(screen.getByTestId('template-id')).toHaveTextContent('SIGN_SUPPORT_POLE')
      expect(screen.getByTestId('target-layer-id')).toHaveTextContent(POLE_LAYER.id)
    })
    fireEvent.click(screen.getByRole('button', { name: 'start selected template' }))

    expect(overlay.beginStagedPointCreate).toHaveBeenCalledWith(
      POLE_LAYER.id,
      true,
      {
        templateId: 'SIGN_SUPPORT_POLE',
        properties: {
          CLASS_NM: 'SIGN_SUPPORT_POLE',
          NOTE: 'template default',
        },
        requiredFields: ['CLASS_NM', 'NOTE'],
        allowNearDuplicate: false,
        overrideReason: '',
      },
    )
    expect(vi.mocked(overlay.beginStagedPointCreate).mock.calls[0][2]?.properties).not.toHaveProperty('class')
  })

  it('advances Shift+Enter pole review only after a synchronized pole save succeeds', async () => {
    mockTemplates()
    const overlay = makeOverlay()
    const confirmPoleBaseProposal = vi
      .fn<OverlayContextValue['confirmPoleBaseProposal']>()
      .mockResolvedValueOnce(false)
      .mockResolvedValueOnce(true)
    overlay.confirmPoleBaseProposal = confirmPoleBaseProposal
    overlay.poleBaseProposal = {
      status: 'ready',
      target: {
        kind: 'pole-base-create',
        layerId: POLE_LAYER.id,
        continuous: false,
      },
      frameId: FRAME_1.id,
      seed: [10, 20, 35],
      idempotencyKey: 'pole-base-fixture-key',
      result: {
        status: 'auto',
        algorithm: 'manual_seed_axis_ground_intersection',
        algorithm_version: '1',
        coordinate_space: 'dataset',
        seed_position: [10, 20, 35],
        base_position: [10, 20, 30],
        quality: {
          score: 0.9,
          candidate_count: 1,
          ambiguous: false,
          bottom_gap_m: 0.1,
          components: {
            seed: 1,
            axis: 1,
            span: 1,
            continuity: 1,
            ground: 1,
            bottom_gap: 1,
          },
        },
        reason_codes: [],
        warnings: [],
      },
    }
    const moveTask = vi.fn()
    contextMocks.useOverlayWorkspace.mockReturnValue(overlay)
    contextMocks.useOptionalReviewWorkspace.mockReturnValue({ moveTask })
    renderManual()
    await waitFor(() => expect(screen.getByTestId('target-layer-id')).toHaveTextContent(TRAFFIC_LAYER.id))

    fireEvent.keyDown(window, { key: 'Enter', code: 'Enter', shiftKey: true })
    await waitFor(() => expect(confirmPoleBaseProposal).toHaveBeenCalledTimes(1))
    expect(moveTask).not.toHaveBeenCalled()

    fireEvent.keyDown(window, { key: 'Enter', code: 'Enter', shiftKey: true })
    await waitFor(() => expect(confirmPoleBaseProposal).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(moveTask).toHaveBeenCalledWith(1))
  })

  it('switches a remembered pole template back to its traffic layer when M starts bbox mode', async () => {
    mockTemplates()
    const overlay = makeOverlay()
    contextMocks.useOverlayWorkspace.mockReturnValue(overlay)
    window.localStorage.setItem('mms.manual-object:dataset-1', JSON.stringify({
      templateId: 'TRAFFIC_SIGN',
      targetLayers: {
        TRAFFIC_SIGN: TRAFFIC_LAYER.id,
        SIGN_SUPPORT_POLE: POLE_LAYER.id,
      },
    }))
    renderManual()

    await waitFor(() => {
      expect(screen.getByTestId('template-id')).toHaveTextContent('TRAFFIC_SIGN')
      expect(screen.getByTestId('target-layer-id')).toHaveTextContent(TRAFFIC_LAYER.id)
    })
    fireEvent.click(screen.getByRole('button', { name: 'select pole' }))

    await waitFor(() => {
      expect(screen.getByTestId('template-id')).toHaveTextContent('SIGN_SUPPORT_POLE')
      expect(screen.getByTestId('target-layer-id')).toHaveTextContent(POLE_LAYER.id)
    })

    fireEvent.keyDown(window, { key: 'm', code: 'KeyM' })

    await waitFor(() => {
      expect(screen.getByTestId('template-id')).toHaveTextContent('TRAFFIC_SIGN')
      expect(screen.getByTestId('target-layer-id')).toHaveTextContent(TRAFFIC_LAYER.id)
      expect(screen.getByTestId('proposal-status')).toHaveTextContent('drawing')
      expect(screen.getByTestId('proposal-frame')).toHaveTextContent(FRAME_1.id)
    })
    expect(overlay.setActiveLayerId).toHaveBeenCalledWith(TRAFFIC_LAYER.id)
  })

  it('aborts an in-flight duplicate preflight and removes its server proposal on frame change', async () => {
    mockTemplates()
    vi.spyOn(api, 'createManualObservation').mockResolvedValue({
      observation: OBSERVATION,
      target_layer_id: TRAFFIC_LAYER.id,
    })
    vi.spyOn(api, 'createManualObjectProposal').mockResolvedValue({
      proposal: PROPOSAL,
      target_layer_id: TRAFFIC_LAYER.id,
      expires_in_seconds: 600,
    })
    const preflightSignals: AbortSignal[] = []
    vi.spyOn(api, 'duplicateManualObjectPreflight').mockImplementation(
      async (_datasetId, _request, signal) => {
        if (!signal) throw new Error('Expected duplicate preflight to be abortable.')
        preflightSignals.push(signal)
        return await new Promise((_resolve, reject) => {
          signal.addEventListener(
            'abort',
            () => reject(new DOMException('Aborted', 'AbortError')),
            { once: true },
          )
        })
      },
    )
    const deleteProposal = vi
      .spyOn(api, 'deleteManualObjectProposal')
      .mockResolvedValue({ proposal_id: PROPOSAL.proposal_id, deleted: true })
    const view = renderManual()
    await waitFor(() => expect(screen.getByTestId('target-layer-id')).toHaveTextContent(TRAFFIC_LAYER.id))

    fireEvent.click(screen.getByRole('button', { name: 'submit bbox' }))
    await waitFor(() => expect(preflightSignals).toHaveLength(1))
    expect(screen.getByTestId('proposal-status')).toHaveTextContent('loading')

    view.rerender(<ManualHarness frame={FRAME_2} />)

    await waitFor(() => expect(preflightSignals[0].aborted).toBe(true))
    await waitFor(() => {
      expect(deleteProposal).toHaveBeenCalledWith(PROPOSAL.proposal_id)
      expect(screen.getByTestId('proposal-status')).toHaveTextContent('idle')
      expect(screen.getByTestId('proposal-position')).toHaveTextContent('none')
    })
  })

  it('reconciles a completed commit after a frame change without restoring stale proposal UI', async () => {
    mockTemplates()
    mockReadyProposal()
    const overlay = makeOverlay()
    contextMocks.useOverlayWorkspace.mockReturnValue(overlay)
    const commit = deferred<Awaited<ReturnType<typeof api.commitManualObjectProposal>>>()
    vi.spyOn(api, 'commitManualObjectProposal').mockReturnValue(commit.promise)
    const view = renderManual()
    await waitFor(() => expect(screen.getByTestId('target-layer-id')).toHaveTextContent(TRAFFIC_LAYER.id))

    fireEvent.click(screen.getByRole('button', { name: 'begin bbox' }))
    fireEvent.click(screen.getByRole('button', { name: 'submit bbox' }))
    await waitFor(() => {
      expect(screen.getByTestId('proposal-status')).toHaveTextContent('ready')
      expect(screen.getByTestId('proposal-position')).toHaveTextContent('10,20,30')
    })

    fireEvent.click(screen.getByRole('button', { name: 'confirm proposal' }))
    await waitFor(() => expect(screen.getByTestId('proposal-status')).toHaveTextContent('committing'))

    view.rerender(<ManualHarness frame={FRAME_2} />)
    await waitFor(() => {
      expect(screen.getByTestId('proposal-status')).toHaveTextContent('drawing')
      expect(screen.getByTestId('proposal-frame')).toHaveTextContent(FRAME_2.id)
      expect(screen.getByTestId('proposal-position')).toHaveTextContent('none')
    })

    commit.resolve({
      feature: {
        type: 'Feature',
        id: 'feature-8',
        geometry: { type: 'Point', coordinates: [10, 20, 30] },
        properties: { CLASS_NM: 'TRAFFIC_SIGN' },
      },
      revision: 8,
      coordinate_space: 'dataset',
      idempotent_replay: false,
      edit_transaction_id: 'edit-1',
      duplicate_warnings: [],
      task_resolution_pending: false,
    })

    await waitFor(() => expect(overlay.refresh).toHaveBeenCalledTimes(1))
    expect(overlay.selectFeature).not.toHaveBeenCalled()
    expect(screen.getByTestId('proposal-status')).toHaveTextContent('drawing')
    expect(screen.getByTestId('proposal-frame')).toHaveTextContent(FRAME_2.id)
    expect(screen.getByTestId('proposal-position')).toHaveTextContent('none')
  })

  it('retries linked task resolution once after a successful manual commit', async () => {
    mockTemplates()
    mockReadyProposal()
    const overlay = makeOverlay()
    contextMocks.useOverlayWorkspace.mockReturnValue(overlay)
    const reload = vi.fn()
    contextMocks.useOptionalReviewWorkspace.mockReturnValue({
      currentTask: {
        id: 'task-manual-1',
        dataset_id: 'dataset-1',
        target_layer_id: TRAFFIC_LAYER.id,
        status: 'in_progress',
      },
      reload,
    })
    vi.spyOn(api, 'commitManualObjectProposal').mockResolvedValue({
      feature: {
        type: 'Feature',
        id: 'feature-reconciled',
        geometry: { type: 'Point', coordinates: [10, 20, 30] },
        properties: { CLASS_NM: 'TRAFFIC_SIGN' },
      },
      revision: 8,
      coordinate_space: 'dataset',
      idempotent_replay: false,
      edit_transaction_id: 'edit-reconciled',
      duplicate_warnings: [],
      task_resolution_pending: true,
    })
    const resolveReviewTask = vi.spyOn(api, 'resolveReviewTask').mockResolvedValue({
      task: {
        status: 'manual_added',
        resolved_feature_ids: ['feature-reconciled'],
      } as never,
    })
    renderManual()
    await waitFor(() => expect(screen.getByTestId('target-layer-id')).toHaveTextContent(TRAFFIC_LAYER.id))

    fireEvent.click(screen.getByRole('button', { name: 'begin bbox' }))
    fireEvent.click(screen.getByRole('button', { name: 'submit bbox' }))
    await waitFor(() => expect(screen.getByTestId('proposal-status')).toHaveTextContent('ready'))
    fireEvent.click(screen.getByRole('button', { name: 'confirm proposal' }))

    await waitFor(() => expect(resolveReviewTask).toHaveBeenCalledWith('task-manual-1', {
      resolution: 'manual_added',
      resolved_feature_ids: ['feature-reconciled'],
    }))
    await waitFor(() => expect(screen.getByTestId('proposal-status')).toHaveTextContent('drawing'))
    expect(overlay.selectFeature).toHaveBeenCalledWith(
      { layerId: TRAFFIC_LAYER.id, featureId: 'feature-reconciled' },
      { navigate: false },
    )
    expect(reload).toHaveBeenCalled()
  })
})
